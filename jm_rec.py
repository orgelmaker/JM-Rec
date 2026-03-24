#!/usr/bin/env python3
"""
JM-Rec — Organ Sample Recorder
Records pipe organ samples with standardized naming.
Includes web-based remote control for Android, iOS or Windows.

File naming convention: {MIDI_number}-{note_name}.mp3
Example: 036-c.mp3, 037-c#.mp3, 038-d.mp3, ...

Author: Martijn
"""

import os
import sys
import re
import json
import time
import wave
import struct
import threading
import subprocess
import socket
import io
import base64
import webbrowser
import numpy as np
import sounddevice as sd
from pathlib import Path
from flask import Flask, render_template_string, jsonify, request, Response

# Add bundled lame.exe to PATH (PyInstaller onefile extracts to _MEIPASS)
_bundle_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
_lame_path = os.path.join(_bundle_dir, 'lame.exe')
if os.path.exists(_lame_path):
    os.environ['PATH'] = _bundle_dir + os.pathsep + os.environ.get('PATH', '')

try:
    import qrcode
    import qrcode.image.svg
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False

try:
    import soundfile as sf
    HAS_SOUNDFILE = True
except ImportError:
    HAS_SOUNDFILE = False

try:
    import soundcard as sc
    HAS_SOUNDCARD = True
except ImportError:
    HAS_SOUNDCARD = False

# ─────────────────────────────────────────────
# Constants & Note Mapping
# ─────────────────────────────────────────────

NOTE_NAMES = ['c', 'c#', 'd', 'd#', 'e', 'f', 'f#', 'g', 'g#', 'a', 'a#', 'b']

