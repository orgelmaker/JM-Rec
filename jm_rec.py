#!/usr/bin/env python3
"""
JM-Rec — Organ Sample Recorder
Records pipe organ samples with GrandOrgue-compatible naming.
Includes web-based remote control for Android, iOS or Windows.

File naming convention: {MIDI_number}-{note_name}.mp3
Example: 036-c.mp3, 037-c#.mp3, 038-d.mp3, ...

Author: Martijn
"""

import os
import sys
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
from pydub import AudioSegment

try:
    import qrcode
    import qrcode.image.svg
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False

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
    """Convert MIDI number to GrandOrgue filename like 036-c, 037-c#, etc."""
    note = NOTE_NAMES[midi_num % 12]
    return f"{midi_num:03d}-{note}"


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
        self.mp3_bitrate = 192
        self.device_index = None
        
        # Recording workflow settings
        self.countdown_seconds = 5
        self.record_seconds = 5
        
        # Register range (MIDI numbers)
        self.start_note = 36   # C2
        self.end_note = 96     # C7
        self.current_note = 36
        
        # State
        self.state = "idle"  # idle, countdown, recording, paused
        self.countdown_value = 0
        self.recording_data = []
        self.is_running = False
        self.auto_advance = True
        
        # VU meter
        self.current_level = 0.0
        
        # Callbacks
        self.on_state_change = None
        
        # Thread lock
        self.lock = threading.Lock()
    
    def get_devices(self):
        """List available audio input devices."""
        devices = sd.query_devices()
        input_devices = []
        for i, d in enumerate(devices):
            if d['max_input_channels'] > 0:
                input_devices.append({
                    'index': i,
                    'name': d['name'],
                    'channels': d['max_input_channels'],
                    'sample_rate': int(d['default_samplerate'])
                })
        return input_devices
    
    def get_current_register_path(self):
        """Get the full path for current register."""
        return os.path.join(self.output_dir, self.project_name, self.register_name)
    
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
        return path
    
    def start_recording_cycle(self):
        """Start the countdown → record → next cycle."""
        if self.state != "idle" and self.state != "paused":
            return
        
        self.is_running = True
        thread = threading.Thread(target=self._recording_cycle, daemon=True)
        thread.start()
    
    def _recording_cycle(self):
        """Main recording cycle: countdown → record → (auto)advance."""
        while self.is_running and self.current_note <= self.end_note:
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
        """Record audio for the configured duration."""
        frames = int(self.sample_rate * self.record_seconds)
        channels = self.channels
        
        try:
            # Record
            audio_data = sd.rec(
                frames, 
                samplerate=self.sample_rate,
                channels=channels,
                dtype='float32' if self.bit_depth == 24 else 'int16',
                device=self.device_index
            )
            
            # Monitor levels while recording
            start_time = time.time()
            while time.time() - start_time < self.record_seconds:
                if not self.is_running:
                    sd.stop()
                    return
                elapsed = time.time() - start_time
                # Update level meter from recorded data so far
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
            
            # Save as MP3
            self._save_mp3(audio_data)
            self.current_level = 0.0
            
        except Exception as e:
            print(f"Recording error: {e}")
            # Don't stop the entire cycle on a single recording error
            # Just log it and continue to the next note
            self.current_level = 0.0
    
    def _save_mp3(self, audio_data):
        """Save recorded audio as MP3 with GrandOrgue-compatible naming."""
        path = self.get_current_register_path()
        filename = midi_to_filename(self.current_note)
        wav_path = os.path.join(path, filename + ".wav")
        mp3_path = os.path.join(path, filename + ".mp3")
        
        # Save as temporary WAV first
        if self.bit_depth == 24:
            # Convert float32 to int24 via int32
            audio_int = (audio_data * 2147483647).astype(np.int32)
            # Write 24-bit WAV manually
            with wave.open(wav_path, 'w') as wf:
                wf.setnchannels(self.channels)
                wf.setsampwidth(3)  # 24-bit = 3 bytes
                wf.setframerate(self.sample_rate)
                # Convert int32 to 24-bit bytes
                raw_bytes = b''
                for sample in audio_int.flatten():
                    # Take upper 3 bytes of int32
                    b = struct.pack('<i', sample)
                    raw_bytes += b[1:4]
                wf.writeframes(raw_bytes)
        else:
            # 16-bit
            with wave.open(wav_path, 'w') as wf:
                wf.setnchannels(self.channels)
                wf.setsampwidth(2)
                wf.setframerate(self.sample_rate)
                wf.writeframes(audio_data.tobytes())
        
        # Convert to MP3 using lame
        try:
            subprocess.run([
                'lame', '-b', str(self.mp3_bitrate), 
                '--quiet',
                wav_path, mp3_path
            ], check=True)
            # Remove temporary WAV
            os.remove(wav_path)
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Fallback: use pydub
            try:
                audio_seg = AudioSegment.from_wav(wav_path)
                audio_seg.export(mp3_path, format="mp3", bitrate=f"{self.mp3_bitrate}k")
                os.remove(wav_path)
            except Exception as e:
                print(f"MP3 conversion failed, keeping WAV: {e}")
    
    def stop(self):
        """Stop recording cycle."""
        self.is_running = False
        self.state = "idle"
        self.current_level = 0.0
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
    
    def new_register(self, register_name):
        """Start a new register."""
        self.stop()
        self.register_name = register_name
        self.current_note = self.start_note
        path = self.get_current_register_path()
        os.makedirs(path, exist_ok=True)
        self._notify()
        return path
    
    def get_state(self):
        """Get full state for UI/remote."""
        with self.lock:
            return {
                'state': self.state,
                'project': self.project_name,
                'register': self.register_name,
                'output_dir': self.output_dir,
                'countdown': self.countdown_value,
                'note': self.get_notes_info(),
                'progress': self.get_progress(),
                'level': self.current_level,
                'settings': {
                    'sample_rate': self.sample_rate,
                    'bit_depth': self.bit_depth,
                    'channels': self.channels,
                    'mp3_bitrate': self.mp3_bitrate,
                    'countdown_seconds': self.countdown_seconds,
                    'record_seconds': self.record_seconds,
                    'start_note': self.start_note,
                    'end_note': self.end_note,
                    'device_index': self.device_index
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
    
    @app.route('/api/settings', methods=['POST'])
    def api_settings():
        data = request.json
        if 'sample_rate' in data:
            engine.sample_rate = int(data['sample_rate'])
        if 'bit_depth' in data:
            engine.bit_depth = int(data['bit_depth'])
        if 'channels' in data:
            engine.channels = int(data['channels'])
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
            engine.device_index = int(data['device_index']) if data['device_index'] is not None else None
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
            path = engine.new_register(data['name'])
            return jsonify({'success': True, 'path': path})
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
</style>
</head>
<body>

<div class="header">
    <div class="logo">JM-Rec <span>v1.0</span></div>
    <div class="header-actions">
        <div class="project-info">
            <span id="projectInfo">—</span>
        </div>
        <button class="header-btn" onclick="openModal('qrModal')">QR Remote</button>
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
            <div class="drawer-section-title">Project</div>
            <div class="d-form-group">
                <label class="d-form-label">Projectnaam</label>
                <input class="d-form-input" id="dProject" placeholder="bijv. Sint-Bavokerk">
            </div>
            <div class="d-form-group">
                <label class="d-form-label">Registernaam</label>
                <input class="d-form-input" id="dRegister" placeholder="bijv. Prestant_8">
            </div>
            <div class="d-form-group">
                <label class="d-form-label">Opslaglocatie</label>
                <input class="d-form-input" id="dOutputDir" placeholder="C:\Users\...\JM-Rec">
            </div>
            <button class="d-btn d-btn-primary" onclick="dSetupProject()" style="margin-top:4px;">Map aanmaken &amp; instellen</button>
        </div>

        <div class="drawer-section">
            <div class="drawer-section-title">Audio</div>
            <div class="d-form-group">
                <label class="d-form-label">Microfoon</label>
                <select class="d-form-select" id="dDevice">
                    <option value="">Standaard</option>
                </select>
            </div>
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
                    <label class="d-form-label">MP3 Bitrate</label>
                    <select class="d-form-select" id="dBitrate">
                        <option value="128">128 kbps</option>
                        <option value="192">192 kbps</option>
                        <option value="256">256 kbps</option>
                        <option value="320">320 kbps</option>
                    </select>
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
                    <input class="d-form-input" type="number" id="dStartNote" value="36" min="0" max="127">
                </div>
                <div class="d-form-group">
                    <label class="d-form-label">Eindnoot (MIDI)</label>
                    <input class="d-form-input" type="number" id="dEndNote" value="96" min="0" max="127">
                </div>
            </div>
            <button class="d-btn d-btn-primary" onclick="dApplySettings()" style="margin-top:8px;">Instellingen toepassen</button>
        </div>

        <div class="drawer-section">
            <div class="drawer-section-title">Nieuw register</div>
            <div class="d-form-group">
                <label class="d-form-label">Registernaam</label>
                <input class="d-form-input" id="dNewRegister" placeholder="bijv. Fluit_4">
            </div>
            <button class="d-btn" onclick="dNewRegister()">Nieuw register starten</button>
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
        <p>Bestanden volgen de <strong>GrandOrgue/Hauptwerk</strong>-conventie:</p>
        <div class="tip-box">
            <code>036-c.mp3</code>, <code>037-c#.mp3</code>, <code>038-d.mp3</code>, ..., <code>096-c.mp3</code><br>
            Formaat: <code>{MIDI-nummer}-{nootnaam}.mp3</code>
        </div>

        <h2>Mapstructuur</h2>
        <div class="tip-box">
            <code>Opslaglocatie / Projectnaam / Registernaam / 036-c.mp3</code>
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
            De MP3-bestanden kun je later converteren naar WAV voor GrandOrgue:<br><br>
            <code>for %f in (*.mp3) do ffmpeg -i "%f" "%~nf.wav"</code>
        </div>

        <h2>Netwerk &amp; Verbinding</h2>
        <div class="warn-box">
            Je telefoon en deze PC moeten op <strong>hetzelfde netwerk</strong> zitten (WiFi).<br>
            Alternatieven: USB-tethering of een mobiele hotspot.
        </div>

        <p style="color:var(--dim);margin-top:20px;font-size:0.8rem;text-align:center;">JM-Rec v1.0</p>
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

async function dApi(url, data) {
    try {
        await fetch(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: data ? JSON.stringify(data) : '{}'
        });
    } catch(e) { console.error(e); }
}

async function dSetupProject() {
    const data = {
        project: document.getElementById('dProject').value,
        register: document.getElementById('dRegister').value,
        output_dir: document.getElementById('dOutputDir').value || undefined
    };
    await dApi('/api/setup', data);
}

async function dApplySettings() {
    const data = {
        sample_rate: parseInt(document.getElementById('dSampleRate').value),
        bit_depth: parseInt(document.getElementById('dBitDepth').value),
        channels: parseInt(document.getElementById('dChannels').value),
        mp3_bitrate: parseInt(document.getElementById('dBitrate').value),
        countdown_seconds: parseInt(document.getElementById('dCountdown').value),
        record_seconds: parseInt(document.getElementById('dRecordDur').value),
        start_note: parseInt(document.getElementById('dStartNote').value),
        end_note: parseInt(document.getElementById('dEndNote').value),
        device_index: document.getElementById('dDevice').value || null
    };
    await dApi('/api/settings', data);
}

async function dNewRegister() {
    const name = document.getElementById('dNewRegister').value;
    if (name) await dApi('/api/new-register', { name });
}

async function dLoadDevices() {
    try {
        const res = await fetch('/api/devices');
        const devices = await res.json();
        const sel = document.getElementById('dDevice');
        sel.innerHTML = '<option value="">Standaard</option>';
        devices.forEach(d => {
            sel.innerHTML += '<option value="' + d.index + '">' + d.name + ' (' + d.channels + 'ch)</option>';
        });
    } catch(e) {}
}

// Sync drawer form fields from state
let _drawerSynced = false;
function syncDrawer(state) {
    if (!_drawerSynced && state.project) {
        document.getElementById('dProject').value = state.project;
        document.getElementById('dRegister').value = state.register;
        document.getElementById('dOutputDir').value = state.output_dir;
        _drawerSynced = true;
    }
    // Always sync current settings values (in case changed from remote)
    const s = state.settings;
    document.getElementById('dSampleRate').value = s.sample_rate;
    document.getElementById('dBitDepth').value = s.bit_depth;
    document.getElementById('dChannels').value = s.channels;
    document.getElementById('dBitrate').value = s.mp3_bitrate;
    document.getElementById('dCountdown').value = s.countdown_seconds;
    document.getElementById('dRecordDur').value = s.record_seconds;
    document.getElementById('dStartNote').value = s.start_note;
    document.getElementById('dEndNote').value = s.end_note;
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
    
    // VU meter
    document.getElementById('vuBar').style.width = (state.level * 100) + '%';
    
    // Progress
    document.getElementById('progressBar').style.width = (state.progress * 100) + '%';
    document.getElementById('progressText').textContent = 
        state.note.done + ' / ' + state.note.total + ' noten';
    
    // Project info
    document.getElementById('projectInfo').innerHTML = 
        '<strong>' + (state.project || '—') + '</strong> / ' + (state.register || '—');
    
    // Settings
    const s = state.settings;
    document.getElementById('settingsInfo').textContent = 
        s.sample_rate + 'Hz / ' + s.bit_depth + '-bit / ' + 
        (s.channels === 1 ? 'Mono' : 'Stereo') + ' / MP3 ' + s.mp3_bitrate + 'kbps';
    
    document.getElementById('registerInfo').textContent = state.output_dir;
}

// Poll state
setInterval(async () => {
    try {
        const res = await fetch('/api/state');
        const state = await res.json();
        updateUI(state);
        syncDrawer(state);
    } catch(e) {}
}, 100);

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
        <div class="section-title">Project instellen</div>
        <div class="form-group">
            <label class="form-label">Projectnaam (hoofdmap)</label>
            <input class="form-input" id="fProject" placeholder="bijv. Sint-Bavokerk">
        </div>
        <div class="form-group">
            <label class="form-label">Registernaam (submap)</label>
            <input class="form-input" id="fRegister" placeholder="bijv. Prestant_8">
        </div>
        <div class="form-group">
            <label class="form-label">Opslaglocatie</label>
            <input class="form-input" id="fOutputDir" placeholder="C:\Users\...\JM-Rec">
        </div>
        <button class="btn btn-primary" onclick="setupProject()" style="width:100%;margin-top:8px;">
            Map aanmaken & instellen
        </button>
    </div>
    
    <div class="section">
        <div class="section-title">Nieuw register starten</div>
        <div class="form-group">
            <label class="form-label">Nieuwe registernaam</label>
            <input class="form-input" id="fNewRegister" placeholder="bijv. Fluit_4">
        </div>
        <button class="btn" onclick="newRegister()" style="width:100%;">
            Nieuw register starten
        </button>
    </div>
</div>

<!-- SETTINGS TAB -->
<div class="tab-content" id="tab-settings">
    <div class="section">
        <div class="section-title">Audio-instellingen</div>
        <div class="form-group">
            <label class="form-label">Microfoon</label>
            <select class="form-select" id="fDevice">
                <option value="">Laden...</option>
            </select>
        </div>
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
                <label class="form-label">MP3 Bitrate</label>
                <select class="form-select" id="fBitrate">
                    <option value="128">128 kbps</option>
                    <option value="192">192 kbps</option>
                    <option value="256">256 kbps</option>
                    <option value="320">320 kbps</option>
                </select>
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
        <button class="btn btn-primary" onclick="applySettings()" style="width:100%;margin-top:12px;">
            Instellingen toepassen
        </button>
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
        t.classList.toggle('active', ['control','setup','settings'][i] === name);
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

// Setup project
async function setupProject() {
    const data = {
        project: document.getElementById('fProject').value,
        register: document.getElementById('fRegister').value,
        output_dir: document.getElementById('fOutputDir').value || undefined
    };
    const res = await apiCall('/api/setup', data);
    if (res && res.success) {
        switchTab('control');
    }
}

// New register
async function newRegister() {
    const name = document.getElementById('fNewRegister').value;
    if (name) {
        await apiCall('/api/new-register', { name });
        switchTab('control');
    }
}

// Apply settings
async function applySettings() {
    const data = {
        sample_rate: parseInt(document.getElementById('fSampleRate').value),
        bit_depth: parseInt(document.getElementById('fBitDepth').value),
        channels: parseInt(document.getElementById('fChannels').value),
        mp3_bitrate: parseInt(document.getElementById('fBitrate').value),
        countdown_seconds: parseInt(document.getElementById('fCountdown').value),
        record_seconds: parseInt(document.getElementById('fRecordDur').value),
        start_note: parseInt(document.getElementById('fStartNote').value),
        end_note: parseInt(document.getElementById('fEndNote').value),
        device_index: document.getElementById('fDevice').value || null
    };
    await apiCall('/api/settings', data);
}

// Load devices
async function loadDevices() {
    try {
        const res = await fetch('/api/devices');
        const devices = await res.json();
        const sel = document.getElementById('fDevice');
        sel.innerHTML = '<option value="">Standaard</option>';
        devices.forEach(d => {
            sel.innerHTML += `<option value="${d.index}">${d.name} (${d.channels}ch)</option>`;
        });
    } catch(e) {}
}

// Note label updates
document.getElementById('fStartNote').addEventListener('input', function() {
    document.getElementById('fStartNoteLabel').textContent = midiToName(parseInt(this.value) || 36);
});
document.getElementById('fEndNote').addEventListener('input', function() {
    document.getElementById('fEndNoteLabel').textContent = midiToName(parseInt(this.value) || 96);
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
    
    // VU
    document.getElementById('rVuBar').style.width = (state.level * 100) + '%';
    
    // Progress
    document.getElementById('rProgress').style.width = (state.progress * 100) + '%';
    document.getElementById('rProgressLabel').textContent = state.note.done + '/' + state.note.total;
    
    // Sync settings to form (initial load)
    if (!window._settingsSynced && state.project) {
        document.getElementById('fProject').value = state.project;
        document.getElementById('fRegister').value = state.register;
        document.getElementById('fOutputDir').value = state.output_dir;
        document.getElementById('fSampleRate').value = state.settings.sample_rate;
        document.getElementById('fBitDepth').value = state.settings.bit_depth;
        document.getElementById('fChannels').value = state.settings.channels;
        document.getElementById('fBitrate').value = state.settings.mp3_bitrate;
        document.getElementById('fCountdown').value = state.settings.countdown_seconds;
        document.getElementById('fRecordDur').value = state.settings.record_seconds;
        document.getElementById('fStartNote').value = state.settings.start_note;
        document.getElementById('fEndNote').value = state.settings.end_note;
        window._settingsSynced = true;
    }
}

// Poll
let pollFailCount = 0;
setInterval(async () => {
    try {
        const res = await fetch('/api/state');
        const state = await res.json();
        updateRemote(state);
        pollFailCount = 0;
    } catch(e) {
        pollFailCount++;
        if (pollFailCount > 3) {
            document.getElementById('connectionDot').classList.add('offline');
        }
    }
}, 150);

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
    
    # Create engine
    engine = RecorderEngine()
    
    # Setup project if provided
    if args.project and args.register:
        engine.setup_project(args.project, args.register, args.output)
    
    # Create web app
    app = create_web_app(engine)
    
    # Get local IP
    local_ip = get_local_ip()
    
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