# Display names (for UI) - with octave
def midi_to_display(midi_num):
    """Convert MIDI number to display name like C2, C#2, D2, etc."""
    octave = (midi_num // 12) - 1
    note = NOTE_NAMES[midi_num % 12]
    return f"{note.upper()}{octave}"

def midi_to_filename(midi_num):
    """Convert MIDI number to filename like 036-c, 037-c#, etc."""
    note = NOTE_NAMES[midi_num % 12]
    return f"{midi_num:03d}-{note}"

def format_register_name(name):
    """Format register input to clean folder name.
    'Holpijp 8 voet' -> 'Holpijp_8'
    'Mixtuur 4 sterk' -> 'Mixtuur_4st'
    'Prestant 16' -> 'Prestant_16'
    """
    name = name.strip()
    # Remove 'voet' / "'" (foot mark)
    name = re.sub(r"\s*voet\b", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*'", "", name)
    # 'sterk' -> 'st'
    name = re.sub(r"\s*sterk\b", "st", name, flags=re.IGNORECASE)
    # Replace spaces with underscores
    name = re.sub(r"\s+", "_", name.strip())
    # Remove unsafe chars
    name = re.sub(r"[^\w\-]", "", name)
    return name or "Register"

def sanitize_device_name(name):
    """Convert audio device name to filesystem-safe folder name."""
    name = re.sub(r"\s*\(.*?\)\s*", "", name)
    name = re.sub(r"[^\w\-]", "_", name)
    name = re.sub(r"_+", "_", name)
    return name.strip("_") or "Device"


# ─────────────────────────────────────────────
# Recorder Engine
# ─────────────────────────────────────────────

class RecorderEngine:
    def __init__(self):
        # Project settings
        self.project_name = ""
        self.register_name = ""
        self.output_dir = str(Path.home() / "JM-Rec")
        
        # Audio settings
        self.sample_rate = 44100
        self.bit_depth = 16  # 16 or 24
        self.channels = 1    # mono by default
        self.output_format = "mp3"  # "mp3", "wav", "flac"
        self.mp3_bitrate = 192
        self.device_indices = []   # list of device indices; empty = system default
        self.device_names = {}     # {index: position name} e.g. {0: "Front", 1: "Rear"}

        # Loopback (system audio) settings
        self.input_mode = "mic"        # "mic" or "loopback"
        self.loopback_device_id = None # soundcard speaker ID

        # Orgel-structuur
        self.keyboards = []        # [{"name": "Hoofdwerk", "zwelwerk": False}, ...]
        self.couplers = []         # [{"source": "Zwelwerk", "target": "Hoofdwerk"}, ...]
        self.has_pedal = False
        self.current_keyboard = "" # selected keyboard/pedal name
        self.tremulant = False     # append _trem to register folder

        # Recording workflow settings
        self.countdown_seconds = 5
        self.record_seconds = 5

        # Register range (MIDI numbers)
        self.start_note = 36   # C2
        self.end_note = 96     # C7
        self.current_note = 36

        # Bas/Discant split
        self.bass_treble_split = False
        self.split_note = 60   # C4 (middle C) — first note of discant
        self.split_record_bas = True
        self.split_record_disc = True

        # State
        self.state = "idle"  # idle, countdown, recording, paused
        self.countdown_value = 0
        self.recording_data = []
        self.is_running = False
        self.auto_advance = True

        # Volume / gain
        self.record_gain = 1.0     # 0.0 – 2.0, applied before save

        # VU meter
        self.current_level = 0.0
        self.current_levels = {}   # per-device levels for multi-mic
        
        # Callbacks
        self.on_state_change = None
        
        # Thread lock
        self.lock = threading.Lock()

        # Review state
        self.review_state = "idle"       # "idle" | "analyzing" | "done"
        self.review_progress = 0.0       # 0.0 - 1.0
        self.review_scope = ""
        self.review_results = []
        self.review_todo = []
        self.review_current_idx = None

    @property
    def device_index(self):
        """Backwards-compatible: return first selected device or None."""
        return self.device_indices[0] if self.device_indices else None

    def get_devices(self):
        """List available audio input devices (WASAPI only to avoid duplicates)."""
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        # Find WASAPI host api index
        wasapi_idx = None
        for idx, api in enumerate(hostapis):
            if 'WASAPI' in api.get('name', ''):
                wasapi_idx = idx
                break
        input_devices = []
        for i, d in enumerate(devices):
            if d['max_input_channels'] > 0:
                # Filter to WASAPI only (if available) to avoid 4x duplicates
                if wasapi_idx is not None and d.get('hostapi') != wasapi_idx:
                    continue
                input_devices.append({
                    'index': i,
                    'name': d['name'],
                    'safe_name': sanitize_device_name(d['name']),
                    'channels': d['max_input_channels'],
                    'sample_rate': int(d['default_samplerate'])
                })
        return input_devices

    def get_loopback_devices(self):
        """List available output devices for loopback ('what you hear') recording."""
        if not HAS_SOUNDCARD:
            return []
        try:
            speakers = sc.all_speakers()
            default_id = sc.default_speaker().id
            devices = []
            for s in speakers:
                devices.append({
                    'id': s.id,
                    'name': s.name,
                    'is_default': s.id == default_id
                })
            return devices
        except Exception as e:
            print(f"Error listing loopback devices: {e}")
            return []

    def _normalize_keyboards(self, keyboards):
        """Convert keyboard list to objects if needed. Accepts strings or dicts."""
        result = []
        for kb in keyboards:
            if isinstance(kb, str):
                result.append({"name": kb, "zwelwerk": False})
            elif isinstance(kb, dict):
                result.append({"name": kb.get("name", ""), "zwelwerk": bool(kb.get("zwelwerk", False))})
        return result

    def _kb_names(self):
        """Get list of keyboard names."""
        return [kb["name"] for kb in self.keyboards]

    def setup_organ(self, organ_name, keyboards, has_pedal, output_dir=None):
        """Set up organ project: creates main folder + keyboard/pedal subfolders."""
        self.project_name = organ_name
        self.keyboards = self._normalize_keyboards(keyboards)
        self.has_pedal = has_pedal
        if output_dir:
            self.output_dir = output_dir
        base = os.path.join(self.output_dir, organ_name)
        os.makedirs(base, exist_ok=True)
        for kb in self.keyboards:
            os.makedirs(os.path.join(base, kb["name"]), exist_ok=True)
        if has_pedal:
            os.makedirs(os.path.join(base, "Pedaal"), exist_ok=True)
        if self.keyboards:
            self.current_keyboard = self.keyboards[0]["name"]
        elif has_pedal:
            self.current_keyboard = "Pedaal"
        self._notify()
        return base

    def get_current_register_path(self):
        """Get the full path for current register."""
        reg_name = self.register_name
        if self.tremulant and not reg_name.endswith("_trem"):
            reg_name += "_trem"
        base = os.path.join(self.output_dir, self.project_name,
                            self.current_keyboard, reg_name)
        if self.bass_treble_split:
            if self.current_note < self.split_note:
                return os.path.join(base, reg_name + "_bas")
            else:
                return os.path.join(base, reg_name + "_dis")
        return base
    
    def get_current_filename(self):
        """Get filename for current note."""
        return midi_to_filename(self.current_note) + ".mp3"
    
    def get_current_display_note(self):
        """Get display name for current note."""
        return midi_to_display(self.current_note)
    
    def get_progress(self):
        """Get recording progress as fraction."""
        total = self.end_note - self.start_note + 1
        done = self.current_note - self.start_note
        return done / total if total > 0 else 0
    
    def get_notes_info(self):
        """Get info about notes to record."""
        total = self.end_note - self.start_note + 1
        done = self.current_note - self.start_note
        return {
            'total': total,
            'done': done,
            'remaining': total - done,
            'current_midi': self.current_note,
            'current_name': self.get_current_display_note(),
            'current_filename': self.get_current_filename()
        }
    
    def setup_project(self, project_name, register_name, output_dir=None):
        """Set up project and register directories."""
        self.project_name = project_name
        self.register_name = register_name
        if output_dir:
            self.output_dir = output_dir

        # Create directories
        path = self.get_current_register_path()
        os.makedirs(path, exist_ok=True)
        # Create multi-mic subdirs if applicable
        if len(self.device_indices) > 1:
            for idx in self.device_indices:
                sub = self.device_names.get(idx, f"Mic_{idx}")
                os.makedirs(os.path.join(path, sub), exist_ok=True)
        return path
    
    def start_recording_cycle(self):
        """Start the countdown → record → next cycle."""
        with self.lock:
            if self.state != "idle" and self.state != "paused":
                return
            self.state = "countdown"
            self.is_running = True
        thread = threading.Thread(target=self._recording_cycle, daemon=True)
        thread.start()
    
    def _should_skip_note(self):
        """Check if current note should be skipped based on split settings."""
        if not self.bass_treble_split:
            return False
        is_bas = self.current_note < self.split_note
        if is_bas and not self.split_record_bas:
            return True
        if not is_bas and not self.split_record_disc:
            return True
        return False

    def _recording_cycle(self):
        """Main recording cycle: countdown → record → (auto)advance."""
        while self.is_running and self.current_note <= self.end_note:
            # Skip notes not in selected split range
            if self._should_skip_note():
                if self.auto_advance and self.current_note < self.end_note:
                    self.current_note += 1
                    continue
                else:
                    self.state = "idle"
                    self.is_running = False
                    self._notify()
                    return

            # Countdown phase
            self.state = "countdown"
            self._notify()
            
            for i in range(self.countdown_seconds, 0, -1):
                if not self.is_running:
                    return
                self.countdown_value = i
                self._notify()
                time.sleep(1)
            
            self.countdown_value = 0
            self._notify()
            
            # Recording phase
            self.state = "recording"
            self._notify()
            
            self._do_record()
            
            if not self.is_running:
                return
            
            # Auto-advance or wait
            if self.auto_advance and self.current_note < self.end_note:
                self.current_note += 1
                # Brief pause between notes
                time.sleep(0.5)
            else:
                self.state = "paused"
                self._notify()
                return
        
        # All done
        self.state = "idle"
        self.is_running = False
        self._notify()
    
    def _do_record(self):
        """Record audio from selected device(s)."""
        if self.input_mode == "loopback":
            self._do_record_loopback()
            return

        frames = int(self.sample_rate * self.record_seconds)
        channels = self.channels
        dtype = 'float32' if self.bit_depth == 24 else 'int16'

        if len(self.device_indices) > 1:
            self._do_record_multi(frames, channels, dtype)
        else:
            dev = self.device_index  # None or single index
            self._do_record_single(dev, frames, channels, dtype)

    def _do_record_single(self, device_index, frames, channels, dtype):
        """Record from a single device (original behavior)."""
        try:
            audio_data = sd.rec(
                frames,
                samplerate=self.sample_rate,
                channels=channels,
                dtype=dtype,
                device=device_index
            )

            start_time = time.time()
            while time.time() - start_time < self.record_seconds:
                if not self.is_running:
                    sd.stop()
                    return
                elapsed = time.time() - start_time
                samples_so_far = int(elapsed * self.sample_rate)
                if samples_so_far > 0 and samples_so_far < len(audio_data):
                    chunk = audio_data[max(0, samples_so_far-1024):samples_so_far]
                    if len(chunk) > 0:
                        if self.bit_depth == 24:
                            rms = np.sqrt(np.mean(chunk.astype(np.float64)**2))
                        else:
                            rms = np.sqrt(np.mean((chunk.astype(np.float64) / 32768.0)**2))
                        self.current_level = min(1.0, rms * 3)
                self._notify()
                time.sleep(0.05)

            sd.wait()
            self._save_audio(audio_data)
            self.current_level = 0.0

        except Exception as e:
            print(f"Recording error: {e}", flush=True)
            import traceback; traceback.print_exc()
            self.current_level = 0.0

    def _do_record_loopback(self):
        """Record system audio ('what you hear') via WASAPI loopback."""
        if not HAS_SOUNDCARD:
            print("Loopback recording requires the 'soundcard' library")
            return

        frames = int(self.sample_rate * self.record_seconds)
        channels = self.channels

        try:
            # Get the speaker to record from
            if self.loopback_device_id:
                speaker = sc.get_speaker(self.loopback_device_id)
            else:
                speaker = sc.default_speaker()

            # Record in chunks for VU meter updates
            chunk_size = int(self.sample_rate * 0.05)  # 50ms chunks
            collected = []

            with speaker.recorder(samplerate=self.sample_rate, channels=channels) as recorder:
                start_time = time.time()
                while time.time() - start_time < self.record_seconds:
                    if not self.is_running:
                        return

                    data = recorder.record(numframes=chunk_size)
                    collected.append(data)

                    # Update VU meter
                    rms = np.sqrt(np.mean(data.astype(np.float64)**2))
                    self.current_level = min(1.0, rms * 3)
                    self._notify()

            audio_data = np.concatenate(collected, axis=0)

            # Trim to exact length
            if len(audio_data) > frames:
                audio_data = audio_data[:frames]

            # soundcard returns float64; convert to match expected format
            if self.bit_depth == 24:
                audio_data = audio_data.astype(np.float32)
            else:
                # Convert float64 to int16
                audio_data = np.clip(audio_data, -1.0, 1.0)
                audio_data = (audio_data * 32767).astype(np.int16)

            self._save_audio(audio_data)
            self.current_level = 0.0

        except Exception as e:
            print(f"Loopback recording error: {e}")
            self.current_level = 0.0

    def _do_record_multi(self, frames, channels, dtype):
        """Record from multiple devices simultaneously using InputStream per device."""
        buffers = {}
        streams = {}

        for dev_idx in self.device_indices:
            buffers[dev_idx] = []

        def make_callback(dev_idx):
            def callback(indata, frame_count, time_info, status):
                buffers[dev_idx].append(indata.copy())
                # Update per-device level
                if self.bit_depth == 24:
                    rms = np.sqrt(np.mean(indata.astype(np.float64)**2))
                else:
                    rms = np.sqrt(np.mean((indata.astype(np.float64) / 32768.0)**2))
                self.current_levels[dev_idx] = min(1.0, rms * 3)
            return callback

        try:
            # Open streams
            for dev_idx in self.device_indices:
                try:
                    stream = sd.InputStream(
                        device=dev_idx,
                        samplerate=self.sample_rate,
                        channels=channels,
                        dtype=dtype,
                        callback=make_callback(dev_idx)
                    )
                    streams[dev_idx] = stream
                except Exception as e:
                    print(f"Warning: Could not open device {dev_idx}: {e}")

            if not streams:
                print("No devices could be opened for multi-mic recording")
                return

            # Start all streams
            for stream in streams.values():
                stream.start()

            # Wait for recording duration
            start_time = time.time()
            while time.time() - start_time < self.record_seconds:
                if not self.is_running:
                    break
                # Primary level = first active device
                primary = next(iter(streams))
                self.current_level = self.current_levels.get(primary, 0.0)
                self._notify()
                time.sleep(0.05)

            # Stop all streams
            for stream in streams.values():
                try:
                    stream.stop()
                    stream.close()
                except:
                    pass

            if not self.is_running:
                return

            # Save per device
            for dev_idx in streams:
                try:
                    if not buffers[dev_idx]:
                        continue
                    audio_data = np.concatenate(buffers[dev_idx], axis=0)
                    # Trim or pad to exact frame count
                    if len(audio_data) > frames:
                        audio_data = audio_data[:frames]
                    elif len(audio_data) < frames:
                        pad_shape = (frames - len(audio_data),) + audio_data.shape[1:]
                        audio_data = np.concatenate([audio_data, np.zeros(pad_shape, dtype=audio_data.dtype)])
                    sub = self.device_names.get(dev_idx, f"Mic_{dev_idx}")
                    self._save_audio(audio_data, subdirectory=sub)
                except Exception as e:
                    print(f"Save error for device {dev_idx}: {e}")

            self.current_level = 0.0
            self.current_levels.clear()

        except Exception as e:
            print(f"Multi-recording error: {e}")
            self.current_level = 0.0
            for stream in streams.values():
                try:
                    stream.stop()
                    stream.close()
                except:
                    pass
    
    def _save_audio(self, audio_data, subdirectory=None):
        """Save recorded audio in chosen format (mp3/wav/flac)."""
        path = self.get_current_register_path()
        if subdirectory:
            path = os.path.join(path, subdirectory)
            os.makedirs(path, exist_ok=True)
        filename = midi_to_filename(self.current_note)

        # Apply recording gain
        if self.record_gain != 1.0:
            if audio_data.dtype in (np.float32, np.float64):
                audio_data = np.clip(audio_data * self.record_gain, -1.0, 1.0).astype(audio_data.dtype)
            else:
                audio_data = np.clip(audio_data.astype(np.float64) * self.record_gain, -32768, 32767).astype(np.int16)

        fmt = self.output_format

        if fmt == "wav":
            self._write_wav(os.path.join(path, filename + ".wav"), audio_data)

        elif fmt == "flac":
            if not HAS_SOUNDFILE:
                print("FLAC not available: soundfile module missing, saving as WAV")
                self._write_wav(os.path.join(path, filename + ".wav"), audio_data)
                return
            flac_path = os.path.join(path, filename + ".flac")
            subtype = 'PCM_24' if self.bit_depth == 24 else 'PCM_16'
            sf.write(flac_path, audio_data, self.sample_rate, subtype=subtype)

        else:  # mp3
            wav_path = os.path.join(path, filename + ".wav")
            mp3_path = os.path.join(path, filename + ".mp3")
            self._write_wav(wav_path, audio_data)
            _cflags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            mp3_ok = False

            # Try LAME first (bundled with PyInstaller build)
            try:
                subprocess.run([
                    'lame', '-b', str(self.mp3_bitrate),
                    '--quiet', wav_path, mp3_path
                ], check=True, creationflags=_cflags,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                mp3_ok = True
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass

            # Fallback: try ffmpeg
            if not mp3_ok:
                try:
                    subprocess.run([
                        'ffmpeg', '-y', '-i', wav_path,
                        '-b:a', f'{self.mp3_bitrate}k', '-q:a', '0',
                        mp3_path
                    ], check=True, creationflags=_cflags,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    mp3_ok = True
                except (subprocess.CalledProcessError, FileNotFoundError):
                    pass

            if mp3_ok:
                os.remove(wav_path)
            else:
                print("No MP3 encoder (lame/ffmpeg) found, keeping WAV")

    def _write_wav(self, wav_path, audio_data):
        """Write audio data to a WAV file."""
        if self.bit_depth == 24:
            audio_int = (audio_data * 2147483647).astype(np.int32)
            with wave.open(wav_path, 'w') as wf:
                wf.setnchannels(self.channels)
                wf.setsampwidth(3)
                wf.setframerate(self.sample_rate)
                raw_bytes = b''
                for sample in audio_int.flatten():
                    b = struct.pack('<i', sample)
                    raw_bytes += b[1:4]
                wf.writeframes(raw_bytes)
        else:
            with wave.open(wav_path, 'w') as wf:
                wf.setnchannels(self.channels)
                wf.setsampwidth(2)
                wf.setframerate(self.sample_rate)
                wf.writeframes(audio_data.tobytes())
    
    # ─── Sample Review / Analysis ───────────────────────

    def _load_audio_file(self, filepath):
        """Load audio file as (float32 numpy array, sample_rate). Returns (None, None) on failure."""
        ext = os.path.splitext(filepath)[1].lower()
        try:
            if ext in ('.wav', '.flac') and HAS_SOUNDFILE:
                data, sr = sf.read(filepath, dtype='float32')
                return data, sr
            elif ext == '.wav':
                with wave.open(filepath, 'r') as wf:
                    sr = wf.getframerate()
                    n = wf.getnframes()
                    raw = wf.readframes(n)
                    sw = wf.getsampwidth()
                    if sw == 2:
                        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                    else:
                        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                    return data, sr
            elif ext == '.mp3':
                _cflags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                result = subprocess.run(
                    ['lame', '--decode', filepath, '-'],
                    capture_output=True, creationflags=_cflags
                )
                if result.returncode == 0 and len(result.stdout) > 44:
                    buf = io.BytesIO(result.stdout)
                    if HAS_SOUNDFILE:
                        data, sr = sf.read(buf, dtype='float32')
                        return data, sr
                    else:
                        with wave.open(buf, 'r') as wf:
                            sr = wf.getframerate()
                            raw = wf.readframes(wf.getnframes())
                            data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                            return data, sr
        except Exception as e:
            print(f"Load audio error ({filepath}): {e}")
        return None, None

    def _write_audio_file(self, filepath, data, sr):
        """Write float32 numpy array back to file in its original format."""
        ext = os.path.splitext(filepath)[1].lower()
        try:
            if ext == '.flac' and HAS_SOUNDFILE:
                sf.write(filepath, data, sr, subtype='PCM_16')
            elif ext == '.wav' and HAS_SOUNDFILE:
                sf.write(filepath, data, sr, subtype='PCM_16')
            elif ext == '.wav':
                audio_int = (data * 32767).astype(np.int16)
                with wave.open(filepath, 'w') as wf:
                    ch = 2 if len(data.shape) > 1 and data.shape[1] == 2 else 1
                    wf.setnchannels(ch)
                    wf.setsampwidth(2)
                    wf.setframerate(sr)
                    wf.writeframes(audio_int.tobytes())
            elif ext == '.mp3':
                tmp_wav = filepath + '.tmp.wav'
                if HAS_SOUNDFILE:
                    sf.write(tmp_wav, data, sr, subtype='PCM_16')
                else:
                    audio_int = (data * 32767).astype(np.int16)
                    with wave.open(tmp_wav, 'w') as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)
                        wf.setframerate(sr)
                        wf.writeframes(audio_int.tobytes())
                _cflags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                subprocess.run(
                    ['lame', '-b', str(self.mp3_bitrate), '--quiet', tmp_wav, filepath],
                    creationflags=_cflags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                os.remove(tmp_wav)
        except Exception as e:
            print(f"Write audio error ({filepath}): {e}")

    def _calculate_trim(self, data, sr, threshold_db=-40):
        """Return (start_sample, end_sample) for non-silent region."""
        threshold = 10 ** (threshold_db / 20.0)
        abs_data = np.abs(data.flatten())
        above = np.where(abs_data > threshold)[0]
        if len(above) == 0:
            return 0, 0
        margin = int(sr * 0.01)
        start = max(0, above[0] - margin)
        end = min(len(abs_data), above[-1] + margin)
        return start, end

    def _check_silent(self, data, sr):
        rms = np.sqrt(np.mean(data.astype(np.float64) ** 2))
        peak = np.max(np.abs(data))
        threshold = 10 ** (-40 / 20.0)
        if rms < threshold and peak < threshold * 2:
            return {"issue": "silent", "detail": f"Stil: RMS {20*np.log10(max(rms,1e-10)):.0f}dB", "severity": "error"}
        return None

    def _check_clipping(self, data, sr):
        abs_data = np.abs(data.flatten())
        clip_count = np.sum(abs_data >= 0.99)
        if clip_count >= 10:
            pct = clip_count / len(abs_data) * 100
            sev = "error" if pct > 1.0 else "warning"
            return {"issue": "clipping", "detail": f"Clipping: {pct:.1f}% overstuurd", "severity": sev}
        return None

    def _check_short(self, data, sr):
        duration = len(data.flatten()) / sr
        if duration < self.record_seconds * 0.5:
            return {"issue": "short", "detail": f"Te kort: {duration:.1f}s (verwacht {self.record_seconds}s)", "severity": "warning"}
        return None

    def _check_noise(self, data, sr, stats):
        if not stats or stats.get('rms_std', 0) == 0:
            return None
        rms = np.sqrt(np.mean(data.astype(np.float64) ** 2))
        z = abs(rms - stats['rms_mean']) / stats['rms_std']
        if z > 3.0:
            return {"issue": "noise", "detail": f"Afwijkend volume: z-score {z:.1f}", "severity": "warning"}
        return None

    def _find_missing_notes(self, folder, start_note, end_note, fmt):
        missing = []
        for midi in range(start_note, end_note + 1):
            if self.bass_treble_split:
                if not self.split_record_bas and midi < self.split_note:
                    continue
                if not self.split_record_disc and midi >= self.split_note:
                    continue
            expected = midi_to_filename(midi) + "." + fmt
            if not os.path.exists(os.path.join(folder, expected)):
                missing.append(midi)
        return missing

    def start_review(self, scope, path, trim=True):
        """Start sample review in background thread."""
        if self.review_state == "analyzing":
            return
        self.review_state = "analyzing"
        self.review_progress = 0.0
        self.review_results = []
        self.review_todo = []
        self.review_current_idx = None
        self.review_scope = scope
        self._notify()
        thread = threading.Thread(target=self._run_review, args=(path, trim), daemon=True)
        thread.start()

    def _run_review(self, base_path, trim):
        """Background: scan and analyze all samples."""
        try:
            self._do_review(base_path, trim)
        except Exception as e:
            print(f"Review error: {e}")
        self.review_state = "done"
        self.review_progress = 1.0
        self._notify()

    def _do_review(self, base_path, trim):
        """Core review logic with two passes."""
        # Collect all audio files
        files = []  # (full_path, keyboard, register, midi_num)
        fmt = self.output_format

        if not os.path.isdir(base_path):
            return

        # Walk and collect audio files
        for root, dirs, filenames in os.walk(base_path):
            for fn in sorted(filenames):
                ext = os.path.splitext(fn)[1].lower().lstrip('.')
                if ext not in ('mp3', 'wav', 'flac'):
                    continue
                # Extract MIDI number from filename like "036-c.mp3"
                match = re.match(r'^(\d{3})-', fn)
                if not match:
                    continue
                midi = int(match.group(1))
                # Determine keyboard and register from path
                rel = os.path.relpath(root, base_path)
                parts = rel.replace('\\', '/').split('/')
                parts = [p for p in parts if p != '.']
                kb = parts[0] if len(parts) >= 2 else self.current_keyboard
                reg = parts[1] if len(parts) >= 2 else (parts[0] if parts else self.register_name)
                files.append((os.path.join(root, fn), kb, reg, midi))

        total = len(files)
        if total == 0:
            return

        # Pass 1: collect statistics per folder (0-50%)
        folder_stats = {}
        for i, (fpath, kb, reg, midi) in enumerate(files):
            if self.review_state != "analyzing":
                return
            self.review_progress = (i / total) * 0.5
            self._notify()
            data, sr = self._load_audio_file(fpath)
            if data is None:
                continue
            folder = os.path.dirname(fpath)
            if folder not in folder_stats:
                folder_stats[folder] = {'rms_values': [], 'fmt': os.path.splitext(fpath)[1].lower().lstrip('.')}
            rms = np.sqrt(np.mean(data.astype(np.float64) ** 2))
            folder_stats[folder]['rms_values'].append(rms)

        # Compute means/stds
        for folder in folder_stats:
            vals = folder_stats[folder]['rms_values']
            folder_stats[folder]['rms_mean'] = float(np.mean(vals)) if vals else 0
            folder_stats[folder]['rms_std'] = float(np.std(vals)) if vals else 0

        # Pass 2: analyze each file + trim (50-100%)
        for i, (fpath, kb, reg, midi) in enumerate(files):
            if self.review_state != "analyzing":
                return
            self.review_progress = 0.5 + (i / total) * 0.5
            self._notify()
            data, sr = self._load_audio_file(fpath)
            if data is None:
                continue

            issues = []
            r = self._check_silent(data, sr)
            if r:
                issues.append(r)
            r = self._check_clipping(data, sr)
            if r:
                issues.append(r)
            r = self._check_short(data, sr)
            if r:
                issues.append(r)
            folder = os.path.dirname(fpath)
            stats = folder_stats.get(folder, {})
            r = self._check_noise(data, sr, stats)
            if r:
                issues.append(r)

            # Trim silence (only if not silent)
            if trim and not any(x['issue'] == 'silent' for x in issues):
                start, end = self._calculate_trim(data, sr)
                if end > start and (start > 0 or end < len(data.flatten())):
                    if len(data.shape) > 1:
                        trimmed = data[start:end, :]
                    else:
                        trimmed = data[start:end]
                    self._write_audio_file(fpath, trimmed, sr)

            for issue in issues:
                self.review_results.append({
                    "file": os.path.basename(fpath),
                    "path": fpath,
                    "midi": midi,
                    "note": midi_to_display(midi),
                    "keyboard": kb,
                    "register": reg,
                    **issue
                })

        # Check missing notes per register folder
        for folder, stats in folder_stats.items():
            ext = stats.get('fmt', fmt)
            missing = self._find_missing_notes(folder, self.start_note, self.end_note, ext)
            rel = os.path.relpath(folder, base_path)
            parts = rel.replace('\\', '/').split('/')
            parts = [p for p in parts if p != '.']
            kb = parts[0] if len(parts) >= 2 else self.current_keyboard
            reg = parts[1] if len(parts) >= 2 else (parts[0] if parts else self.register_name)
            for midi in missing:
                self.review_results.append({
                    "file": midi_to_filename(midi) + "." + ext,
                    "path": os.path.join(folder, midi_to_filename(midi) + "." + ext),
                    "midi": midi,
                    "note": midi_to_display(midi),
                    "keyboard": kb,
                    "register": reg,
                    "issue": "missing",
                    "detail": f"Ontbreekt: {midi_to_display(midi)}",
                    "severity": "error"
                })

        # Build todo list (errors only, sorted)
        self.review_todo = [r for r in self.review_results if r['severity'] == 'error']
        self.review_todo.sort(key=lambda x: (x.get('keyboard', ''), x.get('register', ''), x.get('midi', 0)))

    def stop(self):
        """Stop recording cycle."""
        self.is_running = False
        self.state = "idle"
        self.current_level = 0.0
        self.current_levels.clear()
        try:
            sd.stop()
        except:
            pass
        self._notify()
    
    def pause(self):
        """Pause after current recording."""
        self.is_running = False
        self.state = "paused"
        self._notify()
    
    def next_note(self):
        """Move to next note."""
        if self.current_note < self.end_note:
            self.current_note += 1
            self._notify()
    
    def prev_note(self):
        """Move to previous note."""
        if self.current_note > self.start_note:
            self.current_note -= 1
            self._notify()
    
    def redo_note(self):
        """Re-record current note (don't advance)."""
        self.auto_advance = False
        self.start_recording_cycle()
    
    def set_note(self, midi_num):
        """Jump to specific note."""
        if self.start_note <= midi_num <= self.end_note:
            self.current_note = midi_num
            self._notify()
    
    def new_register(self, register_name, tremulant=False):
        """Start a new register."""
        self.stop()
        self.register_name = register_name
        self.tremulant = tremulant
        self.current_note = self.start_note
        # Create directories (including bas/discant if enabled)
        if self.bass_treble_split:
            reg_name = register_name
            if tremulant and not reg_name.endswith("_trem"):
                reg_name += "_trem"
            base = os.path.join(self.output_dir, self.project_name,
                                self.current_keyboard, reg_name)
            parts = []
            if self.split_record_bas:
                parts.append("_bas")
            if self.split_record_disc:
                parts.append("_dis")
            for suffix in parts:
                sub = os.path.join(base, reg_name + suffix)
                os.makedirs(sub, exist_ok=True)
                if len(self.device_indices) > 1:
                    for idx in self.device_indices:
                        mic = self.device_names.get(idx, f"Mic_{idx}")
                        os.makedirs(os.path.join(sub, mic), exist_ok=True)
        else:
            path = self.get_current_register_path()
            os.makedirs(path, exist_ok=True)
            if len(self.device_indices) > 1:
                for idx in self.device_indices:
                    sub = self.device_names.get(idx, f"Mic_{idx}")
                    os.makedirs(os.path.join(path, sub), exist_ok=True)
        self._notify()
        return self.get_current_register_path()
    
    def get_state(self):
        """Get full state for UI/remote."""
        with self.lock:
            return {
                'state': self.state,
                'project': self.project_name,
                'register': self.register_name,
                'output_dir': self.output_dir,
                'keyboards': self.keyboards,
                'couplers': self.couplers,
                'has_pedal': self.has_pedal,
                'current_keyboard': self.current_keyboard,
                'tremulant': self.tremulant,
                'countdown': self.countdown_value,
                'note': self.get_notes_info(),
                'progress': self.get_progress(),
                'level': self.current_level,
                'levels': dict(self.current_levels),
                'settings': {
                    'sample_rate': self.sample_rate,
                    'bit_depth': self.bit_depth,
                    'channels': self.channels,
                    'output_format': self.output_format,
                    'mp3_bitrate': self.mp3_bitrate,
                    'countdown_seconds': self.countdown_seconds,
                    'record_seconds': self.record_seconds,
                    'start_note': self.start_note,
                    'end_note': self.end_note,
                    'device_index': self.device_index,
                    'device_indices': list(self.device_indices),
                    'device_names': dict(self.device_names),
                    'input_mode': self.input_mode,
                    'loopback_device_id': self.loopback_device_id,
                    'has_soundcard': HAS_SOUNDCARD,
                    'record_gain': self.record_gain,
                    'bass_treble_split': self.bass_treble_split,
                    'split_note': self.split_note,
                    'split_record_bas': self.split_record_bas,
                    'split_record_disc': self.split_record_disc,
                },
                'review': {
                    'state': self.review_state,
                    'progress': self.review_progress,
                    'scope': self.review_scope,
                    'total': len(self.review_results),
                    'errors': sum(1 for r in self.review_results if r.get('severity') == 'error'),
                    'warnings': sum(1 for r in self.review_results if r.get('severity') == 'warning'),
                    'todo_count': len(self.review_todo),
                    'current_idx': self.review_current_idx,
                }
            }
    
    def _notify(self):
        """Notify UI of state change."""
        if self.on_state_change:
            try:
                self.on_state_change(self.get_state())
            except:
                pass


# ─────────────────────────────────────────────
# Web Server (Remote Control)
# ─────────────────────────────────────────────

def get_local_ip():
    """Get local IP address for remote access."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"


def create_web_app(engine: RecorderEngine):
    """Create Flask web application for remote control."""
    
    app = Flask(__name__)
    
    # ── Main Remote Control Page ──
    @app.route('/')
    def index():
        return render_template_string(REMOTE_HTML)
    
    # ── Display Page (for main screen) ──
    @app.route('/display')
    def display():
        return render_template_string(DISPLAY_HTML)
    
    # ── API Endpoints ──
    @app.route('/api/state')
    def api_state():
        return jsonify(engine.get_state())
    
    @app.route('/api/devices')
    def api_devices():
        return jsonify(engine.get_devices())

    @app.route('/api/loopback-devices')
    def api_loopback_devices():
        return jsonify(engine.get_loopback_devices())

    @app.route('/api/setup', methods=['POST'])
    def api_setup():
        data = request.json
        if 'project' in data and 'register' in data:
            path = engine.setup_project(
                data['project'], 
                data['register'],
                data.get('output_dir')
            )
            return jsonify({'success': True, 'path': path})
        return jsonify({'success': False, 'error': 'Missing project or register name'})

    @app.route('/api/setup-organ', methods=['POST'])
    def api_setup_organ():
        data = request.json
        organ = data.get('organ', '').strip()
        keyboards = data.get('keyboards', [])
        has_pedal = data.get('has_pedal', False)
        output_dir = data.get('output_dir')
        if not organ:
            return jsonify({'success': False, 'error': 'Missing organ name'})
        if not keyboards and not has_pedal:
            return jsonify({'success': False, 'error': 'Need at least one keyboard or pedal'})
        path = engine.setup_organ(organ, keyboards, has_pedal, output_dir)
        return jsonify({'success': True, 'path': path})

    @app.route('/api/select-keyboard', methods=['POST'])
    def api_select_keyboard():
        data = request.json
        kb = data.get('keyboard', '').strip()
        available = engine._kb_names()
        if engine.has_pedal:
            available.append('Pedaal')
        if kb not in available:
            return jsonify({'success': False, 'error': f'Unknown keyboard: {kb}'})
        engine.current_keyboard = kb
        engine._notify()
        return jsonify({'success': True, 'current_keyboard': kb})

    @app.route('/api/format-register', methods=['POST'])
    def api_format_register():
        data = request.json
        name = data.get('name', '')
        tremulant = data.get('tremulant', False)
        formatted = format_register_name(name)
        if tremulant and formatted and not formatted.endswith('_trem'):
            formatted += '_trem'
        return jsonify({'formatted': formatted})

    @app.route('/api/settings', methods=['POST'])
    def api_settings():
        data = request.json
        if 'sample_rate' in data:
            engine.sample_rate = int(data['sample_rate'])
        if 'bit_depth' in data:
            engine.bit_depth = int(data['bit_depth'])
        if 'channels' in data:
            engine.channels = int(data['channels'])
        if 'output_format' in data:
            if data['output_format'] in ('mp3', 'wav', 'flac'):
                engine.output_format = data['output_format']
        if 'mp3_bitrate' in data:
            engine.mp3_bitrate = int(data['mp3_bitrate'])
        if 'countdown_seconds' in data:
            engine.countdown_seconds = int(data['countdown_seconds'])
        if 'record_seconds' in data:
            engine.record_seconds = int(data['record_seconds'])
        if 'start_note' in data:
            engine.start_note = int(data['start_note'])
            engine.current_note = max(engine.current_note, engine.start_note)
        if 'end_note' in data:
            engine.end_note = int(data['end_note'])
            engine.current_note = min(engine.current_note, engine.end_note)
        if 'device_index' in data:
            val = data['device_index']
            engine.device_indices = [int(val)] if val is not None else []
        if 'device_indices' in data:
            engine.device_indices = [int(i) for i in data['device_indices']] if data['device_indices'] else []
        if 'device_names' in data:
            engine.device_names = {int(k): v for k, v in data['device_names'].items()}
        if 'input_mode' in data:
            engine.input_mode = data['input_mode'] if data['input_mode'] in ('mic', 'loopback') else 'mic'
        if 'loopback_device_id' in data:
            engine.loopback_device_id = data['loopback_device_id']
        if 'record_gain' in data:
            engine.record_gain = max(0.0, min(2.0, float(data['record_gain'])))
        if 'bass_treble_split' in data:
            engine.bass_treble_split = bool(data['bass_treble_split'])
        if 'split_note' in data:
            engine.split_note = int(data['split_note'])
        if 'split_record_bas' in data:
            engine.split_record_bas = bool(data['split_record_bas'])
        if 'split_record_disc' in data:
            engine.split_record_disc = bool(data['split_record_disc'])
        return jsonify({'success': True, 'state': engine.get_state()})
    
    @app.route('/api/record', methods=['POST'])
    def api_record():
        engine.auto_advance = True
        engine.start_recording_cycle()
        return jsonify({'success': True})
    
    @app.route('/api/record-single', methods=['POST'])
    def api_record_single():
        engine.auto_advance = False
        engine.start_recording_cycle()
        return jsonify({'success': True})
    
    @app.route('/api/stop', methods=['POST'])
    def api_stop():
        engine.stop()
        return jsonify({'success': True})
    
    @app.route('/api/pause', methods=['POST'])
    def api_pause():
        engine.pause()
        return jsonify({'success': True})
    
    @app.route('/api/next', methods=['POST'])
    def api_next():
        engine.next_note()
        return jsonify({'success': True})
    
    @app.route('/api/prev', methods=['POST'])
    def api_prev():
        engine.prev_note()
        return jsonify({'success': True})
    
    @app.route('/api/redo', methods=['POST'])
    def api_redo():
        engine.redo_note()
        return jsonify({'success': True})
    
    @app.route('/api/set-note', methods=['POST'])
    def api_set_note():
        data = request.json
        if 'midi' in data:
            engine.set_note(int(data['midi']))
        return jsonify({'success': True})
    
    @app.route('/api/new-register', methods=['POST'])
    def api_new_register():
        data = request.json
        if 'name' in data:
            name = format_register_name(data['name'])
            tremulant = data.get('tremulant', False)
            path = engine.new_register(name, tremulant=tremulant)
            return jsonify({'success': True, 'path': path, 'formatted_name': name})
        return jsonify({'success': False, 'error': 'Missing register name'})

    @app.route('/api/qr.svg')
    def api_qr_svg():
        """Generate QR code SVG for the remote control URL."""
        local_ip = get_local_ip()
        port = request.host.split(':')[-1] if ':' in request.host else '5555'
        remote_url = f"http://{local_ip}:{port}"

        if HAS_QRCODE:
            qr = qrcode.QRCode(version=1, box_size=10, border=2)
            qr.add_data(remote_url)
            qr.make(fit=True)
            factory = qrcode.image.svg.SvgPathImage
            img = qr.make_image(image_factory=factory, fill_color="#000000", back_color="#ffffff")
            buf = io.BytesIO()
            img.save(buf)
            return Response(buf.getvalue(), mimetype='image/svg+xml')
        else:
            # Fallback: return a simple placeholder SVG
            svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="200" height="60">
                <rect width="200" height="60" rx="8" fill="#12121a" stroke="#1e1e2e"/>
                <text x="100" y="35" text-anchor="middle" fill="#6b6b8a" font-family="monospace" font-size="11">QR niet beschikbaar</text>
            </svg>'''
            return Response(svg, mimetype='image/svg+xml')

    @app.route('/api/remote-url')
    def api_remote_url():
        """Get the remote control URL."""
        local_ip = get_local_ip()
        port = request.host.split(':')[-1] if ':' in request.host else '5555'
        return jsonify({'url': f"http://{local_ip}:{port}"})

    # ── Project export ──

    @app.route('/api/export-project', methods=['POST'])
    def api_export_project():
        """Export project metadata as JM-Rec JSON for JM-Orgue import."""
        if not engine.project_name:
            return jsonify({'success': False, 'error': 'Geen project ingesteld'})

        # Scan all registers from disk
        base = os.path.join(engine.output_dir, engine.project_name)
        registers = {}
        for kb in engine.keyboards:
            kb_path = os.path.join(base, kb["name"])
            if not os.path.isdir(kb_path):
                continue
            regs = []
            for entry in sorted(os.listdir(kb_path)):
                reg_path = os.path.join(kb_path, entry)
                if os.path.isdir(reg_path):
                    # Count samples
                    samples = [f for f in os.listdir(reg_path)
                               if f.endswith(('.mp3', '.wav', '.flac'))]
                    # Check for sub-mic folders
                    sub_mics = [d for d in os.listdir(reg_path)
                                if os.path.isdir(os.path.join(reg_path, d))]
                    if sub_mics and not samples:
                        samples = [f for f in os.listdir(os.path.join(reg_path, sub_mics[0]))
                                   if f.endswith(('.mp3', '.wav', '.flac'))]
                    regs.append({
                        "name": entry,
                        "tremulant": entry.endswith("_trem"),
                        "samples": len(samples),
                        "mics": sub_mics if sub_mics else [],
                    })
            registers[kb["name"]] = regs

        if engine.has_pedal:
            pedaal_path = os.path.join(base, "Pedaal")
            if os.path.isdir(pedaal_path):
                regs = []
                for entry in sorted(os.listdir(pedaal_path)):
                    if os.path.isdir(os.path.join(pedaal_path, entry)):
                        samples = [f for f in os.listdir(os.path.join(pedaal_path, entry))
                                   if f.endswith(('.mp3', '.wav', '.flac'))]
                        regs.append({"name": entry, "tremulant": entry.endswith("_trem"), "samples": len(samples), "mics": []})
                registers["Pedaal"] = regs

        project = {
            "jm_rec_version": "3.2",
            "organ": engine.project_name,
            "keyboards": engine.keyboards,
            "has_pedal": engine.has_pedal,
            "couplers": engine.couplers,
            "registers": registers,
            "settings": {
                "sample_rate": engine.sample_rate,
                "bit_depth": engine.bit_depth,
                "channels": engine.channels,
                "output_format": engine.output_format,
                "start_note": engine.start_note,
                "end_note": engine.end_note,
            }
        }

        export_path = os.path.join(base, engine.project_name + ".jm-rec.json")
        with open(export_path, 'w', encoding='utf-8') as f:
            json.dump(project, f, indent=2, ensure_ascii=False)

        return jsonify({'success': True, 'path': export_path, 'project': project})

    # ── Coupler endpoints ──

    @app.route('/api/add-coupler', methods=['POST'])
    def api_add_coupler():
        data = request.json or {}
        source = data.get('source', '').strip()
        target = data.get('target', '').strip()
        if not source or not target or source == target:
            return jsonify({'success': False, 'error': 'Ongeldige koppel'})
        coupler = {"source": source, "target": target}
        if coupler not in engine.couplers:
            engine.couplers.append(coupler)
            engine._notify()
        return jsonify({'success': True, 'couplers': engine.couplers})

    @app.route('/api/remove-coupler', methods=['POST'])
    def api_remove_coupler():
        data = request.json or {}
        idx = data.get('index')
        if idx is not None and 0 <= idx < len(engine.couplers):
            engine.couplers.pop(idx)
            engine._notify()
            return jsonify({'success': True, 'couplers': engine.couplers})
        return jsonify({'success': False})

    # ── Review endpoints ──

    @app.route('/api/review-start', methods=['POST'])
    def api_review_start():
        data = request.json or {}
        scope = data.get('scope', 'register')
        trim = data.get('trim', True)
        custom_path = data.get('path', '').strip()
        if custom_path and os.path.isdir(custom_path):
            path = custom_path
        elif scope == 'register':
            path = engine.get_current_register_path()
        elif scope == 'keyboard':
            path = os.path.join(engine.output_dir, engine.project_name, engine.current_keyboard)
        else:
            path = os.path.join(engine.output_dir, engine.project_name)
        engine.start_review(scope, path, trim)
        return jsonify({'success': True, 'path': path})

    @app.route('/api/review-stop', methods=['POST'])
    def api_review_stop():
        engine.review_state = "idle"
        engine.review_progress = 0.0
        engine._notify()
        return jsonify({'success': True})

    @app.route('/api/review-results')
    def api_review_results():
        return jsonify({
            'state': engine.review_state,
            'progress': engine.review_progress,
            'results': engine.review_results,
            'todo': engine.review_todo,
            'current_idx': engine.review_current_idx,
        })

    @app.route('/api/review-goto', methods=['POST'])
    def api_review_goto():
        data = request.json or {}
        idx = data.get('index', 0)
        if 0 <= idx < len(engine.review_todo):
            item = engine.review_todo[idx]
            engine.review_current_idx = idx
            engine.current_keyboard = item.get('keyboard', engine.current_keyboard)
            engine.register_name = item.get('register', engine.register_name)
            engine.set_note(item['midi'])
            return jsonify({'success': True, 'item': item})
        return jsonify({'success': False, 'error': 'Ongeldig item'})

    @app.route('/api/review-next', methods=['POST'])
    def api_review_next():
        idx = (engine.review_current_idx or 0) + 1
        if idx < len(engine.review_todo):
            item = engine.review_todo[idx]
            engine.review_current_idx = idx
            engine.current_keyboard = item.get('keyboard', engine.current_keyboard)
            engine.register_name = item.get('register', engine.register_name)
            engine.set_note(item['midi'])
            return jsonify({'success': True, 'item': item})
        return jsonify({'success': False, 'error': 'Geen volgend item'})

    @app.route('/api/review-mark-done', methods=['POST'])
    def api_review_mark_done():
        data = request.json or {}
        idx = data.get('index', engine.review_current_idx)
        if idx is not None and 0 <= idx < len(engine.review_todo):
            engine.review_todo.pop(idx)
            if engine.review_current_idx is not None:
                if engine.review_current_idx >= len(engine.review_todo):
                    engine.review_current_idx = max(0, len(engine.review_todo) - 1) if engine.review_todo else None
            engine._notify()
            return jsonify({'success': True, 'remaining': len(engine.review_todo)})
        return jsonify({'success': False})

    @app.route('/api/shutdown', methods=['POST'])
    def api_shutdown():
        """Shutdown the server when the display page is closed."""
        engine.stop()
        func = request.environ.get('werkzeug.server.shutdown')
        if func:
            func()
        else:
            # Werkzeug >= 2.1: shutdown via os._exit in a thread
            threading.Timer(0.5, lambda: os._exit(0)).start()
        return jsonify({'success': True})

    return app


# ─────────────────────────────────────────────
# HTML Templates
# ─────────────────────────────────────────────

DISPLAY_HTML = r"""
<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JM-Rec — Display</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700;800&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
:root {
    --bg: #0a0a0f;
    --surface: #12121a;
    --border: #1e1e2e;
    --text: #e2e2ef;
    --dim: #6b6b8a;
    --accent: #4ecdc4;
    --recording: #ff3b5c;
    --countdown: #fbbf24;
    --success: #34d399;
}
body {
    font-family: 'DM Sans', sans-serif;
    background: var(--bg);
    color: var(--text);
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}

/* Header */
.header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 20px 40px;
    border-bottom: 1px solid var(--border);
}
.logo {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.5rem;
    font-weight: 800;
    color: var(--accent);
    letter-spacing: -0.5px;
}
.logo span { color: var(--dim); font-weight: 400; }
.project-info {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.85rem;
    color: var(--dim);
}
.project-info strong { color: var(--text); }

/* Main display area */
.main {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 30px;
    padding: 20px;
}

/* State indicator */
.state-badge {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.9rem;
    text-transform: uppercase;
    letter-spacing: 3px;
    padding: 8px 24px;
    border-radius: 100px;
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--dim);
    transition: all 0.3s;
}
.state-badge.recording {
    background: rgba(255,59,92,0.15);
    border-color: var(--recording);
    color: var(--recording);
    animation: pulse-recording 1s infinite;
}
.state-badge.countdown {
    background: rgba(251,191,36,0.15);
    border-color: var(--countdown);
    color: var(--countdown);
}

@keyframes pulse-recording {
    0%, 100% { box-shadow: 0 0 0 0 rgba(255,59,92,0.4); }
    50% { box-shadow: 0 0 0 12px rgba(255,59,92,0); }
}

/* Note display */
.note-display {
    text-align: center;
}
.note-name {
    font-family: 'JetBrains Mono', monospace;
    font-size: 12rem;
    font-weight: 800;
    line-height: 1;
    color: var(--text);
    transition: color 0.3s;
}
.note-name.recording { color: var(--recording); }
.note-name.countdown { color: var(--countdown); }

.note-filename {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.2rem;
    color: var(--dim);
    margin-top: 10px;
}

/* Countdown overlay */
.countdown-display {
    position: absolute;
    font-family: 'JetBrains Mono', monospace;
    font-size: 20rem;
    font-weight: 800;
    color: var(--countdown);
    opacity: 0.15;
    pointer-events: none;
    transition: all 0.2s;
}

/* VU Meter */
.vu-container {
    width: 80%;
    max-width: 600px;
}
.vu-bar-bg {
    height: 12px;
    background: var(--surface);
    border-radius: 6px;
    overflow: hidden;
    border: 1px solid var(--border);
}
.vu-bar {
    height: 100%;
    background: linear-gradient(90deg, var(--success), var(--accent), var(--countdown), var(--recording));
    border-radius: 6px;
    transition: width 0.05s;
    width: 0%;
}

/* Progress bar */
.progress-container {
    width: 80%;
    max-width: 600px;
}
.progress-bar-bg {
    height: 6px;
    background: var(--surface);
    border-radius: 3px;
    overflow: hidden;
}
.progress-bar {
    height: 100%;
    background: var(--accent);
    border-radius: 3px;
    transition: width 0.3s;
}
.progress-text {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.8rem;
    color: var(--dim);
    text-align: center;
    margin-top: 8px;
}

/* Footer */
.footer {
    padding: 15px 40px;
    border-top: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    color: var(--dim);
}

/* Header buttons */
.header-actions {
    display: flex;
    align-items: center;
    gap: 12px;
}
.header-btn {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    padding: 6px 14px;
    border-radius: 8px;
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--dim);
    cursor: pointer;
    transition: all 0.2s;
}
.header-btn:hover {
    border-color: var(--accent);
    color: var(--accent);
}

/* QR Modal */
.modal-overlay {
    display: none;
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.8);
    z-index: 100;
    align-items: center;
    justify-content: center;
}
.modal-overlay.active { display: flex; }
.modal {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 32px;
    max-width: 90vw;
    max-height: 90vh;
    overflow-y: auto;
    position: relative;
}
.modal-close {
    position: absolute;
    top: 12px;
    right: 16px;
    font-size: 1.5rem;
    color: var(--dim);
    cursor: pointer;
    background: none;
    border: none;
    line-height: 1;
}
.modal-close:hover { color: var(--text); }
.modal-title {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.2rem;
    font-weight: 800;
    color: var(--accent);
    margin-bottom: 20px;
}

/* QR Modal specific */
.qr-modal { text-align: center; }
.qr-modal .qr-img { width: 220px; height: 220px; margin: 16px auto; background: #fff; border-radius: 12px; padding: 12px; }
.qr-modal .qr-url {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1rem;
    color: var(--accent);
    background: var(--bg);
    padding: 10px 20px;
    border-radius: 10px;
    display: inline-block;
    margin-top: 8px;
    border: 1px solid var(--border);
}
.qr-modal .qr-hint {
    font-size: 0.85rem;
    color: var(--dim);
    margin-top: 12px;
}

/* README Modal */
.readme-modal { max-width: 700px; }
.readme-modal h2 {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.95rem;
    color: var(--accent);
    margin-top: 20px;
    margin-bottom: 8px;
    padding-bottom: 4px;
    border-bottom: 1px solid var(--border);
}
.readme-modal p, .readme-modal li {
    font-size: 0.85rem;
    color: var(--text);
    line-height: 1.6;
}
.readme-modal ul {
    padding-left: 20px;
    margin-bottom: 8px;
}
.readme-modal code {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.8rem;
    background: var(--bg);
    padding: 2px 6px;
    border-radius: 4px;
    color: var(--accent);
}
.readme-modal .tip-box {
    background: rgba(78,205,196,0.08);
    border: 1px solid rgba(78,205,196,0.2);
    border-radius: 10px;
    padding: 12px 16px;
    margin: 8px 0;
    font-size: 0.85rem;
}
.readme-modal .warn-box {
    background: rgba(251,191,36,0.08);
    border: 1px solid rgba(251,191,36,0.2);
    border-radius: 10px;
    padding: 12px 16px;
    margin: 8px 0;
    font-size: 0.85rem;
}
.readme-modal table {
    width: 100%;
    border-collapse: collapse;
    margin: 8px 0;
    font-size: 0.8rem;
    font-family: 'JetBrains Mono', monospace;
}
.readme-modal th, .readme-modal td {
    padding: 6px 10px;
    text-align: left;
    border-bottom: 1px solid var(--border);
}
.readme-modal th {
    color: var(--accent);
    font-weight: 700;
}

/* Settings Drawer */
.drawer-overlay {
    display: none;
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.5);
    z-index: 50;
}
.drawer-overlay.active { display: block; }
.drawer {
    position: fixed;
    top: 0; right: -420px; bottom: 0;
    width: 400px;
    max-width: 90vw;
    background: var(--surface);
    border-left: 1px solid var(--border);
    z-index: 51;
    transition: right 0.3s ease;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
}
.drawer.active { right: 0; }
.drawer-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 20px;
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    background: var(--surface);
    z-index: 1;
}
.drawer-header h3 {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1rem;
    font-weight: 800;
    color: var(--accent);
}
.drawer-close {
    font-size: 1.5rem;
    color: var(--dim);
    cursor: pointer;
    background: none;
    border: none;
    line-height: 1;
}
.drawer-close:hover { color: var(--text); }
.drawer-body { padding: 16px 20px; flex: 1; }
.drawer-section {
    margin-bottom: 20px;
}
.drawer-section-title {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 2px;
    color: var(--dim);
    margin-bottom: 10px;
}
.d-form-group { margin-bottom: 10px; }
.d-form-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--dim);
    margin-bottom: 4px;
    display: block;
}
.d-form-input, .d-form-select {
    width: 100%;
    padding: 8px 10px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.8rem;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    outline: none;
}
.d-form-input:focus, .d-form-select:focus { border-color: var(--accent); }
.d-form-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
}
.d-btn {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.8rem;
    font-weight: 700;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 10px;
    background: var(--surface);
    color: var(--text);
    cursor: pointer;
    text-align: center;
    transition: all 0.15s;
    width: 100%;
}
.d-btn:active { transform: scale(0.97); }
.d-btn-primary { background: var(--accent); color: var(--bg); border-color: var(--accent); }
.d-btn-danger { background: rgba(255,59,92,0.15); color: var(--recording); border-color: rgba(255,59,92,0.3); }
.d-controls-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    margin-bottom: 8px;
}
.d-controls-row {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 8px;
}
.d-checkbox-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 4px 0;
}
.d-checkbox-row input[type="checkbox"] {
    accent-color: var(--accent);
    width: 16px;
    height: 16px;
}
.d-checkbox-row label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    color: var(--text);
    flex: 1;
}
.d-checkbox-row .d-mic-name {
    width: 90px;
    padding: 4px 8px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.7rem;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    outline: none;
}
.d-checkbox-row .d-mic-name:focus { border-color: var(--accent); }
.d-preview {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    color: var(--accent);
    background: var(--bg);
    padding: 4px 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    margin-top: 4px;
}
.d-kbd-inputs { display: flex; flex-direction: column; gap: 4px; margin: 6px 0; }
.d-kbd-row { display: flex; gap: 6px; align-items: center; }
.d-kbd-row input { flex: 1; }
.d-kbd-row .d-kbd-num {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.7rem;
    color: var(--dim);
    min-width: 16px;
}
.d-kb-selector {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin: 6px 0;
}
.d-kb-btn {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    padding: 6px 14px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--dim);
    cursor: pointer;
    transition: all 0.15s;
}
.d-kb-btn:hover { border-color: var(--accent); color: var(--accent); }
.d-kb-btn.active { background: var(--accent); color: var(--bg); border-color: var(--accent); }
</style>
</head>
<body>

<div class="header">
    <div class="logo">JM-Rec <span>v3.2</span></div>
    <div class="header-actions">
        <div class="project-info">
            <span id="projectInfo">—</span>
        </div>
        <button class="header-btn" onclick="openModal('qrModal')">QR Remote</button>
        <button class="header-btn" onclick="openModal('reviewModal')">Controle</button>
        <button class="header-btn" onclick="openModal('readmeModal')">? Info</button>
        <button class="header-btn" onclick="toggleDrawer()">Instellingen</button>
    </div>
</div>

<div class="main">
    <div class="state-badge" id="stateBadge">IDLE</div>
    
    <div class="note-display">
        <div class="note-name" id="noteName">—</div>
        <div class="note-filename" id="noteFilename">—</div>
    </div>
    
    <div class="countdown-display" id="countdownDisplay"></div>
    
    <div class="vu-container">
        <div class="vu-bar-bg">
            <div class="vu-bar" id="vuBar"></div>
        </div>
    </div>
    
    <div class="progress-container">
        <div class="progress-bar-bg">
            <div class="progress-bar" id="progressBar"></div>
        </div>
        <div class="progress-text" id="progressText">0 / 0</div>
    </div>
</div>

<div class="footer">
    <span id="settingsInfo">44100Hz / 16-bit / Mono</span>
    <span id="registerInfo">—</span>
</div>

<!-- Settings Drawer -->
<div class="drawer-overlay" id="drawerOverlay" onclick="toggleDrawer()"></div>
<div class="drawer" id="settingsDrawer">
    <div class="drawer-header">
        <h3>Instellingen &amp; Bediening</h3>
        <button class="drawer-close" onclick="toggleDrawer()">&times;</button>
    </div>
    <div class="drawer-body">

        <div class="drawer-section">
            <div class="drawer-section-title">Bediening</div>
            <div class="d-controls-grid">
                <button class="d-btn d-btn-primary" onclick="dApi('/api/record')">&#9654; Opnemen</button>
                <button class="d-btn d-btn-danger" onclick="dApi('/api/stop')">&#9632; Stop</button>
            </div>
            <div class="d-controls-row">
                <button class="d-btn" onclick="dApi('/api/prev')">&#9664; Vorige</button>
                <button class="d-btn" onclick="dApi('/api/redo')">&#8635; Opnieuw</button>
                <button class="d-btn" onclick="dApi('/api/next')">Volgende &#9654;</button>
            </div>
            <div style="margin-top:8px;">
                <button class="d-btn" onclick="dApi('/api/record-single')" style="font-size:0.7rem;">&#9210; Enkele opname (zonder auto-advance)</button>
            </div>
        </div>

        <div class="drawer-section">
            <div class="drawer-section-title">Orgel instellen</div>
            <div class="d-form-group">
                <label class="d-form-label">Orgelnaam</label>
                <input class="d-form-input" id="dOrganName" placeholder="bijv. Sint-Bavokerk">
            </div>
            <div class="d-form-group">
                <label class="d-form-label">Opslaglocatie</label>
                <input class="d-form-input" id="dOutputDir" placeholder="C:\Users\...\JM-Rec">
            </div>
            <div class="d-form-group">
                <label class="d-form-label">Aantal klavieren</label>
                <input class="d-form-input" type="number" id="dKbCount" value="2" min="1" max="5" onchange="dUpdateKbInputs()">
            </div>
            <div class="d-kbd-inputs" id="dKbInputs"></div>
            <div class="d-checkbox-row">
                <input type="checkbox" id="dHasPedal" checked>
                <label for="dHasPedal">Pedaal</label>
            </div>
            <button class="d-btn d-btn-primary" onclick="dSetupOrgan()" style="margin-top:6px;">Orgel instellen</button>
        </div>

        <div class="drawer-section" id="dKbSection" style="display:none;">
            <div class="drawer-section-title">Klavier / Pedaal</div>
            <div class="d-kb-selector" id="dKbSelector"></div>
        </div>

        <div class="drawer-section" id="dRegSection" style="display:none;">
            <div class="drawer-section-title">Register</div>
            <div class="d-form-group">
                <label class="d-form-label">Registernaam</label>
                <input class="d-form-input" id="dRegName" placeholder="bijv. Holpijp 8 voet" oninput="dUpdateRegPreview()">
            </div>
            <div class="d-checkbox-row">
                <input type="checkbox" id="dTremulant" onchange="dUpdateRegPreview()">
                <label for="dTremulant">Tremulant</label>
            </div>
            <div class="d-preview" id="dRegPreview">Mapnaam: —</div>
            <button class="d-btn d-btn-primary" onclick="dNewRegister()" style="margin-top:6px;">Register opnemen</button>
        </div>

        <div class="drawer-section">
            <div style="display:flex;align-items:center;justify-content:space-between;">
                <div class="drawer-section-title" style="margin:0;">Audiobron</div>
                <button class="d-btn" onclick="dLoadDevices()" title="Apparaten verversen" style="padding:2px 8px;font-size:0.7rem;">&#x21bb; Verversen</button>
            </div>
            <select class="d-form-select" id="dInputMode" onchange="dSetInputMode(this.value)" style="margin:8px 0;">
                <option value="mic">Microfoon</option>
                <option value="loopback">Wat je hoort</option>
            </select>
            <div id="dMicSection">
                <div id="dMicList">Laden...</div>
            </div>
            <div id="dLoopbackSection" style="display:none;">
                <div id="dLoopbackList" style="font-size:0.75rem;color:var(--dim);">Laden...</div>
            </div>
        </div>

        <div class="drawer-section">
            <div class="drawer-section-title">Audio</div>
            <div class="d-form-row">
                <div class="d-form-group">
                    <label class="d-form-label">Samplerate</label>
                    <select class="d-form-select" id="dSampleRate">
                        <option value="44100">44100 Hz</option>
                        <option value="48000">48000 Hz</option>
                        <option value="96000">96000 Hz</option>
                    </select>
                </div>
                <div class="d-form-group">
                    <label class="d-form-label">Bitdiepte</label>
                    <select class="d-form-select" id="dBitDepth">
                        <option value="16">16-bit</option>
                        <option value="24">24-bit</option>
                    </select>
                </div>
            </div>
            <div class="d-form-row">
                <div class="d-form-group">
                    <label class="d-form-label">Kanalen</label>
                    <select class="d-form-select" id="dChannels">
                        <option value="1">Mono</option>
                        <option value="2">Stereo</option>
                    </select>
                </div>
                <div class="d-form-group">
                    <label class="d-form-label">Formaat</label>
                    <select class="d-form-select" id="dFormat" onchange="document.getElementById('dBitrateGroup').style.display=this.value==='mp3'?'':'none'">
                        <option value="mp3">MP3</option>
                        <option value="wav">WAV</option>
                        <option value="flac">FLAC</option>
                    </select>
                </div>
                <div class="d-form-group" id="dBitrateGroup">
                    <label class="d-form-label">MP3 Bitrate</label>
                    <select class="d-form-select" id="dBitrate">
                        <option value="128">128 kbps</option>
                        <option value="192">192 kbps</option>
                        <option value="256">256 kbps</option>
                        <option value="320">320 kbps</option>
                    </select>
                </div>
            </div>
            <div class="d-form-row">
                <div class="d-form-group" style="flex:1;">
                    <label class="d-form-label">Volume <span id="dGainVal">100%</span></label>
                    <input type="range" id="dGain" min="0" max="200" value="100" step="5" style="width:100%;accent-color:var(--accent);" oninput="document.getElementById('dGainVal').textContent=this.value+'%'">
                </div>
            </div>
        </div>

        <div class="drawer-section">
            <div class="drawer-section-title">Workflow</div>
            <div class="d-form-row">
                <div class="d-form-group">
                    <label class="d-form-label">Aftellen (sec)</label>
                    <input class="d-form-input" type="number" id="dCountdown" value="5" min="1" max="30">
                </div>
                <div class="d-form-group">
                    <label class="d-form-label">Opnameduur (sec)</label>
                    <input class="d-form-input" type="number" id="dRecordDur" value="5" min="1" max="60">
                </div>
            </div>
            <div class="d-form-row">
                <div class="d-form-group">
                    <label class="d-form-label">Startnoot (MIDI)</label>
                    <div style="display:flex;align-items:center;gap:8px;">
                        <input class="d-form-input" type="number" id="dStartNote" value="36" min="0" max="127" style="flex:1;" oninput="document.getElementById('dStartNoteLabel').textContent=dMidiToName(parseInt(this.value)||0)">
                        <span id="dStartNoteLabel" style="color:var(--accent);min-width:32px;">C2</span>
                    </div>
                </div>
                <div class="d-form-group">
                    <label class="d-form-label">Eindnoot (MIDI)</label>
                    <div style="display:flex;align-items:center;gap:8px;">
                        <input class="d-form-input" type="number" id="dEndNote" value="96" min="0" max="127" style="flex:1;" oninput="document.getElementById('dEndNoteLabel').textContent=dMidiToName(parseInt(this.value)||0)">
                        <span id="dEndNoteLabel" style="color:var(--accent);min-width:32px;">C7</span>
                    </div>
                </div>
            </div>
            <div class="d-form-row">
                <div class="d-form-group" style="flex:1;">
                    <label class="d-form-label" style="display:flex;align-items:center;gap:8px;">
                        <input type="checkbox" id="dBasDiscant" onchange="document.getElementById('dSplitGroup').style.display=this.checked?'':'none'"> Bas/Discant splitsen
                    </label>
                </div>
                <div class="d-form-group" id="dSplitGroup" style="display:none;">
                    <label class="d-form-label">Splitstoets (MIDI)</label>
                    <div style="display:flex;align-items:center;gap:8px;">
                        <input class="d-form-input" type="number" id="dSplitNote" value="60" min="0" max="127" style="flex:1;" oninput="document.getElementById('dSplitNoteLabel').textContent=dMidiToName(parseInt(this.value)||0)">
                        <span id="dSplitNoteLabel" style="color:var(--accent);min-width:32px;">C4</span>
                    </div>
                    <div style="display:flex;gap:16px;margin-top:6px;">
                        <label class="d-form-label" style="display:flex;align-items:center;gap:4px;">
                            <input type="checkbox" id="dSplitBas" checked> Bas opnemen
                        </label>
                        <label class="d-form-label" style="display:flex;align-items:center;gap:4px;">
                            <input type="checkbox" id="dSplitDisc" checked> Discant opnemen
                        </label>
                    </div>
                </div>
            </div>
            <button class="d-btn d-btn-primary" onclick="dApplySettings()" style="margin-top:8px;">Instellingen toepassen</button>
        </div>


    </div>
</div>

<!-- QR Code Modal -->
<div class="modal-overlay" id="qrModal" onclick="if(event.target===this)closeModal('qrModal')">
    <div class="modal qr-modal">
        <button class="modal-close" onclick="closeModal('qrModal')">&times;</button>
        <div class="modal-title">Remote Control</div>
        <p style="color:var(--dim);font-size:0.85rem;">Scan de QR-code met je telefoon<br>om de afstandsbediening te openen</p>
        <div class="qr-img">
            <img id="qrImage" src="/api/qr.svg" alt="QR Code" style="width:100%;height:100%;">
        </div>
        <div class="qr-url" id="qrUrl">Laden...</div>
        <div class="qr-hint">Zorg dat je telefoon op hetzelfde netwerk zit als deze PC</div>
    </div>
</div>

<!-- Review Modal -->
<div class="modal-overlay" id="reviewModal" onclick="if(event.target===this)closeModal('reviewModal')">
    <div class="modal" style="max-width:650px;max-height:80vh;overflow-y:auto;">
        <button class="modal-close" onclick="closeModal('reviewModal')">&times;</button>
        <div class="modal-title">Sample Controle</div>
        <div style="margin-bottom:8px;">
            <label class="d-form-label">Map</label>
            <input class="d-form-input" id="dRevPath" placeholder="Pad naar register-, klavier- of orgelmap" style="font-size:0.7rem;">
        </div>
        <div style="display:flex;gap:4px;margin-bottom:8px;">
            <button class="d-btn" id="dRevRegister" onclick="dSetReviewScope('register')" style="flex:1;">Register</button>
            <button class="d-btn" id="dRevKeyboard" onclick="dSetReviewScope('keyboard')" style="flex:1;">Klavier</button>
            <button class="d-btn" id="dRevOrgan" onclick="dSetReviewScope('organ')" style="flex:1;">Orgel</button>
            <button class="d-btn" id="dRevCustom" onclick="dSetReviewScope('custom')" style="flex:1;border-color:var(--accent);color:var(--accent);">Map</button>
        </div>
        <label style="display:flex;align-items:center;gap:6px;font-size:0.75rem;color:var(--dim);margin-bottom:8px;">
            <input type="checkbox" id="dRevTrim" checked> Stilte knippen
        </label>
        <button class="d-btn d-btn-primary" id="dRevStart" onclick="dStartReview()" style="width:100%;">Analyseren</button>
        <button class="d-btn" id="dRevStop" onclick="dApi('/api/review-stop')" style="width:100%;display:none;color:var(--recording);">Annuleren</button>
        <div id="dRevProgress" style="display:none;margin-top:12px;">
            <div style="background:var(--surface);border-radius:4px;height:8px;overflow:hidden;">
                <div id="dRevBar" style="height:100%;background:var(--accent);width:0%;transition:width 0.3s;"></div>
            </div>
            <div id="dRevPct" style="text-align:center;font-size:0.75rem;color:var(--dim);margin-top:4px;">0%</div>
        </div>
        <div id="dRevResults" style="display:none;margin-top:12px;">
            <div id="dRevSummary" style="font-size:0.8rem;color:var(--dim);margin-bottom:8px;"></div>
            <div id="dRevList" style="max-height:350px;overflow-y:auto;"></div>
        </div>
        <div id="dRevRerecord" style="display:none;margin-top:12px;padding-top:8px;border-top:1px solid var(--border);">
            <div style="font-size:0.8rem;color:var(--dim);margin-bottom:6px;">Her-opname:</div>
            <div style="display:flex;gap:4px;">
                <button class="d-btn" onclick="dRevPrev()" style="flex:1;">Vorige</button>
                <button class="d-btn d-btn-primary" onclick="dApi('/api/record-single')" style="flex:1;">Opnemen</button>
                <button class="d-btn" onclick="dRevMarkDone()" style="flex:1;color:var(--success);">Klaar</button>
                <button class="d-btn" onclick="dRevNext()" style="flex:1;">Volgende</button>
            </div>
        </div>
    </div>
</div>

<!-- README Modal -->
<div class="modal-overlay" id="readmeModal" onclick="if(event.target===this)closeModal('readmeModal')">
    <div class="modal readme-modal">
        <button class="modal-close" onclick="closeModal('readmeModal')">&times;</button>
        <div class="modal-title">JM-Rec — Handleiding</div>

        <h2>Snelstart</h2>
        <ul>
            <li>Open de <strong>Remote</strong> op je telefoon (scan de QR-code via de knop hierboven)</li>
            <li>Ga naar het <strong>Project</strong>-tabblad en vul projectnaam + registernaam in</li>
            <li>Stel in het <strong>Instellingen</strong>-tabblad de microfoon en het nootbereik in</li>
            <li>Druk op <strong>Opnemen</strong> — de rest gaat automatisch!</li>
        </ul>

        <h2>Bediening</h2>
        <table>
            <tr><th>Knop</th><th>Functie</th></tr>
            <tr><td><code>Opnemen</code></td><td>Start automatische opnamecyclus (alle noten)</td></tr>
            <tr><td><code>Enkele opname</code></td><td>Neemt alleen de huidige noot op</td></tr>
            <tr><td><code>Stop</code></td><td>Stopt direct</td></tr>
            <tr><td><code>Vorige / Volgende</code></td><td>Spring naar andere noot</td></tr>
            <tr><td><code>Opnieuw</code></td><td>Neem de huidige noot opnieuw op</td></tr>
        </table>

        <h2>Opnamecyclus</h2>
        <p>Per noot: <strong>Aftellen</strong> (standaard 5s) &rarr; <strong>Opnemen</strong> (standaard 5s) &rarr; <strong>Volgende noot</strong>. Dit herhaalt zich automatisch tot de laatste noot.</p>

        <h2>Bestandsnamen</h2>
        <p>Bestandsnaamgeving:</p>
        <div class="tip-box">
            <code>036-c.mp3</code>, <code>037-c#.mp3</code>, <code>038-d.mp3</code>, ..., <code>096-c.mp3</code><br>
            Formaat: <code>{MIDI-nummer}-{nootnaam}.mp3</code>
        </div>

        <h2>Mapstructuur</h2>
        <div class="tip-box">
            <code>Opslaglocatie / Orgel / Klavier / Register / 036-c.mp3</code><br>
            Bij multi-mic: <code>... / Register / Positie / 036-c.mp3</code>
        </div>

        <h2>Instelbare parameters</h2>
        <table>
            <tr><th>Parameter</th><th>Standaard</th><th>Opties</th></tr>
            <tr><td>Samplerate</td><td>44100 Hz</td><td>44100 / 48000 / 96000</td></tr>
            <tr><td>Bitdiepte</td><td>16-bit</td><td>16 / 24</td></tr>
            <tr><td>Kanalen</td><td>Mono</td><td>Mono / Stereo</td></tr>
            <tr><td>MP3 Bitrate</td><td>192 kbps</td><td>128 / 192 / 256 / 320</td></tr>
            <tr><td>Afteltijd</td><td>5 sec</td><td>1 &ndash; 30</td></tr>
            <tr><td>Opnameduur</td><td>5 sec</td><td>1 &ndash; 60</td></tr>
            <tr><td>Startnoot</td><td>MIDI 36 (C2)</td><td>0 &ndash; 127</td></tr>
            <tr><td>Eindnoot</td><td>MIDI 96 (C7)</td><td>0 &ndash; 127</td></tr>
        </table>

        <h2>Tips voor opnemen</h2>
        <ul>
            <li>Gebruik een <strong>condensatormicrofoon</strong> voor de beste kwaliteit</li>
            <li>Neem op in <strong>24-bit</strong> voor maximale dynamiek</li>
            <li>Gebruik <strong>Stereo</strong> bij een AB- of ORTF-microfoonopstelling</li>
            <li>Zet de opnameduur lang genoeg voor langzaam sprekende pijpen (<strong>10+ sec</strong> voor 16')</li>
            <li>Houd de <strong>winddruk constant</strong> — wacht tot het orgel stabiel is voor je begint</li>
            <li>Neem op in een <strong>stille omgeving</strong> — vermijd verkeer, wind, en kerkklokken</li>
            <li>Plaats de microfoon op <strong>1-2 meter</strong> van de pijpen voor een natuurlijk geluid</li>
        </ul>

        <h2>Conversie naar WAV</h2>
        <div class="tip-box">
            MP3 naar WAV converteren:<br><br>
            <code>for %f in (*.mp3) do ffmpeg -i "%f" "%~nf.wav"</code>
        </div>

        <h2>Netwerk &amp; Verbinding</h2>
        <div class="warn-box">
            Je telefoon en deze PC moeten op <strong>hetzelfde netwerk</strong> zitten (WiFi).<br>
            Alternatieven: USB-tethering of een mobiele hotspot.
        </div>

        <p style="color:var(--dim);margin-top:20px;font-size:0.8rem;text-align:center;">JM-Rec v3.2</p>
    </div>
</div>

<script>
// Modal functions
function openModal(id) {
    document.getElementById(id).classList.add('active');
    if (id === 'qrModal') loadQrUrl();
}
function closeModal(id) {
    document.getElementById(id).classList.remove('active');
}
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        document.querySelectorAll('.modal-overlay.active').forEach(m => m.classList.remove('active'));
    }
});
async function loadQrUrl() {
    try {
        const res = await fetch('/api/remote-url');
        const data = await res.json();
        document.getElementById('qrUrl').textContent = data.url;
    } catch(e) {}
}

// Drawer functions
function toggleDrawer() {
    document.getElementById('settingsDrawer').classList.toggle('active');
    document.getElementById('drawerOverlay').classList.toggle('active');
}

const _dNoteNames = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
function dMidiToName(midi) { return _dNoteNames[midi % 12] + (Math.floor(midi / 12) - 1); }

async function dApi(url, data) {
    try {
        await fetch(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: data ? JSON.stringify(data) : '{}'
        });
    } catch(e) { console.error(e); }
}

// ── Keyboard inputs ──
function dUpdateKbInputs() {
    const n = parseInt(document.getElementById('dKbCount').value) || 2;
    const c = document.getElementById('dKbInputs');
    const defaults = ['Hoofdwerk','Zwelwerk','Borstwerk','Rugwerk','Bovenwerk'];
    c.innerHTML = '';
    for (let i = 0; i < n; i++) {
        c.innerHTML += '<div class="d-kbd-row"><span class="d-kbd-num">' + (i+1) + '.</span>' +
            '<input class="d-form-input" id="dKb' + i + '" placeholder="Klavier ' + (i+1) + '" value="' + (defaults[i]||'') + '" style="flex:1;">' +
            '<label style="display:flex;align-items:center;gap:3px;font-size:0.65rem;color:var(--dim);white-space:nowrap;">' +
            '<input type="checkbox" id="dKbZw' + i + '"' + (defaults[i] === 'Zwelwerk' ? ' checked' : '') + '> Zwelkast</label></div>';
    }
}
dUpdateKbInputs();

// ── Organ setup ──
async function dSetupOrgan() {
    const n = parseInt(document.getElementById('dKbCount').value) || 2;
    const keyboards = [];
    for (let i = 0; i < n; i++) {
        const v = document.getElementById('dKb' + i).value.trim();
        if (v) keyboards.push({ name: v, zwelwerk: document.getElementById('dKbZw' + i).checked });
    }
    const data = {
        organ: document.getElementById('dOrganName').value,
        keyboards: keyboards,
        has_pedal: document.getElementById('dHasPedal').checked,
        output_dir: document.getElementById('dOutputDir').value || undefined
    };
    await dApi('/api/setup-organ', data);
}

// ── Keyboard selector ──
function dBuildKbSelector(keyboards, hasPedal, current) {
    const c = document.getElementById('dKbSelector');
    const sec = document.getElementById('dKbSection');
    const all = keyboards.map(kb => typeof kb === 'string' ? {name: kb, zwelwerk: false} : kb);
    if (hasPedal) all.push({name: 'Pedaal', zwelwerk: false});
    if (all.length === 0) { sec.style.display = 'none'; return; }
    sec.style.display = '';
    c.innerHTML = '';
    all.forEach(kb => {
        const cls = kb.name === current ? 'd-kb-btn active' : 'd-kb-btn';
        const zw = kb.zwelwerk ? ' <span style="font-size:0.6rem;opacity:0.5;">ZW</span>' : '';
        c.innerHTML += '<button class="' + cls + '" onclick="dSelectKb(\'' + kb.name.replace(/'/g,"\\'") + '\')">' + kb.name + zw + '</button>';
    });
    // Show register section when organ is set up
    document.getElementById('dRegSection').style.display = '';
}
async function dSelectKb(kb) {
    await dApi('/api/select-keyboard', { keyboard: kb });
}

// ── Register preview ──
async function dUpdateRegPreview() {
    const name = document.getElementById('dRegName').value;
    const trem = document.getElementById('dTremulant').checked;
    const el = document.getElementById('dRegPreview');
    if (!name.trim()) { el.textContent = 'Mapnaam: —'; return; }
    try {
        const res = await fetch('/api/format-register', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ name: name, tremulant: trem })
        });
        const data = await res.json();
        el.textContent = 'Mapnaam: ' + data.formatted;
    } catch(e) { el.textContent = 'Mapnaam: —'; }
}

async function dNewRegister() {
    const name = document.getElementById('dRegName').value;
    const trem = document.getElementById('dTremulant').checked;
    if (name) await dApi('/api/new-register', { name: name, tremulant: trem });
}

// ── Input mode & device lists ──
let _deviceList = [];
let _loopbackList = [];
let _dInputMode = 'mic';

function dSetInputMode(mode) {
    _dInputMode = mode;
    document.getElementById('dInputMode').value = mode;
    document.getElementById('dMicSection').style.display = mode === 'mic' ? '' : 'none';
    document.getElementById('dLoopbackSection').style.display = mode === 'loopback' ? '' : 'none';
    dApi('/api/settings', { input_mode: mode });
}

async function dLoadDevices() {
    try {
        const res = await fetch('/api/devices');
        _deviceList = await res.json();
        dRenderMicList();
    } catch(e) {}
    try {
        const res = await fetch('/api/loopback-devices');
        _loopbackList = await res.json();
        dRenderLoopbackList();
    } catch(e) {}
}
function dRenderMicList(activeIndices, activeNames) {
    const c = document.getElementById('dMicList');
    if (!_deviceList.length) { c.innerHTML = '<span style="color:var(--dim);font-size:0.75rem;">Geen apparaten gevonden</span>'; return; }
    activeIndices = activeIndices || [];
    activeNames = activeNames || {};
    let html = '';
    _deviceList.forEach(d => {
        const checked = activeIndices.includes(d.index) ? ' checked' : '';
        const posName = activeNames[d.index] || d.safe_name || '';
        html += '<div class="d-checkbox-row">' +
            '<input type="checkbox" id="dMic' + d.index + '" data-idx="' + d.index + '"' + checked + ' onchange="dApplyMics()">' +
            '<label for="dMic' + d.index + '">' + d.name + '</label>' +
            '<input class="d-mic-name" id="dMicN' + d.index + '" placeholder="Positie" value="' + posName + '" onchange="dApplyMics()">' +
            '</div>';
    });
    c.innerHTML = html;
}
function dRenderLoopbackList(activeId) {
    const c = document.getElementById('dLoopbackList');
    if (!_loopbackList.length) { c.innerHTML = '<span style="color:var(--dim);font-size:0.75rem;">Loopback niet beschikbaar (soundcard library ontbreekt)</span>'; return; }
    let html = '<select class="d-form-select" id="dLoopbackSel" onchange="dApplyLoopback()" style="width:100%;">';
    _loopbackList.forEach(d => {
        const sel = (activeId && d.id === activeId) || (!activeId && d.is_default) ? ' selected' : '';
        html += '<option value="' + d.id + '"' + sel + '>' + d.name + (d.is_default ? ' (standaard)' : '') + '</option>';
    });
    html += '</select>';
    c.innerHTML = html;
}
async function dApplyMics() {
    const indices = [];
    const names = {};
    _deviceList.forEach(d => {
        const cb = document.getElementById('dMic' + d.index);
        if (cb && cb.checked) {
            indices.push(d.index);
            const n = document.getElementById('dMicN' + d.index);
            if (n && n.value.trim()) names[d.index] = n.value.trim();
        }
    });
    await dApi('/api/settings', { device_indices: indices, device_names: names });
}
async function dApplyLoopback() {
    const sel = document.getElementById('dLoopbackSel');
    const devId = sel ? sel.value : null;
    await dApi('/api/settings', { input_mode: 'loopback', loopback_device_id: devId });
}

async function dApplySettings() {
    const data = {
        sample_rate: parseInt(document.getElementById('dSampleRate').value),
        bit_depth: parseInt(document.getElementById('dBitDepth').value),
        channels: parseInt(document.getElementById('dChannels').value),
        output_format: document.getElementById('dFormat').value,
        mp3_bitrate: parseInt(document.getElementById('dBitrate').value),
        record_gain: parseInt(document.getElementById('dGain').value) / 100,
        countdown_seconds: parseInt(document.getElementById('dCountdown').value),
        record_seconds: parseInt(document.getElementById('dRecordDur').value),
        start_note: parseInt(document.getElementById('dStartNote').value),
        end_note: parseInt(document.getElementById('dEndNote').value),
        bass_treble_split: document.getElementById('dBasDiscant').checked,
        split_note: parseInt(document.getElementById('dSplitNote').value),
        split_record_bas: document.getElementById('dSplitBas').checked,
        split_record_disc: document.getElementById('dSplitDisc').checked
    };
    await dApi('/api/settings', data);
}

// ── Sync drawer from state ──
let _drawerSynced = false;
let _settingsSyncDone = false;
function syncDrawer(state) {
    if (!_drawerSynced && state.project) {
        document.getElementById('dOrganName').value = state.project;
        document.getElementById('dOutputDir').value = state.output_dir;
        _drawerSynced = true;
    }
    // Keyboard selector (always sync - reflects project state)
    dBuildKbSelector(state.keyboards || [], state.has_pedal || false, state.current_keyboard || '');
    // Settings + devices - only sync once at startup to avoid overwriting user edits
    const s = state.settings;
    if (!_settingsSyncDone) {
        document.getElementById('dSampleRate').value = s.sample_rate;
        document.getElementById('dBitDepth').value = s.bit_depth;
        document.getElementById('dChannels').value = s.channels;
        document.getElementById('dFormat').value = s.output_format || 'mp3';
        document.getElementById('dBitrateGroup').style.display = (s.output_format || 'mp3') === 'mp3' ? '' : 'none';
        document.getElementById('dBitrate').value = s.mp3_bitrate;
        document.getElementById('dGain').value = Math.round((s.record_gain || 1.0) * 100);
        document.getElementById('dGainVal').textContent = Math.round((s.record_gain || 1.0) * 100) + '%';
        document.getElementById('dCountdown').value = s.countdown_seconds;
        document.getElementById('dRecordDur').value = s.record_seconds;
        document.getElementById('dStartNote').value = s.start_note;
        document.getElementById('dStartNoteLabel').textContent = dMidiToName(s.start_note);
        document.getElementById('dEndNote').value = s.end_note;
        document.getElementById('dEndNoteLabel').textContent = dMidiToName(s.end_note);
        document.getElementById('dBasDiscant').checked = s.bass_treble_split || false;
        document.getElementById('dSplitGroup').style.display = s.bass_treble_split ? '' : 'none';
        document.getElementById('dSplitNote').value = s.split_note || 60;
        document.getElementById('dSplitNoteLabel').textContent = dMidiToName(s.split_note || 60);
        document.getElementById('dSplitBas').checked = s.split_record_bas !== false;
        document.getElementById('dSplitDisc').checked = s.split_record_disc !== false;
        if (_deviceList.length) dRenderMicList(s.device_indices || [], s.device_names || {});
        if (_loopbackList.length) dRenderLoopbackList(s.loopback_device_id);
        _settingsSyncDone = true;
    }
    // Input mode - always sync (toggled via buttons, not text fields)
    if (s.input_mode && s.input_mode !== _dInputMode) {
        _dInputMode = s.input_mode;
        document.getElementById('dInputMode').value = s.input_mode;
        document.getElementById('dMicSection').style.display = s.input_mode === 'mic' ? '' : 'none';
        document.getElementById('dLoopbackSection').style.display = s.input_mode === 'loopback' ? '' : 'none';
    }
}

dLoadDevices();

function updateUI(state) {
    // State badge
    const badge = document.getElementById('stateBadge');
    badge.textContent = state.state.toUpperCase();
    badge.className = 'state-badge ' + (state.state === 'recording' ? 'recording' : state.state === 'countdown' ? 'countdown' : '');
    
    // Note
    const noteName = document.getElementById('noteName');
    noteName.textContent = state.note.current_name;
    noteName.className = 'note-name ' + (state.state === 'recording' ? 'recording' : state.state === 'countdown' ? 'countdown' : '');
    
    document.getElementById('noteFilename').textContent = state.note.current_filename;
    
    // Countdown
    const cd = document.getElementById('countdownDisplay');
    if (state.state === 'countdown' && state.countdown > 0) {
        cd.textContent = state.countdown;
        cd.style.opacity = '0.15';
    } else {
        cd.textContent = '';
        cd.style.opacity = '0';
    }
    
    // VU meter (max across all mics)
    let vuLevel = state.level || 0;
    if (state.levels) {
        const vals = Object.values(state.levels);
        if (vals.length) vuLevel = Math.max(...vals);
    }
    document.getElementById('vuBar').style.width = (vuLevel * 100) + '%';
    
    // Progress
    document.getElementById('progressBar').style.width = (state.progress * 100) + '%';
    document.getElementById('progressText').textContent = 
        state.note.done + ' / ' + state.note.total + ' noten';
    
    // Project info: Orgel / Klavier / Register
    const kb = state.current_keyboard || '';
    const reg = state.register || '';
    const trem = state.tremulant ? ' (trem)' : '';
    document.getElementById('projectInfo').innerHTML =
        '<strong>' + (state.project || '—') + '</strong>' +
        (kb ? ' / ' + kb : '') +
        (reg ? ' / ' + reg + trem : '');

    // Settings
    const s = state.settings;
    const micCount = (s.device_indices && s.device_indices.length > 1) ? ' / ' + s.device_indices.length + ' mics' : '';
    document.getElementById('settingsInfo').textContent =
        s.sample_rate + 'Hz / ' + s.bit_depth + '-bit / ' +
        (s.channels === 1 ? 'Mono' : 'Stereo') + ' / ' +
        ((s.output_format || 'mp3') === 'mp3' ? 'MP3 ' + s.mp3_bitrate + 'kbps' : (s.output_format || 'mp3').toUpperCase()) + micCount;

    document.getElementById('registerInfo').textContent = state.output_dir;
}

// ── Review (Controle) ──
let _dRevScope = 'register';
let _dRevResultsLoaded = false;

function dSetReviewScope(scope) {
    _dRevScope = scope;
    ['register','keyboard','organ','custom'].forEach(s => {
        const el = document.getElementById('dRev' + s.charAt(0).toUpperCase() + s.slice(1));
        if (el) {
            el.style.borderColor = s === scope ? 'var(--accent)' : 'var(--border)';
            el.style.color = s === scope ? 'var(--accent)' : 'var(--dim)';
        }
    });
}

async function dStartReview() {
    _dRevResultsLoaded = false;
    const data = { scope: _dRevScope, trim: document.getElementById('dRevTrim').checked };
    const customPath = document.getElementById('dRevPath').value.trim();
    if (_dRevScope === 'custom' && customPath) data.path = customPath;
    await dApi('/api/review-start', data);
}

function dUpdateReview(review) {
    if (!review) return;
    const analyzing = review.state === 'analyzing';
    const done = review.state === 'done';
    document.getElementById('dRevStart').style.display = analyzing ? 'none' : '';
    document.getElementById('dRevStop').style.display = analyzing ? '' : 'none';
    document.getElementById('dRevProgress').style.display = analyzing ? '' : 'none';
    if (analyzing) {
        const pct = Math.round(review.progress * 100);
        document.getElementById('dRevBar').style.width = pct + '%';
        document.getElementById('dRevPct').textContent = pct + '%';
    }
    if (done) {
        document.getElementById('dRevResults').style.display = '';
        document.getElementById('dRevSummary').textContent =
            review.errors + ' fout' + (review.errors !== 1 ? 'en' : '') + ', ' +
            review.warnings + ' waarschuwing' + (review.warnings !== 1 ? 'en' : '');
        if (!_dRevResultsLoaded) { _dRevResultsLoaded = true; dLoadReviewResults(); }
        document.getElementById('dRevRerecord').style.display = review.todo_count > 0 ? '' : 'none';
        if (review.todo_count === 0 && review.total > 0) {
            document.getElementById('dRevSummary').textContent += ' — Alles in orde!';
        }
    } else {
        document.getElementById('dRevResults').style.display = 'none';
        document.getElementById('dRevRerecord').style.display = 'none';
    }
}

async function dLoadReviewResults() {
    try {
        const res = await fetch('/api/review-results');
        const data = await res.json();
        const list = document.getElementById('dRevList');
        if (!data.results || !data.results.length) { list.innerHTML = '<div style="color:var(--dim);font-size:0.75rem;">Geen problemen gevonden</div>'; return; }
        let html = '';
        data.results.forEach((r, i) => {
            const icon = r.severity === 'error' ? '<span style="color:var(--recording);">&#x2716;</span>' : '<span style="color:#f5a623;">&#x26A0;</span>';
            const todoIdx = data.todo.findIndex(t => t.path === r.path && t.issue === r.issue);
            const click = todoIdx >= 0 ? ' onclick="dRevGoto(' + todoIdx + ')" style="cursor:pointer;"' : '';
            html += '<div' + click + ' style="display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--border);font-size:0.75rem;">' +
                icon + ' <span style="color:var(--accent);min-width:32px;">' + r.note + '</span>' +
                '<span style="color:var(--text);flex:1;">' + r.detail + '</span>' +
                '<span style="color:var(--dim);font-size:0.65rem;">' + r.register + '</span></div>';
        });
        list.innerHTML = html;
    } catch(e) {}
}

async function dRevGoto(idx) { await dApi('/api/review-goto', { index: idx }); closeModal('reviewModal'); }
async function dRevNext() { await dApi('/api/review-next'); _dRevResultsLoaded = false; }
async function dRevPrev() {
    const idx = (window._dRevIdx || 0) - 1;
    if (idx >= 0) await dApi('/api/review-goto', { index: idx });
}
async function dRevMarkDone() { await dApi('/api/review-mark-done'); _dRevResultsLoaded = false; }

// Poll state
setInterval(async () => {
    try {
        const res = await fetch('/api/state');
        const state = await res.json();
        updateUI(state);
        syncDrawer(state);
        dUpdateReview(state.review);
    } catch(e) {}
}, 100);

// Heartbeat to keep server alive
setInterval(() => { fetch('/api/heartbeat', {method:'POST'}).catch(()=>{}); }, 5000);

// Shutdown server when display page is closed
window.addEventListener('beforeunload', function() {
    navigator.sendBeacon('/api/shutdown', '{}');
});
</script>
</body>
</html>
"""


REMOTE_HTML = r"""
<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>JM-Rec Remote</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700;800&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
:root {
    --bg: #0a0a0f;
    --surface: #12121a;
    --surface2: #1a1a28;
    --border: #1e1e2e;
    --text: #e2e2ef;
    --dim: #6b6b8a;
    --accent: #4ecdc4;
    --recording: #ff3b5c;
    --countdown: #fbbf24;
    --success: #34d399;
}
body {
    font-family: 'DM Sans', sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    -webkit-tap-highlight-color: transparent;
    padding-bottom: env(safe-area-inset-bottom, 20px);
}

/* Header */
.header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 20px;
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    background: var(--bg);
    z-index: 10;
}
.logo {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.1rem;
    font-weight: 800;
    color: var(--accent);
}
.logo span { color: var(--dim); font-weight: 400; font-size: 0.8rem; }
.connection-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--success);
}
.connection-dot.offline { background: var(--recording); }

/* Sections */
.section {
    padding: 16px 20px;
    border-bottom: 1px solid var(--border);
}
.section-title {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 2px;
    color: var(--dim);
    margin-bottom: 12px;
}

/* Status card */
.status-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 20px;
    text-align: center;
}
.current-note {
    font-family: 'JetBrains Mono', monospace;
    font-size: 4rem;
    font-weight: 800;
    line-height: 1;
    margin: 8px 0;
}
.current-note.recording { color: var(--recording); }
.current-note.countdown { color: var(--countdown); }

.state-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 2px;
    color: var(--dim);
    padding: 4px 12px;
    border-radius: 100px;
    display: inline-block;
    margin-bottom: 8px;
}
.state-label.recording {
    color: var(--recording);
    background: rgba(255,59,92,0.15);
}
.state-label.countdown {
    color: var(--countdown);
    background: rgba(251,191,36,0.15);
}

.filename-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.85rem;
    color: var(--dim);
}

.countdown-big {
    font-family: 'JetBrains Mono', monospace;
    font-size: 2rem;
    font-weight: 800;
    color: var(--countdown);
    margin-top: 4px;
}

/* Progress */
.progress-row {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-top: 12px;
}
.progress-bar-bg {
    flex: 1;
    height: 6px;
    background: var(--bg);
    border-radius: 3px;
    overflow: hidden;
}
.progress-bar {
    height: 100%;
    background: var(--accent);
    border-radius: 3px;
    transition: width 0.3s;
}
.progress-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.7rem;
    color: var(--dim);
    white-space: nowrap;
}

/* VU Meter */
.vu-bar-bg {
    height: 8px;
    background: var(--bg);
    border-radius: 4px;
    overflow: hidden;
    margin-top: 12px;
}
.vu-bar {
    height: 100%;
    background: linear-gradient(90deg, var(--success), var(--accent), var(--countdown), var(--recording));
    border-radius: 4px;
    transition: width 0.05s;
}

/* Control buttons */
.controls {
    display: grid;
    gap: 10px;
}
.controls-row {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 10px;
}
.controls-main {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
    margin-bottom: 10px;
}

.btn {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.85rem;
    font-weight: 700;
    padding: 16px 12px;
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--surface);
    color: var(--text);
    cursor: pointer;
    transition: all 0.15s;
    text-align: center;
    -webkit-user-select: none;
    user-select: none;
}
.btn:active {
    transform: scale(0.96);
    background: var(--surface2);
}
.btn-primary {
    background: var(--accent);
    color: var(--bg);
    border-color: var(--accent);
}
.btn-primary:active {
    background: #3db8b0;
}
.btn-danger {
    background: rgba(255,59,92,0.15);
    color: var(--recording);
    border-color: rgba(255,59,92,0.3);
}
.btn-danger:active {
    background: rgba(255,59,92,0.25);
}
.btn-icon {
    font-size: 1.3rem;
}
.btn-sm {
    padding: 10px 8px;
    font-size: 0.75rem;
}

/* Setup form */
.form-group {
    margin-bottom: 12px;
}
.form-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--dim);
    margin-bottom: 6px;
    display: block;
}
.form-input, .form-select {
    width: 100%;
    padding: 12px 14px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.85rem;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    color: var(--text);
    outline: none;
}
.form-input:focus, .form-select:focus {
    border-color: var(--accent);
}
.form-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
}

/* Tabs */
.tabs {
    display: flex;
    gap: 0;
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 52px;
    background: var(--bg);
    z-index: 9;
}
.tab {
    flex: 1;
    padding: 12px;
    text-align: center;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--dim);
    cursor: pointer;
    border-bottom: 2px solid transparent;
    transition: all 0.2s;
}
.tab.active {
    color: var(--accent);
    border-bottom-color: var(--accent);
}
.tab-content { display: none; }
.tab-content.active { display: block; }
</style>
</head>
<body>

<div class="header">
    <div class="logo">JM-Rec <span>Remote</span></div>
    <div class="connection-dot" id="connectionDot"></div>
</div>

<div class="tabs">
    <div class="tab active" onclick="switchTab('control')">Bediening</div>
    <div class="tab" onclick="switchTab('setup')">Project</div>
    <div class="tab" onclick="switchTab('settings')">Instellingen</div>
    <div class="tab" onclick="switchTab('review')">Controle</div>
</div>

<!-- CONTROL TAB -->
<div class="tab-content active" id="tab-control">
    <div class="section">
        <div class="status-card">
            <div class="state-label" id="rStateLabel">IDLE</div>
            <div class="current-note" id="rNoteName">—</div>
            <div class="filename-label" id="rFilename">—</div>
            <div class="countdown-big" id="rCountdown"></div>
            
            <div class="vu-bar-bg">
                <div class="vu-bar" id="rVuBar"></div>
            </div>
            
            <div class="progress-row">
                <div class="progress-bar-bg">
                    <div class="progress-bar" id="rProgress"></div>
                </div>
                <div class="progress-label" id="rProgressLabel">0/0</div>
            </div>
        </div>
    </div>
    
    <div class="section">
        <div class="section-title">Opname</div>
        <div class="controls">
            <div class="controls-main">
                <button class="btn btn-primary" onclick="apiCall('/api/record')">▶ Opnemen</button>
                <button class="btn btn-danger" onclick="apiCall('/api/stop')">■ Stop</button>
            </div>
            <div class="controls-row">
                <button class="btn" onclick="apiCall('/api/prev')">◀ Vorige</button>
                <button class="btn" onclick="apiCall('/api/redo')">↻ Opnieuw</button>
                <button class="btn" onclick="apiCall('/api/next')">Volgende ▶</button>
            </div>
            <button class="btn btn-sm" onclick="apiCall('/api/record-single')">⏺ Enkele opname (zonder auto-advance)</button>
        </div>
    </div>
</div>

<!-- SETUP TAB -->
<div class="tab-content" id="tab-setup">
    <div class="section">
        <div class="section-title">Orgel instellen</div>
        <div class="form-group">
            <label class="form-label">Orgelnaam</label>
            <input class="form-input" id="fOrganName" placeholder="bijv. Sint-Bavokerk">
        </div>
        <div class="form-group">
            <label class="form-label">Opslaglocatie</label>
            <input class="form-input" id="fOutputDir" placeholder="C:\Users\...\JM-Rec">
        </div>
        <div class="form-group">
            <label class="form-label">Aantal klavieren</label>
            <input class="form-input" type="number" id="fKbCount" value="2" min="1" max="5" onchange="fUpdateKbInputs()">
        </div>
        <div id="fKbInputs" style="display:flex;flex-direction:column;gap:6px;margin:6px 0;"></div>
        <div style="display:flex;align-items:center;gap:8px;margin:6px 0;">
            <input type="checkbox" id="fHasPedal" checked style="accent-color:var(--accent);width:18px;height:18px;">
            <label for="fHasPedal" style="font-family:'JetBrains Mono',monospace;font-size:0.8rem;color:var(--text);">Pedaal</label>
        </div>
        <button class="btn btn-primary" onclick="fSetupOrgan()" style="width:100%;margin-top:8px;">
            Orgel instellen
        </button>
    </div>

    <div class="section" id="fKbSection" style="display:none;">
        <div class="section-title">Klavier / Pedaal selecteren</div>
        <div id="fKbSelector" style="display:flex;flex-wrap:wrap;gap:8px;"></div>
    </div>

    <div class="section" id="fRegSection" style="display:none;">
        <div class="section-title">Register</div>
        <div class="form-group">
            <label class="form-label">Registernaam</label>
            <input class="form-input" id="fRegName" placeholder="bijv. Holpijp 8 voet" oninput="fUpdateRegPreview()">
        </div>
        <div style="display:flex;align-items:center;gap:8px;margin:6px 0;">
            <input type="checkbox" id="fTremulant" onchange="fUpdateRegPreview()" style="accent-color:var(--accent);width:18px;height:18px;">
            <label for="fTremulant" style="font-family:'JetBrains Mono',monospace;font-size:0.8rem;color:var(--text);">Tremulant</label>
        </div>
        <div id="fRegPreview" style="font-family:'JetBrains Mono',monospace;font-size:0.8rem;color:var(--accent);background:var(--surface);padding:6px 12px;border-radius:8px;border:1px solid var(--border);margin:4px 0;">Mapnaam: —</div>
        <button class="btn btn-primary" onclick="fNewRegister()" style="width:100%;margin-top:8px;">
            Register opnemen
        </button>
    </div>
    <div class="section" id="fExportSection">
        <button class="btn btn-primary" onclick="fExportProject()" style="width:100%;">Exporteer project (.jm-rec.json)</button>
        <div id="fExportResult" style="font-size:0.75rem;color:var(--dim);margin-top:4px;"></div>
    </div>
    <div class="section" id="fCouplerSection">
        <div class="section-title">Koppels</div>
        <div id="fCouplerList" style="margin-bottom:8px;"></div>
        <div style="display:flex;gap:6px;align-items:center;">
            <select class="form-select" id="fCplSource" style="flex:1;font-size:0.8rem;"></select>
            <span style="color:var(--dim);font-size:0.8rem;">naar</span>
            <select class="form-select" id="fCplTarget" style="flex:1;font-size:0.8rem;"></select>
            <button class="btn" onclick="fAddCoupler()" style="padding:6px 12px;">+</button>
        </div>
    </div>
</div>

<!-- SETTINGS TAB -->
<div class="tab-content" id="tab-settings">
    <div class="section">
        <div style="display:flex;align-items:center;justify-content:space-between;">
            <div class="section-title" style="margin:0;">Audiobron</div>
            <button class="btn" onclick="fLoadDevices()" style="padding:2px 8px;font-size:0.7rem;">&#x21bb; Verversen</button>
        </div>
        <select class="form-select" id="fInputMode" onchange="fSetInputMode(this.value)" style="margin:8px 0;">
            <option value="mic">Microfoon</option>
            <option value="loopback">Wat je hoort</option>
        </select>
        <div id="fMicSection">
            <div id="fMicList" style="font-size:0.8rem;color:var(--dim);">Laden...</div>
        </div>
        <div id="fLoopbackSection" style="display:none;">
            <div id="fLoopbackList" style="font-size:0.8rem;color:var(--dim);">Laden...</div>
        </div>
    </div>
    <div class="section">
        <div class="section-title">Audio-instellingen</div>
        <div class="form-row">
            <div class="form-group">
                <label class="form-label">Samplerate</label>
                <select class="form-select" id="fSampleRate">
                    <option value="44100">44100 Hz</option>
                    <option value="48000">48000 Hz</option>
                    <option value="96000">96000 Hz</option>
                </select>
            </div>
            <div class="form-group">
                <label class="form-label">Bitdiepte</label>
                <select class="form-select" id="fBitDepth">
                    <option value="16">16-bit</option>
                    <option value="24">24-bit</option>
                </select>
            </div>
        </div>
        <div class="form-row">
            <div class="form-group">
                <label class="form-label">Kanalen</label>
                <select class="form-select" id="fChannels">
                    <option value="1">Mono</option>
                    <option value="2">Stereo</option>
                </select>
            </div>
            <div class="form-group">
                <label class="form-label">Formaat</label>
                <select class="form-select" id="fFormat" onchange="document.getElementById('fBitrateGroup').style.display=this.value==='mp3'?'':'none'">
                    <option value="mp3">MP3</option>
                    <option value="wav">WAV</option>
                    <option value="flac">FLAC</option>
                </select>
            </div>
            <div class="form-group" id="fBitrateGroup">
                <label class="form-label">MP3 Bitrate</label>
                <select class="form-select" id="fBitrate">
                    <option value="128">128 kbps</option>
                    <option value="192">192 kbps</option>
                    <option value="256">256 kbps</option>
                    <option value="320">320 kbps</option>
                </select>
            </div>
            <div class="form-group" style="flex-basis:100%;">
                <label class="form-label">Volume <span id="fGainVal">100%</span></label>
                <input type="range" id="fGain" min="0" max="200" value="100" step="5" style="width:100%;accent-color:var(--accent);" oninput="document.getElementById('fGainVal').textContent=this.value+'%'">
            </div>
        </div>
    </div>

    <div class="section">
        <div class="section-title">Opname-workflow</div>
        <div class="form-row">
            <div class="form-group">
                <label class="form-label">Aftellen (sec)</label>
                <input class="form-input" type="number" id="fCountdown" value="5" min="1" max="30">
            </div>
            <div class="form-group">
                <label class="form-label">Opnameduur (sec)</label>
                <input class="form-input" type="number" id="fRecordDur" value="5" min="1" max="60">
            </div>
        </div>
    </div>
    
    <div class="section">
        <div class="section-title">Nootbereik (MIDI-nummers)</div>
        <div class="form-row">
            <div class="form-group">
                <label class="form-label">Startnoot (MIDI)</label>
                <input class="form-input" type="number" id="fStartNote" value="36" min="0" max="127">
                <div class="form-label" style="margin-top:4px;" id="fStartNoteLabel">C2</div>
            </div>
            <div class="form-group">
                <label class="form-label">Eindnoot (MIDI)</label>
                <input class="form-input" type="number" id="fEndNote" value="96" min="0" max="127">
                <div class="form-label" style="margin-top:4px;" id="fEndNoteLabel">C7</div>
            </div>
        </div>
        <div class="form-row" style="margin-top:12px;">
            <div class="form-group" style="flex:1;">
                <label class="form-label" style="display:flex;align-items:center;gap:8px;">
                    <input type="checkbox" id="fBasDiscant" onchange="document.getElementById('fSplitGroup').style.display=this.checked?'':'none'"> Bas/Discant splitsen
                </label>
            </div>
            <div class="form-group" id="fSplitGroup" style="display:none;">
                <label class="form-label">Splitstoets</label>
                <input class="form-input" type="number" id="fSplitNote" value="60" min="0" max="127">
                <div class="form-label" style="margin-top:4px;" id="fSplitNoteLabel">C4</div>
                <div style="display:flex;gap:16px;margin-top:6px;">
                    <label class="form-label" style="display:flex;align-items:center;gap:4px;">
                        <input type="checkbox" id="fSplitBas" checked> Bas
                    </label>
                    <label class="form-label" style="display:flex;align-items:center;gap:4px;">
                        <input type="checkbox" id="fSplitDisc" checked> Discant
                    </label>
                </div>
            </div>
        </div>
        <button class="btn btn-primary" onclick="applySettings()" style="width:100%;margin-top:12px;">
            Instellingen toepassen
        </button>
    </div>
</div>

<!-- REVIEW TAB -->
<div class="tab-content" id="tab-review">
    <div class="section">
        <div class="section-title">Sample Controle</div>
        <div class="form-group" style="margin-bottom:8px;">
            <label class="form-label">Map</label>
            <input class="form-input" id="fRevPath" placeholder="Pad naar register-, klavier- of orgelmap" style="font-size:0.75rem;">
        </div>
        <div style="display:flex;gap:6px;margin-bottom:8px;">
            <button class="btn" id="fRevRegister" onclick="fSetReviewScope('register')" style="flex:1;">Register</button>
            <button class="btn" id="fRevKeyboard" onclick="fSetReviewScope('keyboard')" style="flex:1;">Klavier</button>
            <button class="btn" id="fRevOrgan" onclick="fSetReviewScope('organ')" style="flex:1;">Orgel</button>
            <button class="btn" id="fRevCustom" onclick="fSetReviewScope('custom')" style="flex:1;border-color:var(--accent);color:var(--accent);">Map</button>
        </div>
        <label style="display:flex;align-items:center;gap:6px;font-size:0.8rem;color:var(--dim);margin-bottom:8px;">
            <input type="checkbox" id="fRevTrim" checked style="accent-color:var(--accent);"> Stilte knippen
        </label>
        <button class="btn btn-primary" id="fRevStart" onclick="fStartReview()" style="width:100%;">Analyseren</button>
        <button class="btn btn-danger" id="fRevStop" onclick="apiCall('/api/review-stop')" style="width:100%;display:none;">Annuleren</button>
    </div>
    <div class="section" id="fRevProgress" style="display:none;">
        <div class="section-title">Voortgang</div>
        <div style="background:var(--surface);border-radius:4px;height:8px;overflow:hidden;">
            <div id="fRevBar" style="height:100%;background:var(--accent);width:0%;transition:width 0.3s;"></div>
        </div>
        <div id="fRevPct" style="text-align:center;font-size:0.8rem;color:var(--dim);margin-top:4px;">0%</div>
    </div>
    <div class="section" id="fRevResults" style="display:none;">
        <div class="section-title">Resultaten</div>
        <div id="fRevSummary" style="font-size:0.85rem;color:var(--dim);margin-bottom:8px;"></div>
        <div id="fRevList" style="max-height:300px;overflow-y:auto;"></div>
    </div>
    <div class="section" id="fRevRerecord" style="display:none;">
        <div class="section-title">Her-opname</div>
        <div id="fRevCurrentItem" style="font-size:0.85rem;color:var(--text);margin-bottom:8px;"></div>
        <div style="display:flex;gap:6px;">
            <button class="btn" onclick="fRevPrev()" style="flex:1;">Vorige</button>
            <button class="btn btn-primary" onclick="apiCall('/api/record-single')" style="flex:1;">Opnemen</button>
            <button class="btn" onclick="fRevMarkDone()" style="flex:1;color:var(--success);">Klaar</button>
            <button class="btn" onclick="fRevNext()" style="flex:1;">Volgende</button>
        </div>
    </div>
</div>

<script>
const NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];

function midiToName(midi) {
    const octave = Math.floor(midi / 12) - 1;
    return NOTE_NAMES[midi % 12] + octave;
}

// Tab switching
function switchTab(name) {
    document.querySelectorAll('.tab').forEach((t, i) => {
        t.classList.toggle('active', ['control','setup','settings','review'][i] === name);
    });
    document.querySelectorAll('.tab-content').forEach(tc => tc.classList.remove('active'));
    document.getElementById('tab-' + name).classList.add('active');
}

// API calls
async function apiCall(url, data) {
    try {
        const res = await fetch(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: data ? JSON.stringify(data) : '{}'
        });
        return await res.json();
    } catch(e) {
        console.error(e);
    }
}

// ── Keyboard inputs ──
const KB_DEFAULTS = ['Hoofdwerk','Zwelwerk','Borstwerk','Rugwerk','Bovenwerk'];
function fUpdateKbInputs() {
    const n = parseInt(document.getElementById('fKbCount').value) || 2;
    const c = document.getElementById('fKbInputs');
    c.innerHTML = '';
    for (let i = 0; i < n; i++) {
        c.innerHTML += '<div style="display:flex;gap:6px;align-items:center;">' +
            '<span style="font-family:JetBrains Mono,monospace;font-size:0.7rem;color:var(--dim);min-width:16px;">' + (i+1) + '.</span>' +
            '<input class="form-input" id="fKb' + i + '" placeholder="Klavier ' + (i+1) + '" value="' + (KB_DEFAULTS[i]||'') + '" style="padding:8px 10px;font-size:0.8rem;flex:1;">' +
            '<label style="display:flex;align-items:center;gap:3px;font-size:0.7rem;color:var(--dim);white-space:nowrap;">' +
            '<input type="checkbox" id="fKbZw' + i + '"' + (KB_DEFAULTS[i] === 'Zwelwerk' ? ' checked' : '') + '> Zwelkast</label></div>';
    }
}
fUpdateKbInputs();

// ── Organ setup ──
async function fSetupOrgan() {
    const n = parseInt(document.getElementById('fKbCount').value) || 2;
    const keyboards = [];
    for (let i = 0; i < n; i++) {
        const v = document.getElementById('fKb' + i).value.trim();
        if (v) keyboards.push({ name: v, zwelwerk: document.getElementById('fKbZw' + i).checked });
    }
    const res = await apiCall('/api/setup-organ', {
        organ: document.getElementById('fOrganName').value,
        keyboards: keyboards,
        has_pedal: document.getElementById('fHasPedal').checked,
        output_dir: document.getElementById('fOutputDir').value || undefined
    });
    if (res && res.success) switchTab('control');
}

// ── Keyboard selector ──
function fBuildKbSelector(keyboards, hasPedal, current) {
    const c = document.getElementById('fKbSelector');
    const sec = document.getElementById('fKbSection');
    const all = (keyboards || []).map(kb => typeof kb === 'string' ? {name: kb, zwelwerk: false} : kb);
    if (hasPedal) all.push({name: 'Pedaal', zwelwerk: false});
    if (all.length === 0) { sec.style.display = 'none'; return; }
    sec.style.display = '';
    c.innerHTML = '';
    all.forEach(kb => {
        const cls = kb.name === current ? 'btn btn-primary' : 'btn';
        const zw = kb.zwelwerk ? ' <span style="font-size:0.6rem;opacity:0.5;">ZW</span>' : '';
        c.innerHTML += '<button class="' + cls + '" style="padding:10px 16px;font-size:0.8rem;" onclick="fSelectKb(\'' + kb.name.replace(/'/g,"\\'") + '\')">' + kb.name + zw + '</button>';
    });
    document.getElementById('fRegSection').style.display = '';
}
async function fSelectKb(kb) {
    await apiCall('/api/select-keyboard', { keyboard: kb });
}

// ── Register preview ──
async function fUpdateRegPreview() {
    const name = document.getElementById('fRegName').value;
    const trem = document.getElementById('fTremulant').checked;
    const el = document.getElementById('fRegPreview');
    if (!name.trim()) { el.textContent = 'Mapnaam: \u2014'; return; }
    try {
        const res = await fetch('/api/format-register', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ name: name, tremulant: trem })
        });
        const data = await res.json();
        el.textContent = 'Mapnaam: ' + data.formatted;
    } catch(e) { el.textContent = 'Mapnaam: \u2014'; }
}

async function fNewRegister() {
    const name = document.getElementById('fRegName').value;
    const trem = document.getElementById('fTremulant').checked;
    if (name) {
        await apiCall('/api/new-register', { name: name, tremulant: trem });
        switchTab('control');
    }
}

// ── Input mode & device lists ──
let _rDeviceList = [];
let _rLoopbackList = [];
let _fInputMode = 'mic';

function fSetInputMode(mode) {
    _fInputMode = mode;
    document.getElementById('fInputMode').value = mode;
    document.getElementById('fMicSection').style.display = mode === 'mic' ? '' : 'none';
    document.getElementById('fLoopbackSection').style.display = mode === 'loopback' ? '' : 'none';
    apiCall('/api/settings', { input_mode: mode });
}

async function fLoadDevices() {
    await loadDevices();
}

async function loadDevices() {
    try {
        const res = await fetch('/api/devices');
        _rDeviceList = await res.json();
        fRenderMicList();
    } catch(e) {}
    try {
        const res = await fetch('/api/loopback-devices');
        _rLoopbackList = await res.json();
        fRenderLoopbackList();
    } catch(e) {}
}
function fRenderMicList(activeIndices, activeNames) {
    const c = document.getElementById('fMicList');
    if (!_rDeviceList.length) { c.innerHTML = '<span style="color:var(--dim);font-size:0.8rem;">Geen apparaten gevonden</span>'; return; }
    activeIndices = activeIndices || [];
    activeNames = activeNames || {};
    let html = '';
    _rDeviceList.forEach(d => {
        const checked = activeIndices.includes(d.index) ? ' checked' : '';
        const posName = activeNames[d.index] || d.safe_name || '';
        html += '<div style="display:flex;align-items:center;gap:8px;margin:4px 0;">' +
            '<input type="checkbox" id="fMic' + d.index + '" data-idx="' + d.index + '"' + checked + ' onchange="fApplyMics()" style="accent-color:var(--accent);width:16px;height:16px;">' +
            '<label for="fMic' + d.index + '" style="font-family:JetBrains Mono,monospace;font-size:0.75rem;color:var(--text);flex:1;">' + d.name + '</label>' +
            '<input id="fMicN' + d.index + '" placeholder="Positie" value="' + posName + '" onchange="fApplyMics()" style="width:80px;padding:4px 8px;font-family:JetBrains Mono,monospace;font-size:0.7rem;background:var(--surface);border:1px solid var(--border);border-radius:6px;color:var(--text);outline:none;">' +
            '</div>';
    });
    c.innerHTML = html;
}
function fRenderLoopbackList(activeId) {
    const c = document.getElementById('fLoopbackList');
    if (!_rLoopbackList.length) { c.innerHTML = '<span style="color:var(--dim);font-size:0.8rem;">Loopback niet beschikbaar (soundcard library ontbreekt)</span>'; return; }
    let html = '<select class="form-select" id="fLoopbackSel" onchange="fApplyLoopback()" style="width:100%;">';
    _rLoopbackList.forEach(d => {
        const sel = (activeId && d.id === activeId) || (!activeId && d.is_default) ? ' selected' : '';
        html += '<option value="' + d.id + '"' + sel + '>' + d.name + (d.is_default ? ' (standaard)' : '') + '</option>';
    });
    html += '</select>';
    c.innerHTML = html;
}
async function fApplyMics() {
    const indices = [];
    const names = {};
    _rDeviceList.forEach(d => {
        const cb = document.getElementById('fMic' + d.index);
        if (cb && cb.checked) {
            indices.push(d.index);
            const n = document.getElementById('fMicN' + d.index);
            if (n && n.value.trim()) names[d.index] = n.value.trim();
        }
    });
    await apiCall('/api/settings', { device_indices: indices, device_names: names });
}
async function fApplyLoopback() {
    const sel = document.getElementById('fLoopbackSel');
    const devId = sel ? sel.value : null;
    await apiCall('/api/settings', { input_mode: 'loopback', loopback_device_id: devId });
}

// Apply settings
async function applySettings() {
    const data = {
        sample_rate: parseInt(document.getElementById('fSampleRate').value),
        bit_depth: parseInt(document.getElementById('fBitDepth').value),
        channels: parseInt(document.getElementById('fChannels').value),
        output_format: document.getElementById('fFormat').value,
        mp3_bitrate: parseInt(document.getElementById('fBitrate').value),
        record_gain: parseInt(document.getElementById('fGain').value) / 100,
        countdown_seconds: parseInt(document.getElementById('fCountdown').value),
        record_seconds: parseInt(document.getElementById('fRecordDur').value),
        start_note: parseInt(document.getElementById('fStartNote').value),
        end_note: parseInt(document.getElementById('fEndNote').value),
        bass_treble_split: document.getElementById('fBasDiscant').checked,
        split_note: parseInt(document.getElementById('fSplitNote').value),
        split_record_bas: document.getElementById('fSplitBas').checked,
        split_record_disc: document.getElementById('fSplitDisc').checked
    };
    await apiCall('/api/settings', data);
}

// Note label updates
document.getElementById('fStartNote').addEventListener('input', function() {
    document.getElementById('fStartNoteLabel').textContent = midiToName(parseInt(this.value) || 36);
});
document.getElementById('fEndNote').addEventListener('input', function() {
    document.getElementById('fEndNoteLabel').textContent = midiToName(parseInt(this.value) || 96);
});
document.getElementById('fSplitNote').addEventListener('input', function() {
    document.getElementById('fSplitNoteLabel').textContent = midiToName(parseInt(this.value) || 60);
});

// Update UI from state
function updateRemote(state) {
    // Connection
    document.getElementById('connectionDot').classList.remove('offline');
    
    // State label
    const label = document.getElementById('rStateLabel');
    label.textContent = {idle:'GEREED', countdown:'AFTELLEN', recording:'OPNAME', paused:'GEPAUZEERD'}[state.state] || state.state.toUpperCase();
    label.className = 'state-label ' + (state.state === 'recording' ? 'recording' : state.state === 'countdown' ? 'countdown' : '');
    
    // Note
    const note = document.getElementById('rNoteName');
    note.textContent = state.note.current_name;
    note.className = 'current-note ' + (state.state === 'recording' ? 'recording' : state.state === 'countdown' ? 'countdown' : '');
    
    // Filename
    document.getElementById('rFilename').textContent = state.note.current_filename;
    
    // Countdown
    const cd = document.getElementById('rCountdown');
    cd.textContent = state.state === 'countdown' && state.countdown > 0 ? state.countdown : '';
    
    // VU (max across all mics)
    let vuLevel = state.level || 0;
    if (state.levels) {
        const vals = Object.values(state.levels);
        if (vals.length) vuLevel = Math.max(...vals);
    }
    document.getElementById('rVuBar').style.width = (vuLevel * 100) + '%';

    // Progress
    document.getElementById('rProgress').style.width = (state.progress * 100) + '%';
    document.getElementById('rProgressLabel').textContent = state.note.done + '/' + state.note.total;

    // Keyboard selector
    fBuildKbSelector(state.keyboards || [], state.has_pedal || false, state.current_keyboard || '');
    fRenderCouplers(state.couplers || [], state.keyboards || []);

    // Sync settings to form (initial load)
    if (!window._settingsSynced && state.project) {
        document.getElementById('fOrganName').value = state.project;
        document.getElementById('fOutputDir').value = state.output_dir;
        const s = state.settings;
        document.getElementById('fSampleRate').value = s.sample_rate;
        document.getElementById('fBitDepth').value = s.bit_depth;
        document.getElementById('fChannels').value = s.channels;
        document.getElementById('fFormat').value = s.output_format || 'mp3';
        document.getElementById('fBitrateGroup').style.display = (s.output_format || 'mp3') === 'mp3' ? '' : 'none';
        document.getElementById('fBitrate').value = s.mp3_bitrate;
        document.getElementById('fGain').value = Math.round((s.record_gain || 1.0) * 100);
        document.getElementById('fGainVal').textContent = Math.round((s.record_gain || 1.0) * 100) + '%';
        document.getElementById('fCountdown').value = s.countdown_seconds;
        document.getElementById('fRecordDur').value = s.record_seconds;
        document.getElementById('fStartNote').value = s.start_note;
        document.getElementById('fStartNoteLabel').textContent = midiToName(s.start_note);
        document.getElementById('fEndNote').value = s.end_note;
        document.getElementById('fEndNoteLabel').textContent = midiToName(s.end_note);
        document.getElementById('fBasDiscant').checked = s.bass_treble_split || false;
        document.getElementById('fSplitGroup').style.display = s.bass_treble_split ? '' : 'none';
        document.getElementById('fSplitNote').value = s.split_note || 60;
        document.getElementById('fSplitNoteLabel').textContent = midiToName(s.split_note || 60);
        document.getElementById('fSplitBas').checked = s.split_record_bas !== false;
        document.getElementById('fSplitDisc').checked = s.split_record_disc !== false;
        window._settingsSynced = true;
    }
    // Input mode & device list sync
    const s2 = state.settings;
    if (s2.input_mode && s2.input_mode !== _fInputMode) {
        _fInputMode = s2.input_mode;
        document.getElementById('fInputMode').value = s2.input_mode;
        document.getElementById('fMicSection').style.display = s2.input_mode === 'mic' ? '' : 'none';
        document.getElementById('fLoopbackSection').style.display = s2.input_mode === 'loopback' ? '' : 'none';
    }
    if (_rDeviceList.length) {
        fRenderMicList(s2.device_indices || [], s2.device_names || {});
    }
    if (_rLoopbackList.length) {
        fRenderLoopbackList(s2.loopback_device_id);
    }
}

// ── Project export ──
async function fExportProject() {
    const res = await apiCall('/api/export-project');
    const el = document.getElementById('fExportResult');
    if (res && res.success) {
        el.textContent = 'Opgeslagen: ' + res.path;
        el.style.color = 'var(--success)';
    } else {
        el.textContent = res ? res.error : 'Export mislukt';
        el.style.color = 'var(--recording)';
    }
}

// ── Couplers ──
async function fAddCoupler() {
    const source = document.getElementById('fCplSource').value;
    const target = document.getElementById('fCplTarget').value;
    if (source && target && source !== target) {
        await apiCall('/api/add-coupler', { source, target });
    }
}
async function fRemoveCoupler(idx) {
    await apiCall('/api/remove-coupler', { index: idx });
}
function fRenderCouplers(couplers, keyboards) {
    const c = document.getElementById('fCouplerList');
    if (!couplers || !couplers.length) { c.innerHTML = '<div style="color:var(--dim);font-size:0.75rem;">Geen koppels</div>'; return; }
    c.innerHTML = couplers.map((cp, i) =>
        '<div style="display:flex;align-items:center;gap:6px;padding:4px 0;font-size:0.8rem;">' +
        '<span style="color:var(--text);">' + cp.source + ' &rarr; ' + cp.target + '</span>' +
        '<button class="btn" onclick="fRemoveCoupler(' + i + ')" style="padding:2px 8px;font-size:0.7rem;margin-left:auto;">&times;</button></div>'
    ).join('');
    // Update selects with available keyboards
    if (keyboards && keyboards.length) {
        const names = keyboards.map(kb => typeof kb === 'string' ? kb : kb.name);
        ['fCplSource','fCplTarget'].forEach(id => {
            const sel = document.getElementById(id);
            const cur = sel.value;
            sel.innerHTML = names.map(n => '<option value="' + n + '">' + n + '</option>').join('');
            if (cur) sel.value = cur;
        });
    }
}

// ── Review (Controle) ──
let _fRevScope = 'register';
let _fRevResultsLoaded = false;

function fSetReviewScope(scope) {
    _fRevScope = scope;
    ['register','keyboard','organ','custom'].forEach(s => {
        const el = document.getElementById('fRev' + s.charAt(0).toUpperCase() + s.slice(1));
        if (el) {
            el.style.borderColor = s === scope ? 'var(--accent)' : 'var(--border)';
            el.style.color = s === scope ? 'var(--accent)' : 'var(--dim)';
        }
    });
}

async function fStartReview() {
    _fRevResultsLoaded = false;
    const data = {
        scope: _fRevScope,
        trim: document.getElementById('fRevTrim').checked
    };
    const customPath = document.getElementById('fRevPath').value.trim();
    if (_fRevScope === 'custom' && customPath) {
        data.path = customPath;
    }
    await apiCall('/api/review-start', data);
}

function fUpdateReview(review) {
    if (!review) return;
    const analyzing = review.state === 'analyzing';
    const done = review.state === 'done';

    document.getElementById('fRevStart').style.display = analyzing ? 'none' : '';
    document.getElementById('fRevStop').style.display = analyzing ? '' : 'none';
    document.getElementById('fRevProgress').style.display = analyzing ? '' : 'none';

    if (analyzing) {
        const pct = Math.round(review.progress * 100);
        document.getElementById('fRevBar').style.width = pct + '%';
        document.getElementById('fRevPct').textContent = pct + '%';
    }

    if (done) {
        document.getElementById('fRevResults').style.display = '';
        document.getElementById('fRevSummary').textContent =
            review.errors + ' fout' + (review.errors !== 1 ? 'en' : '') + ', ' +
            review.warnings + ' waarschuwing' + (review.warnings !== 1 ? 'en' : '') +
            ' (' + review.total + ' samples gecontroleerd)';

        // Load full results once
        if (!_fRevResultsLoaded) {
            _fRevResultsLoaded = true;
            fLoadReviewResults();
        }

        // Show re-record panel if there are todos
        document.getElementById('fRevRerecord').style.display = review.todo_count > 0 ? '' : 'none';
        if (review.todo_count === 0 && review.total > 0) {
            document.getElementById('fRevSummary').textContent += ' — Alles in orde!';
        }
    } else {
        document.getElementById('fRevResults').style.display = 'none';
        document.getElementById('fRevRerecord').style.display = 'none';
    }
}

async function fLoadReviewResults() {
    try {
        const res = await fetch('/api/review-results');
        const data = await res.json();
        const list = document.getElementById('fRevList');
        if (!data.results || !data.results.length) {
            list.innerHTML = '<div style="color:var(--dim);font-size:0.8rem;padding:8px;">Geen problemen gevonden</div>';
            return;
        }
        let html = '';
        data.results.forEach((r, i) => {
            const icon = r.severity === 'error' ? '<span style="color:var(--recording);">&#x2716;</span>' : '<span style="color:#f5a623;">&#x26A0;</span>';
            const todoIdx = data.todo.findIndex(t => t.path === r.path && t.issue === r.issue);
            const clickable = todoIdx >= 0 ? ' onclick="fRevGoto(' + todoIdx + ')" style="cursor:pointer;"' : '';
            html += '<div' + clickable + ' style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);font-size:0.8rem;">' +
                icon + ' <span style="color:var(--accent);min-width:36px;">' + r.note + '</span>' +
                '<span style="color:var(--text);flex:1;">' + r.detail + '</span>' +
                '<span style="color:var(--dim);font-size:0.7rem;">' + r.register + '</span>' +
                '</div>';
        });
        list.innerHTML = html;
    } catch(e) {}
}

async function fRevGoto(idx) {
    await apiCall('/api/review-goto', { index: idx });
    fUpdateRevCurrent(idx);
    switchTab('control');
}

async function fRevNext() {
    const res = await apiCall('/api/review-next');
    if (res && res.success) {
        fUpdateRevCurrent((res.item && res.item.midi) ? undefined : undefined);
        _fRevResultsLoaded = false;
    }
}

async function fRevPrev() {
    const idx = (window._fRevCurrentIdx || 0) - 1;
    if (idx >= 0) {
        await apiCall('/api/review-goto', { index: idx });
        fUpdateRevCurrent(idx);
    }
}

async function fRevMarkDone() {
    await apiCall('/api/review-mark-done');
    _fRevResultsLoaded = false;
}

function fUpdateRevCurrent(idx) {
    window._fRevCurrentIdx = idx;
}

// Poll
let pollFailCount = 0;
setInterval(async () => {
    try {
        const res = await fetch('/api/state');
        const state = await res.json();
        updateRemote(state);
        fUpdateReview(state.review);
        pollFailCount = 0;
    } catch(e) {
        pollFailCount++;
        if (pollFailCount > 3) {
            document.getElementById('connectionDot').classList.add('offline');
        }
    }
}, 150);

// Heartbeat to keep server alive
setInterval(() => { fetch('/api/heartbeat', {method:'POST'}).catch(()=>{}); }, 5000);

// Init
loadDevices();
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────
# Main Entry Point
# ─────────────────────────────────────────────

def main():
    # PyInstaller onefile: freeze_support must be called before anything else
    import multiprocessing
    multiprocessing.freeze_support()

    import argparse

    # Fix console encoding on Windows
    if sys.platform == 'win32':
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    parser = argparse.ArgumentParser(description='JM-Rec — Organ Sample Recorder')
    parser.add_argument('--port', type=int, default=5555, help='Web server port (default: 5555)')
    parser.add_argument('--host', type=str, default='0.0.0.0', help='Host to bind to')
    parser.add_argument('--project', type=str, help='Project name')
    parser.add_argument('--register', type=str, help='Register name')
    parser.add_argument('--output', type=str, help='Output directory')
    args = parser.parse_args()

    # Single-instance check: try to bind the port early
    try:
        _test = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _test.bind(('127.0.0.1', args.port))
        _test.close()
    except OSError:
        # Port already in use — another instance is running
        webbrowser.open(f"http://localhost:{args.port}/display")
        return

    # Create engine
    engine = RecorderEngine()

    # Setup project if provided
    if args.project and args.register:
        engine.setup_project(args.project, args.register, args.output)

    # Create web app
    app = create_web_app(engine)

    # Get local IP
    local_ip = get_local_ip()

    # Track last browser heartbeat for auto-shutdown
    _last_heartbeat = [time.time()]

    @app.route('/api/heartbeat', methods=['POST'])
    def heartbeat():
        _last_heartbeat[0] = time.time()
        return '', 204

    def shutdown_monitor():
        """Shut down Flask when no browser has connected for 30 seconds."""
        while True:
            time.sleep(5)
            if time.time() - _last_heartbeat[0] > 30:
                os._exit(0)

    threading.Thread(target=shutdown_monitor, daemon=True).start()

    # Auto-open browser after short delay
    def open_browser():
        time.sleep(1.5)
        webbrowser.open(f"http://localhost:{args.port}/display")
    threading.Thread(target=open_browser, daemon=True).start()

    # Run Flask (suppress request logging for clean background operation)
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)

    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == '__main__':
    main()
