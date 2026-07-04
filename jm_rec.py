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


def normalize_output_dir(path):
    """Make a user-entered output directory safe for os.path.join.

    A bare Windows drive letter like 'D:' is drive-RELATIVE, so
    os.path.join('D:', 'Orgel') yields 'D:Orgel' (invalid, WinError 123).
    Append a separator so it becomes an absolute root ('D:\\').
    Also strips stray surrounding quotes/whitespace.
    """
    if not path:
        return path
    p = str(path).strip().strip('"').strip("'").strip()
    if re.match(r"^[A-Za-z]:$", p):
        p += os.sep
    return p


# Characters Windows forbids in a file/folder name (drive colon excluded
# because components are joined under a separate drive root).
_ILLEGAL_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_path_component(name):
    """Strip characters that are illegal in a Windows file/folder name.

    A single path component (organ, keyboard, register or mic position name)
    containing e.g. ':' '\"' '?' '*' raises WinError 123 the moment a path is
    built from it. Keeps spaces and other legal characters intact, and removes
    trailing dots/spaces (also forbidden by Windows).
    """
    if not name:
        return name
    cleaned = _ILLEGAL_PATH_CHARS.sub('', str(name))
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    cleaned = cleaned.rstrip('. ')
    return cleaned


JM_REC_VERSION = "3.7"


# ─────────────────────────────────────────────
# i18n — served as /i18n.js to both DISPLAY and REMOTE pages.
# Dutch is the source in the HTML; the dictionary maps NL → EN/DE/FR and a
# DOM walker swaps the text at load. Anything missing degrades to Dutch.
# ─────────────────────────────────────────────
I18N_JS = r'''
(function(){
  const I18N = {
  "en": {
    // header / nav
    "Registers":"Registers","Controle":"Review","? Info":"? Help","Nieuw orgel":"New organ","Instellingen":"Settings","QR Remote":"QR Remote",
    "Bediening":"Control","Project":"Project",
    // startup
    "Welkom":"Welcome","Doorgaan met dit orgel":"Continue with this organ","Nieuw orgel instellen":"Set up a new organ",
    // wizard chrome
    "Orgel instellen":"Set up organ","Stap":"Step","Vorige":"Back","Volgende":"Next","Opslaan & starten":"Save & start","Vorige →":"Back","Volgende →":"Next →","← Vorige":"← Back",
    // step 1
    "1. Opslaglocatie":"1. Storage location","Map waarin alle opnames worden bewaard.":"Folder where all recordings are stored.","Opslagmap":"Storage folder","Bladeren…":"Browse…","bijv. D:\\Opnames":"e.g. D:\\Recordings",
    // step 2
    "2. Microfoon(s)":"2. Microphone(s)","Kies de opnamebron. Bij meerdere microfoons krijgt elke een eigen positienaam (submap).":"Choose the input source. With multiple microphones each gets its own position name (subfolder).","Bron":"Source","Microfoon(s)":"Microphone(s)","Wat je hoort (loopback)":"What you hear (loopback)","positie":"position","Geen microfoons gevonden.":"No microphones found.","Kon apparaten niet laden.":"Could not load devices.","Speaker (loopback)":"Speaker (loopback)","Geen loopback-apparaten.":"No loopback devices.",
    // step 3-5
    "3. Plaatsnaam":"3. Town","Plaats waar het orgel staat.":"Town where the organ is located.","bijv. Puttershoek":"e.g. Puttershoek","Mapcode wordt:":"Folder code:",
    "4. Kerknaam":"4. Church name","Naam van de kerk/gebouw (wordt als info bewaard).":"Name of the church/building (stored as info).","bijv. Hervormde Kerk":"e.g. St. Mary's Church",
    "5. Orgelbouwer":"5. Organ builder","De bouwer van het orgel.":"The builder of the organ.","bijv. Muller":"e.g. Muller","Mapnaam (4 letters plaats + 4 letters bouwer, aanpasbaar)":"Folder name (4 letters town + 4 letters builder, editable)",
    // step 6-7
    "6. Klavieren en pedaal":"6. Manuals and pedal","Hoeveel klavieren (manualen) heeft het orgel?":"How many manuals does the organ have?","Aantal klavieren":"Number of manuals","Pedaal aanwezig":"Pedal present",
    "7. Naam per klavier":"7. Name per manual","Geef elk klavier een naam.":"Give each manual a name.","Klavier":"Manual",
    // step 8
    "8. Tremulant en zwelkast":"8. Tremulant and swell box","Bij een tremulant wordt elk register 2x opgenomen (normaal én _trem).":"With a tremulant each register is recorded twice (normal and _trem).","Tremulant":"Tremulant","Geen tremulant":"No tremulant","Heel het orgel":"Whole organ","Per klavier":"Per manual","Zwelkast (zwelwerk) per klavier":"Swell box per manual","Tremulant op":"Tremulant on","Zwelkast op":"Swell box on",
    // step 9
    "9. Registers per klavier":"9. Registers per manual","Naam":"Name","Voet":"Foot","Begin":"Begin","Eind":"End","Bas/disc":"Bass/Treble","+ Register":"+ Register","Prestant":"Principal",
    // step 10
    "10. Opname-instellingen en koppels":"10. Recording settings and couplers","Samplerate":"Sample rate","Bitdiepte":"Bit depth","Kanalen":"Channels","Formaat":"Format","Aftellen (sec)":"Countdown (sec)","Opnameduur (sec)":"Record duration (sec)","Splitstoets bas/disc (MIDI)":"Bass/treble split note (MIDI)","Koppels":"Couplers","+ Koppel":"+ Coupler","Mono":"Mono","Stereo":"Stereo",
    // intelligent (assistive) recording
    "Opnamemodus":"Recording mode","Vaste duur":"Fixed duration","Intelligent (assisterend)":"Intelligent (assistive)","Neemt automatisch op tot er genoeg stabiele, loopbare toon is en seint dan dat je kunt loslaten. Alleen microfooningang.":"Records automatically until there is enough stable, loopable tone, then signals that you can release. Microphone input only.","Min. stabiele toon (sec)":"Min. stable tone (sec)","Max. duur (sec)":"Max. duration (sec)","Gevoeligheid":"Sensitivity","Gevoeligheid (0.3 streng – 3 los)":"Sensitivity (0.3 strict – 3 loose)","Wachten op toon…":"Waiting for tone…","Stabiliseren…":"Stabilizing…","Genoeg — laat los":"Enough — release","Uitklank opnemen…":"Capturing release tail…",
    // main controls + legend
    "Opnemen":"Record","Pauze":"Pause","Stop":"Stop","Vorige noot":"Previous note","Opnieuw":"Redo","Volgende noot":"Next note","Opname":"Recording","Enkele opname (zonder auto-advance)":"Single recording (no auto-advance)",
    "nog op te nemen":"to be recorded","niet compleet":"incomplete","nog te controleren":"to be reviewed","nog controleren":"to review","goed":"approved","controleren":"review",
    "Register kiezen":"Select register","Geen registers":"No registers","Geen registers gedefinieerd. Stel het orgel in op de master-PC.":"No registers defined. Set up the organ on the master PC.","Geen registers gedefinieerd op de master-PC.":"No registers defined on the master PC.",
    // check prompt
    "Register compleet":"Register complete","Nu controleren":"Review now","Goedgekeurd":"Approved","Later":"Later","Controleren":"Review",
    // register modal
    "Registerbeheer":"Register manager","Klavier":"Manual","Nieuw register":"New register","Nog geen registers.":"No registers yet.","normaal":"normal","tremulant":"tremulant","gecontroleerd":"reviewed","beginnoot":"begin note","eindnoot":"end note","bas/disc":"bass/treble","naam (bijv. Prestant)":"name (e.g. Principal)",
    // state labels
    "GEREED":"READY","AFTELLEN":"COUNTDOWN","OPNAME":"RECORDING","GEPAUZEERD":"PAUSED","IDLE":"IDLE",
    // drawer
    "Instellingen & Bediening":"Settings & Control","Audiobron":"Audio source","Verversen":"Refresh","Microfoon":"Microphone","Wat je hoort":"What you hear","Audio":"Audio","Volume":"Volume","Workflow":"Workflow","Startnoot (MIDI)":"Start note (MIDI)","Eindnoot (MIDI)":"End note (MIDI)","Bas/Discant splitsen":"Bass/treble split","Splitstoets (MIDI)":"Split note (MIDI)","Bas opnemen":"Record bass","Discant opnemen":"Record treble","Orgelnaam":"Organ name","Opslaglocatie":"Storage location","Pedaal":"Pedal","Toepassen":"Apply","Instellingen toepassen":"Apply settings",
    "Exporteren":"Export","Exporteer .organ (JM-Orgue)":"Export .organ (JM-Orgue)","Exporteer projectgegevens (.json)":"Export project data (.json)","Maakt een .organ-definitiebestand in de projectmap dat JM-Orgue direct kan laden.":"Creates a .organ definition file in the project folder that JM-Orgue can load directly.",".organ opgeslagen:":".organ saved:","registers":"stops","samples":"samples","ontbrekend":"missing","Export mislukt:":"Export failed:","Project-JSON opgeslagen:":"Project JSON saved:","Let op: JM-Orgue ondersteunt geen FLAC; kies WAV of MP3 als formaat":"Note: JM-Orgue does not support FLAC; choose WAV or MP3 as format",
    // QR / hotspot
    "Remote Control":"Remote Control","Zorg dat je telefoon op hetzelfde netwerk zit als deze PC.":"Make sure your phone is on the same network as this PC.","Kies het netwerk waarmee je telefoon verbonden is:":"Choose the network your phone is connected to:","Directe verbinding (geen WiFi nodig)":"Direct connection (no WiFi needed)","Geen WiFi op deze locatie? Laat deze PC zelf een netwerk uitzenden en verbind je telefoon of tablet daarmee.":"No WiFi at this location? Let this PC broadcast its own network and connect your phone or tablet to it.","Open hotspot-instellingen":"Open hotspot settings",
    // review modal
    "Sample Controle":"Sample Review","Analyseren":"Analyze","Annuleren":"Cancel","Map":"Folder","Orgel":"Organ","Stilte knippen":"Trim silence","Klaar":"Done",
    // handleiding
    "JM-Rec — Handleiding":"JM-Rec — Manual","Snelstart":"Quick start","Kleurcodes per register":"Colour codes per register","Kleur":"Colour","Betekenis":"Meaning","Knop":"Button","Functie":"Function",
    "Register":"Register","op":"on","is volledig opgenomen":"is fully recorded","Wil je het nu controleren?":"Review it now?","is compleet":"is complete","Controleren?":"Review?",
    "Klavier / Pedaal":"Manual / Pedal","Register opnemen":"Record register","Apparaten verversen":"Refresh devices","Laden...":"Loading...","MP3 Bitrate":"MP3 bitrate","Mapnaam:":"Folder name:","Mapnaam: —":"Folder name: —","Scan de QR-code met je telefoon of tablet":"Scan the QR code with your phone or tablet","om de afstandsbediening te openen":"to open the remote control","Internet is niet nodig — de bediening werkt ook zonder. Houd dit venster open; schakelt de hotspot uit, zet hem opnieuw aan.":"Internet is not required — control works without it. Keep this window open; if the hotspot turns off, switch it on again.","Klik op 'Open hotspot-instellingen' en zet de Mobiele hotspot AAN.":"Click 'Open hotspot settings' and turn the Mobile hotspot ON.","Noteer de netwerknaam en het wachtwoord die Windows toont.":"Note the network name and password that Windows shows.","Verbind je iPad/iPhone of Android met dat netwerk.":"Connect your iPad/iPhone or Android to that network.","Kies hierboven het hotspot-netwerk en scan de QR-code (of typ het adres).":"Select the hotspot network above and scan the QR code (or type the address).","Pad naar register-, klavier- of orgelmap":"Path to register, manual or organ folder","Her-opname:":"Re-record:","Registers toevoegen/verwijderen per klavier. C-groot = MIDI 36.":"Add/remove registers per manual. Bottom C = MIDI 36.","Register toevoegen":"Add register"
  },
  "de": {
    "Registers":"Register","Controle":"Kontrolle","? Info":"? Hilfe","Nieuw orgel":"Neue Orgel","Instellingen":"Einstellungen","QR Remote":"QR Remote","Bediening":"Steuerung","Project":"Projekt",
    "Welkom":"Willkommen","Doorgaan met dit orgel":"Mit dieser Orgel fortfahren","Nieuw orgel instellen":"Neue Orgel einrichten",
    "Orgel instellen":"Orgel einrichten","Stap":"Schritt","Vorige":"Zurück","Volgende":"Weiter","Opslaan & starten":"Speichern & starten","Vorige →":"Zurück","Volgende →":"Weiter →","← Vorige":"← Zurück",
    "1. Opslaglocatie":"1. Speicherort","Map waarin alle opnames worden bewaard.":"Ordner, in dem alle Aufnahmen gespeichert werden.","Opslagmap":"Speicherordner","Bladeren…":"Durchsuchen…","bijv. D:\\Opnames":"z. B. D:\\Aufnahmen",
    "2. Microfoon(s)":"2. Mikrofon(e)","Kies de opnamebron. Bij meerdere microfoons krijgt elke een eigen positienaam (submap).":"Wählen Sie die Aufnahmequelle. Bei mehreren Mikrofonen erhält jedes einen eigenen Positionsnamen (Unterordner).","Bron":"Quelle","Microfoon(s)":"Mikrofon(e)","Wat je hoort (loopback)":"Was du hörst (Loopback)","positie":"Position","Geen microfoons gevonden.":"Keine Mikrofone gefunden.","Kon apparaten niet laden.":"Geräte konnten nicht geladen werden.","Speaker (loopback)":"Lautsprecher (Loopback)","Geen loopback-apparaten.":"Keine Loopback-Geräte.",
    "3. Plaatsnaam":"3. Ort","Plaats waar het orgel staat.":"Ort, an dem die Orgel steht.","bijv. Puttershoek":"z. B. Puttershoek","Mapcode wordt:":"Ordnercode:",
    "4. Kerknaam":"4. Kirchenname","Naam van de kerk/gebouw (wordt als info bewaard).":"Name der Kirche/des Gebäudes (wird als Info gespeichert).","bijv. Hervormde Kerk":"z. B. Marienkirche",
    "5. Orgelbouwer":"5. Orgelbauer","De bouwer van het orgel.":"Der Erbauer der Orgel.","bijv. Muller":"z. B. Müller","Mapnaam (4 letters plaats + 4 letters bouwer, aanpasbaar)":"Ordnername (4 Buchstaben Ort + 4 Buchstaben Erbauer, anpassbar)",
    "6. Klavieren en pedaal":"6. Manuale und Pedal","Hoeveel klavieren (manualen) heeft het orgel?":"Wie viele Manuale hat die Orgel?","Aantal klavieren":"Anzahl Manuale","Pedaal aanwezig":"Pedal vorhanden",
    "7. Naam per klavier":"7. Name pro Manual","Geef elk klavier een naam.":"Geben Sie jedem Manual einen Namen.","Klavier":"Manual",
    "8. Tremulant en zwelkast":"8. Tremulant und Schwellkasten","Bij een tremulant wordt elk register 2x opgenomen (normaal én _trem).":"Bei einem Tremulant wird jedes Register zweimal aufgenommen (normal und _trem).","Tremulant":"Tremulant","Geen tremulant":"Kein Tremulant","Heel het orgel":"Ganze Orgel","Per klavier":"Pro Manual","Zwelkast (zwelwerk) per klavier":"Schwellkasten pro Manual","Tremulant op":"Tremulant für","Zwelkast op":"Schwellkasten für",
    "9. Registers per klavier":"9. Register pro Manual","Naam":"Name","Voet":"Fuß","Begin":"Anfang","Eind":"Ende","Bas/disc":"Bass/Diskant","+ Register":"+ Register","Prestant":"Prinzipal",
    "10. Opname-instellingen en koppels":"10. Aufnahme-Einstellungen und Koppeln","Samplerate":"Abtastrate","Bitdiepte":"Bittiefe","Kanalen":"Kanäle","Formaat":"Format","Aftellen (sec)":"Countdown (Sek.)","Opnameduur (sec)":"Aufnahmedauer (Sek.)","Splitstoets bas/disc (MIDI)":"Trennton Bass/Diskant (MIDI)","Koppels":"Koppeln","+ Koppel":"+ Koppel","Mono":"Mono","Stereo":"Stereo",
    "Opnamemodus":"Aufnahmemodus","Vaste duur":"Feste Dauer","Intelligent (assisterend)":"Intelligent (assistierend)","Neemt automatisch op tot er genoeg stabiele, loopbare toon is en seint dan dat je kunt loslaten. Alleen microfooningang.":"Nimmt automatisch auf, bis genug stabiler, loop-fähiger Ton vorhanden ist, und signalisiert dann, dass Sie loslassen können. Nur Mikrofoneingang.","Min. stabiele toon (sec)":"Min. stabiler Ton (Sek.)","Max. duur (sec)":"Max. Dauer (Sek.)","Gevoeligheid":"Empfindlichkeit","Gevoeligheid (0.3 streng – 3 los)":"Empfindlichkeit (0.3 streng – 3 locker)","Wachten op toon…":"Warte auf Ton…","Stabiliseren…":"Stabilisiert…","Genoeg — laat los":"Genug — loslassen","Uitklank opnemen…":"Ausklang aufnehmen…",
    "Opnemen":"Aufnehmen","Pauze":"Pause","Stop":"Stopp","Vorige noot":"Vorherige Note","Opnieuw":"Wiederholen","Volgende noot":"Nächste Note","Opname":"Aufnahme","Enkele opname (zonder auto-advance)":"Einzelaufnahme (ohne Auto-Vorlauf)",
    "nog op te nemen":"noch aufzunehmen","niet compleet":"unvollständig","nog te controleren":"noch zu prüfen","nog controleren":"prüfen","goed":"freigegeben","controleren":"prüfen",
    "Register kiezen":"Register wählen","Geen registers":"Keine Register","Geen registers gedefinieerd. Stel het orgel in op de master-PC.":"Keine Register definiert. Richten Sie die Orgel am Master-PC ein.","Geen registers gedefinieerd op de master-PC.":"Keine Register am Master-PC definiert.",
    "Register compleet":"Register vollständig","Nu controleren":"Jetzt prüfen","Goedgekeurd":"Freigegeben","Later":"Später","Controleren":"Prüfen",
    "Registerbeheer":"Registerverwaltung","Klavier":"Manual","Nieuw register":"Neues Register","Nog geen registers.":"Noch keine Register.","normaal":"normal","tremulant":"Tremulant","gecontroleerd":"geprüft","beginnoot":"Anfangsnote","eindnoot":"Endnote","bas/disc":"Bass/Diskant","naam (bijv. Prestant)":"Name (z. B. Prinzipal)",
    "GEREED":"BEREIT","AFTELLEN":"COUNTDOWN","OPNAME":"AUFNAHME","GEPAUZEERD":"PAUSIERT","IDLE":"BEREIT",
    "Instellingen & Bediening":"Einstellungen & Steuerung","Audiobron":"Audioquelle","Verversen":"Aktualisieren","Microfoon":"Mikrofon","Wat je hoort":"Was du hörst","Audio":"Audio","Volume":"Lautstärke","Workflow":"Ablauf","Startnoot (MIDI)":"Startnote (MIDI)","Eindnoot (MIDI)":"Endnote (MIDI)","Bas/Discant splitsen":"Bass/Diskant trennen","Splitstoets (MIDI)":"Trennton (MIDI)","Bas opnemen":"Bass aufnehmen","Discant opnemen":"Diskant aufnehmen","Orgelnaam":"Orgelname","Opslaglocatie":"Speicherort","Pedaal":"Pedal","Toepassen":"Anwenden","Instellingen toepassen":"Einstellungen anwenden",
    "Exporteren":"Exportieren","Exporteer .organ (JM-Orgue)":".organ exportieren (JM-Orgue)","Exporteer projectgegevens (.json)":"Projektdaten exportieren (.json)","Maakt een .organ-definitiebestand in de projectmap dat JM-Orgue direct kan laden.":"Erstellt eine .organ-Definitionsdatei im Projektordner, die JM-Orgue direkt laden kann.",".organ opgeslagen:":".organ gespeichert:","registers":"Register","samples":"Samples","ontbrekend":"fehlend","Export mislukt:":"Export fehlgeschlagen:","Project-JSON opgeslagen:":"Projekt-JSON gespeichert:","Let op: JM-Orgue ondersteunt geen FLAC; kies WAV of MP3 als formaat":"Hinweis: JM-Orgue unterstützt kein FLAC; wählen Sie WAV oder MP3 als Format",
    "Remote Control":"Fernsteuerung","Zorg dat je telefoon op hetzelfde netwerk zit als deze PC.":"Stellen Sie sicher, dass Ihr Telefon im selben Netzwerk wie dieser PC ist.","Kies het netwerk waarmee je telefoon verbonden is:":"Wählen Sie das Netzwerk, mit dem Ihr Telefon verbunden ist:","Directe verbinding (geen WiFi nodig)":"Direktverbindung (kein WLAN nötig)","Geen WiFi op deze locatie? Laat deze PC zelf een netwerk uitzenden en verbind je telefoon of tablet daarmee.":"Kein WLAN vor Ort? Lassen Sie diesen PC ein eigenes Netzwerk aussenden und verbinden Sie Ihr Telefon oder Tablet damit.","Open hotspot-instellingen":"Hotspot-Einstellungen öffnen",
    "Sample Controle":"Sample-Kontrolle","Analyseren":"Analysieren","Annuleren":"Abbrechen","Map":"Ordner","Orgel":"Orgel","Stilte knippen":"Stille beschneiden","Klaar":"Fertig",
    "JM-Rec — Handleiding":"JM-Rec — Handbuch","Snelstart":"Schnellstart","Kleurcodes per register":"Farbcodes pro Register","Kleur":"Farbe","Betekenis":"Bedeutung","Knop":"Taste","Functie":"Funktion",
    "Register":"Register","op":"auf","is volledig opgenomen":"ist vollständig aufgenommen","Wil je het nu controleren?":"Möchten Sie es jetzt prüfen?","is compleet":"ist vollständig","Controleren?":"Prüfen?",
    "Klavier / Pedaal":"Manual / Pedal","Register opnemen":"Register aufnehmen","Apparaten verversen":"Geräte aktualisieren","Laden...":"Lädt...","MP3 Bitrate":"MP3-Bitrate","Mapnaam:":"Ordnername:","Mapnaam: —":"Ordnername: —","Scan de QR-code met je telefoon of tablet":"Scannen Sie den QR-Code mit Ihrem Telefon oder Tablet","om de afstandsbediening te openen":"um die Fernsteuerung zu öffnen","Internet is niet nodig — de bediening werkt ook zonder. Houd dit venster open; schakelt de hotspot uit, zet hem opnieuw aan.":"Internet ist nicht nötig — die Steuerung funktioniert auch ohne. Lassen Sie dieses Fenster offen; schaltet sich der Hotspot ab, schalten Sie ihn erneut ein.","Klik op 'Open hotspot-instellingen' en zet de Mobiele hotspot AAN.":"Klicken Sie auf 'Hotspot-Einstellungen öffnen' und schalten Sie den Mobilen Hotspot EIN.","Noteer de netwerknaam en het wachtwoord die Windows toont.":"Notieren Sie den Netzwerknamen und das Passwort, die Windows anzeigt.","Verbind je iPad/iPhone of Android met dat netwerk.":"Verbinden Sie Ihr iPad/iPhone oder Android mit diesem Netzwerk.","Kies hierboven het hotspot-netwerk en scan de QR-code (of typ het adres).":"Wählen Sie oben das Hotspot-Netzwerk und scannen Sie den QR-Code (oder tippen Sie die Adresse).","Pad naar register-, klavier- of orgelmap":"Pfad zum Register-, Manual- oder Orgelordner","Her-opname:":"Neuaufnahme:","Registers toevoegen/verwijderen per klavier. C-groot = MIDI 36.":"Register pro Manual hinzufügen/entfernen. Großes C = MIDI 36.","Register toevoegen":"Register hinzufügen"
  },
  "fr": {
    "Registers":"Jeux","Controle":"Contrôle","? Info":"? Aide","Nieuw orgel":"Nouvel orgue","Instellingen":"Réglages","QR Remote":"QR Remote","Bediening":"Commande","Project":"Projet",
    "Welkom":"Bienvenue","Doorgaan met dit orgel":"Continuer avec cet orgue","Nieuw orgel instellen":"Configurer un nouvel orgue",
    "Orgel instellen":"Configurer l'orgue","Stap":"Étape","Vorige":"Précédent","Volgende":"Suivant","Opslaan & starten":"Enregistrer & démarrer","Vorige →":"Précédent","Volgende →":"Suivant →","← Vorige":"← Précédent",
    "1. Opslaglocatie":"1. Emplacement de stockage","Map waarin alle opnames worden bewaard.":"Dossier où tous les enregistrements sont conservés.","Opslagmap":"Dossier de stockage","Bladeren…":"Parcourir…","bijv. D:\\Opnames":"p. ex. D:\\Enregistrements",
    "2. Microfoon(s)":"2. Microphone(s)","Kies de opnamebron. Bij meerdere microfoons krijgt elke een eigen positienaam (submap).":"Choisissez la source. Avec plusieurs micros, chacun reçoit un nom de position (sous-dossier).","Bron":"Source","Microfoon(s)":"Microphone(s)","Wat je hoort (loopback)":"Ce que vous entendez (loopback)","positie":"position","Geen microfoons gevonden.":"Aucun microphone trouvé.","Kon apparaten niet laden.":"Impossible de charger les appareils.","Speaker (loopback)":"Haut-parleur (loopback)","Geen loopback-apparaten.":"Aucun appareil loopback.",
    "3. Plaatsnaam":"3. Localité","Plaats waar het orgel staat.":"Localité où se trouve l'orgue.","bijv. Puttershoek":"p. ex. Puttershoek","Mapcode wordt:":"Code du dossier :",
    "4. Kerknaam":"4. Nom de l'église","Naam van de kerk/gebouw (wordt als info bewaard).":"Nom de l'église/du bâtiment (conservé comme info).","bijv. Hervormde Kerk":"p. ex. église Saint-Pierre",
    "5. Orgelbouwer":"5. Facteur d'orgues","De bouwer van het orgel.":"Le facteur de l'orgue.","bijv. Muller":"p. ex. Muller","Mapnaam (4 letters plaats + 4 letters bouwer, aanpasbaar)":"Nom du dossier (4 lettres localité + 4 lettres facteur, modifiable)",
    "6. Klavieren en pedaal":"6. Claviers et pédalier","Hoeveel klavieren (manualen) heeft het orgel?":"Combien de claviers possède l'orgue ?","Aantal klavieren":"Nombre de claviers","Pedaal aanwezig":"Pédalier présent",
    "7. Naam per klavier":"7. Nom par clavier","Geef elk klavier een naam.":"Donnez un nom à chaque clavier.","Klavier":"Clavier",
    "8. Tremulant en zwelkast":"8. Tremblant et boîte expressive","Bij een tremulant wordt elk register 2x opgenomen (normaal én _trem).":"Avec un tremblant, chaque jeu est enregistré deux fois (normal et _trem).","Tremulant":"Tremblant","Geen tremulant":"Pas de tremblant","Heel het orgel":"Tout l'orgue","Per klavier":"Par clavier","Zwelkast (zwelwerk) per klavier":"Boîte expressive par clavier","Tremulant op":"Tremblant sur","Zwelkast op":"Boîte expressive sur",
    "9. Registers per klavier":"9. Jeux par clavier","Naam":"Nom","Voet":"Pieds","Begin":"Début","Eind":"Fin","Bas/disc":"Basse/Dessus","+ Register":"+ Jeu","Prestant":"Montre",
    "10. Opname-instellingen en koppels":"10. Réglages d'enregistrement et accouplements","Samplerate":"Fréquence","Bitdiepte":"Profondeur","Kanalen":"Canaux","Formaat":"Format","Aftellen (sec)":"Compte à rebours (s)","Opnameduur (sec)":"Durée d'enregistrement (s)","Splitstoets bas/disc (MIDI)":"Note de coupure basse/dessus (MIDI)","Koppels":"Accouplements","+ Koppel":"+ Accouplement","Mono":"Mono","Stereo":"Stéréo",
    "Opnamemodus":"Mode d'enregistrement","Vaste duur":"Durée fixe","Intelligent (assisterend)":"Intelligent (assisté)","Neemt automatisch op tot er genoeg stabiele, loopbare toon is en seint dan dat je kunt loslaten. Alleen microfooningang.":"Enregistre automatiquement jusqu'à obtenir un son stable et bouclable, puis signale que vous pouvez relâcher. Entrée microphone uniquement.","Min. stabiele toon (sec)":"Son stable min. (s)","Max. duur (sec)":"Durée max. (s)","Gevoeligheid":"Sensibilité","Gevoeligheid (0.3 streng – 3 los)":"Sensibilité (0.3 strict – 3 souple)","Wachten op toon…":"En attente du son…","Stabiliseren…":"Stabilisation…","Genoeg — laat los":"Assez — relâchez","Uitklank opnemen…":"Capture de la résonance…",
    "Opnemen":"Enregistrer","Pauze":"Pause","Stop":"Arrêt","Vorige noot":"Note précédente","Opnieuw":"Refaire","Volgende noot":"Note suivante","Opname":"Enregistrement","Enkele opname (zonder auto-advance)":"Enregistrement simple (sans avance auto)",
    "nog op te nemen":"à enregistrer","niet compleet":"incomplet","nog te controleren":"à contrôler","nog controleren":"à contrôler","goed":"validé","controleren":"contrôler",
    "Register kiezen":"Choisir un jeu","Geen registers":"Aucun jeu","Geen registers gedefinieerd. Stel het orgel in op de master-PC.":"Aucun jeu défini. Configurez l'orgue sur le PC maître.","Geen registers gedefinieerd op de master-PC.":"Aucun jeu défini sur le PC maître.",
    "Register compleet":"Jeu complet","Nu controleren":"Contrôler maintenant","Goedgekeurd":"Validé","Later":"Plus tard","Controleren":"Contrôler",
    "Registerbeheer":"Gestion des jeux","Klavier":"Clavier","Nieuw register":"Nouveau jeu","Nog geen registers.":"Aucun jeu pour l'instant.","normaal":"normal","tremulant":"tremblant","gecontroleerd":"contrôlé","beginnoot":"note de début","eindnoot":"note de fin","bas/disc":"basse/dessus","naam (bijv. Prestant)":"nom (p. ex. Montre)",
    "GEREED":"PRÊT","AFTELLEN":"COMPTE À REBOURS","OPNAME":"ENREGISTREMENT","GEPAUZEERD":"EN PAUSE","IDLE":"PRÊT",
    "Instellingen & Bediening":"Réglages & Commande","Audiobron":"Source audio","Verversen":"Actualiser","Microfoon":"Microphone","Wat je hoort":"Ce que vous entendez","Audio":"Audio","Volume":"Volume","Workflow":"Déroulement","Startnoot (MIDI)":"Note de départ (MIDI)","Eindnoot (MIDI)":"Note de fin (MIDI)","Bas/Discant splitsen":"Séparer basse/dessus","Splitstoets (MIDI)":"Note de coupure (MIDI)","Bas opnemen":"Enregistrer la basse","Discant opnemen":"Enregistrer le dessus","Orgelnaam":"Nom de l'orgue","Opslaglocatie":"Emplacement de stockage","Pedaal":"Pédalier","Toepassen":"Appliquer","Instellingen toepassen":"Appliquer les réglages",
    "Exporteren":"Exporter","Exporteer .organ (JM-Orgue)":"Exporter .organ (JM-Orgue)","Exporteer projectgegevens (.json)":"Exporter les données du projet (.json)","Maakt een .organ-definitiebestand in de projectmap dat JM-Orgue direct kan laden.":"Crée un fichier de définition .organ dans le dossier du projet, directement chargeable par JM-Orgue.",".organ opgeslagen:":".organ enregistré :","registers":"jeux","samples":"échantillons","ontbrekend":"manquant","Export mislukt:":"Échec de l'export :","Project-JSON opgeslagen:":"JSON du projet enregistré :","Let op: JM-Orgue ondersteunt geen FLAC; kies WAV of MP3 als formaat":"Attention : JM-Orgue ne prend pas en charge le FLAC ; choisissez WAV ou MP3",
    "Remote Control":"Télécommande","Zorg dat je telefoon op hetzelfde netwerk zit als deze PC.":"Assurez-vous que votre téléphone est sur le même réseau que ce PC.","Kies het netwerk waarmee je telefoon verbonden is:":"Choisissez le réseau auquel votre téléphone est connecté :","Directe verbinding (geen WiFi nodig)":"Connexion directe (pas de WiFi requis)","Geen WiFi op deze locatie? Laat deze PC zelf een netwerk uitzenden en verbind je telefoon of tablet daarmee.":"Pas de WiFi sur place ? Laissez ce PC diffuser son propre réseau et connectez-y votre téléphone ou tablette.","Open hotspot-instellingen":"Ouvrir les réglages du point d'accès",
    "Sample Controle":"Contrôle des samples","Analyseren":"Analyser","Annuleren":"Annuler","Map":"Dossier","Orgel":"Orgue","Stilte knippen":"Couper le silence","Klaar":"Terminé",
    "JM-Rec — Handleiding":"JM-Rec — Manuel","Snelstart":"Démarrage rapide","Kleurcodes per register":"Codes couleur par jeu","Kleur":"Couleur","Betekenis":"Signification","Knop":"Bouton","Functie":"Fonction",
    "Register":"Jeu","op":"sur","is volledig opgenomen":"est entièrement enregistré","Wil je het nu controleren?":"Le contrôler maintenant ?","is compleet":"est complet","Controleren?":"Contrôler ?",
    "Klavier / Pedaal":"Clavier / Pédalier","Register opnemen":"Enregistrer le jeu","Apparaten verversen":"Actualiser les appareils","Laden...":"Chargement...","MP3 Bitrate":"Débit MP3","Mapnaam:":"Nom du dossier :","Mapnaam: —":"Nom du dossier : —","Scan de QR-code met je telefoon of tablet":"Scannez le QR code avec votre téléphone ou tablette","om de afstandsbediening te openen":"pour ouvrir la télécommande","Internet is niet nodig — de bediening werkt ook zonder. Houd dit venster open; schakelt de hotspot uit, zet hem opnieuw aan.":"Internet n'est pas nécessaire — la commande fonctionne sans. Gardez cette fenêtre ouverte ; si le point d'accès se coupe, réactivez-le.","Klik op 'Open hotspot-instellingen' en zet de Mobiele hotspot AAN.":"Cliquez sur « Ouvrir les réglages du point d'accès » et activez le point d'accès mobile.","Noteer de netwerknaam en het wachtwoord die Windows toont.":"Notez le nom du réseau et le mot de passe affichés par Windows.","Verbind je iPad/iPhone of Android met dat netwerk.":"Connectez votre iPad/iPhone ou Android à ce réseau.","Kies hierboven het hotspot-netwerk en scan de QR-code (of typ het adres).":"Choisissez le réseau du point d'accès ci-dessus et scannez le QR code (ou tapez l'adresse).","Pad naar register-, klavier- of orgelmap":"Chemin vers le dossier jeu, clavier ou orgue","Her-opname:":"Réenregistrement :","Registers toevoegen/verwijderen per klavier. C-groot = MIDI 36.":"Ajouter/supprimer des jeux par clavier. Do grave = MIDI 36.","Register toevoegen":"Ajouter un jeu"
  }
  };
  const HANDLEIDING = {
    "en": `<div class="modal-title">JM-Rec — Manual</div>
<h2>Quick start</h2>
<ul>
<li>On startup, go through the <strong>wizard</strong> (10 steps) to define the organ + registers — or choose <strong>Continue</strong> with the last organ.</li>
<li>On the <strong>main screen</strong>: select a register and press <strong>Record</strong>.</li>
<li>Scan the <strong>QR code</strong> (QR Remote) to control from your phone.</li>
<li>Edit afterwards: button <strong>Registers</strong> (add/remove, mark reviewed) or <strong>New organ</strong>.</li>
</ul>
<h2>Control</h2>
<table>
<tr><th>Button</th><th>Function</th></tr>
<tr><td><code>Record</code></td><td>Start the automatic recording cycle of the selected register</td></tr>
<tr><td><code>Pause</code></td><td>Pauses after the current note</td></tr>
<tr><td><code>Stop</code></td><td>Stops immediately</td></tr>
<tr><td><code>Previous note / Next note</code></td><td>Jump to another note</td></tr>
<tr><td><code>Redo</code></td><td>Re-record the current note</td></tr>
</table>
<h2>Colour codes per register</h2>
<table>
<tr><th>Colour</th><th>Meaning</th></tr>
<tr><td>🔴 red</td><td>to be recorded (0 notes)</td></tr>
<tr><td>🟠 orange</td><td>started, not yet complete</td></tr>
<tr><td>🟣 purple</td><td>fully recorded, to be reviewed</td></tr>
<tr><td>🟢 green</td><td>reviewed and approved</td></tr>
</table>
<p>Mark a register as <strong>reviewed</strong> (purple → green) via the <strong>Registers</strong> button.</p>
<p>As soon as a register is fully recorded, the question <strong>Review now / Approved / Later</strong> appears automatically — on the PC and the remote.</p>
<h2>Recording cycle</h2>
<p>Per note: <strong>Countdown</strong> (default 5s) → <strong>Record</strong> (default 5s) → <strong>Next note</strong>. This repeats automatically until the last note.</p>
<h2>Intelligent recording (assistive)</h2>
<p>Set <strong>Recording mode</strong> to <em>Intelligent (assistive)</em> in the settings (microphone input only). The recorder measures the noise floor, waits for the tone and listens whether the sound is <strong>stable and loopable</strong>. Once there is enough good tone, a green cue <strong>&ldquo;Enough — release&rdquo;</strong> appears. Release the key: the <strong>release tail</strong> is captured down to silence and the recorder moves on. For a <em>tremulant</em> series it waits for a stable tremulant modulation instead of a flat tone. <em>Min. stable tone</em> sets how much good tone is required, <em>Max. duration</em> is a safety cap, and <em>Sensitivity</em> controls how sensitive detection is (higher = approves faster, lower = stricter). You can always use <strong>Next</strong>/<strong>Stop</strong> manually.</p>
<h2>File names</h2>
<p>File naming:</p>
<div class="tip-box"><code>036-c.mp3</code>, <code>037-c#.mp3</code>, <code>038-d.mp3</code>, ..., <code>096-c.mp3</code><br>Format: <code>{MIDI-number}-{note-name}.mp3</code></div>
<h2>Folder structure</h2>
<div class="tip-box"><code>Storage / Organ / Manual / Register / 036-c.mp3</code><br>With multi-mic: <code>... / Register / Position / 036-c.mp3</code></div>
<h2>Export to JM-Orgue (.organ)</h2>
<p>Via <strong>Settings &rarr; Export</strong> you create a <code>.organ</code> definition file in the project folder. It contains all manuals, stops (with footage), swell boxes, tremulants and couplers, with references to the recorded samples. JM-Orgue loads this file directly; missing notes stay silent and can be recorded later (then export again). With multi-mic the first configured microphone position is used.</p>
<h2>Adjustable parameters</h2>
<table>
<tr><th>Parameter</th><th>Default</th><th>Options</th></tr>
<tr><td>Sample rate</td><td>44100 Hz</td><td>44100 / 48000 / 96000</td></tr>
<tr><td>Bit depth</td><td>16-bit</td><td>16 / 24</td></tr>
<tr><td>Channels</td><td>Mono</td><td>Mono / Stereo</td></tr>
<tr><td>MP3 bitrate</td><td>192 kbps</td><td>128 / 192 / 256 / 320</td></tr>
<tr><td>Countdown</td><td>5 sec</td><td>1 – 30</td></tr>
<tr><td>Record duration</td><td>5 sec</td><td>1 – 60</td></tr>
<tr><td>Start note</td><td>MIDI 36 (C2)</td><td>0 – 127</td></tr>
<tr><td>End note</td><td>MIDI 96 (C7)</td><td>0 – 127</td></tr>
</table>
<h2>Recording tips</h2>
<ul>
<li>Use a <strong>condenser microphone</strong> for the best quality</li>
<li>Record in <strong>24-bit</strong> for maximum dynamics</li>
<li>Use <strong>Stereo</strong> with an AB or ORTF microphone setup</li>
<li>Set the record duration long enough for slow-speaking pipes (<strong>10+ sec</strong> for 16')</li>
<li>Keep the <strong>wind pressure constant</strong> — wait until the organ is stable before starting</li>
<li>Record in a <strong>quiet environment</strong> — avoid traffic, wind and church bells</li>
<li>Place the microphone <strong>1-2 metres</strong> from the pipes for a natural sound</li>
</ul>
<h2>Conversion to WAV</h2>
<div class="tip-box">Convert MP3 to WAV:<br><br><code>for %f in (*.mp3) do ffmpeg -i "%f" "%~nf.wav"</code></div>
<h2>Network &amp; Connection</h2>
<div class="warn-box">Your phone and this PC must be on the <strong>same network</strong> (WiFi).<br>Alternatives: USB tethering or a mobile hotspot.</div>
<p style="color:var(--dim);margin-top:20px;font-size:0.8rem;text-align:center;">JM-Rec v3.7</p>`,
    "de": `<div class="modal-title">JM-Rec — Handbuch</div>
<h2>Schnellstart</h2>
<ul>
<li>Gehen Sie beim Start durch den <strong>Assistenten</strong> (10 Schritte), um die Orgel + Register festzulegen — oder wählen Sie <strong>Fortfahren</strong> mit der letzten Orgel.</li>
<li>Auf dem <strong>Hauptbildschirm</strong>: wählen Sie ein Register und drücken Sie <strong>Aufnehmen</strong>.</li>
<li>Scannen Sie den <strong>QR-Code</strong> (QR Remote), um per Telefon zu steuern.</li>
<li>Später bearbeiten: Schaltfläche <strong>Register</strong> (hinzufügen/entfernen, als geprüft markieren) oder <strong>Neue Orgel</strong>.</li>
</ul>
<h2>Steuerung</h2>
<table>
<tr><th>Taste</th><th>Funktion</th></tr>
<tr><td><code>Aufnehmen</code></td><td>Startet den automatischen Aufnahmezyklus des gewählten Registers</td></tr>
<tr><td><code>Pause</code></td><td>Pausiert nach der aktuellen Note</td></tr>
<tr><td><code>Stopp</code></td><td>Stoppt sofort</td></tr>
<tr><td><code>Vorherige Note / Nächste Note</code></td><td>Zu einer anderen Note springen</td></tr>
<tr><td><code>Wiederholen</code></td><td>Aktuelle Note neu aufnehmen</td></tr>
</table>
<h2>Farbcodes pro Register</h2>
<table>
<tr><th>Farbe</th><th>Bedeutung</th></tr>
<tr><td>🔴 rot</td><td>noch aufzunehmen (0 Noten)</td></tr>
<tr><td>🟠 orange</td><td>begonnen, noch nicht vollständig</td></tr>
<tr><td>🟣 lila</td><td>vollständig aufgenommen, noch zu prüfen</td></tr>
<tr><td>🟢 grün</td><td>geprüft und freigegeben</td></tr>
</table>
<p>Markieren Sie ein Register als <strong>geprüft</strong> (lila → grün) über die Schaltfläche <strong>Register</strong>.</p>
<p>Sobald ein Register vollständig aufgenommen ist, erscheint automatisch die Frage <strong>Jetzt prüfen / Freigegeben / Später</strong> — am PC und an der Fernsteuerung.</p>
<h2>Aufnahmezyklus</h2>
<p>Pro Note: <strong>Countdown</strong> (Standard 5s) → <strong>Aufnehmen</strong> (Standard 5s) → <strong>Nächste Note</strong>. Dies wiederholt sich automatisch bis zur letzten Note.</p>
<h2>Intelligente Aufnahme (assistierend)</h2>
<p>Stellen Sie in den Einstellungen den <strong>Aufnahmemodus</strong> auf <em>Intelligent (assistierend)</em> (nur Mikrofoneingang). Der Recorder misst den Geräuschpegel, wartet auf den Ton und prüft, ob der Klang <strong>stabil und loop-fähig</strong> ist. Sobald genug guter Ton vorhanden ist, erscheint ein grünes Signal <strong>&ldquo;Genug — loslassen&rdquo;</strong>. Lassen Sie die Taste los: der <strong>Ausklang</strong> wird bis zur Stille aufgenommen und der Recorder geht weiter. Bei einer <em>Tremulant</em>-Reihe wartet er auf eine stabile Tremulant-Modulation statt auf einen gleichmäßigen Ton. <em>Min. stabiler Ton</em> legt fest, wie viel guter Ton nötig ist, <em>Max. Dauer</em> ist eine Sicherheitsgrenze und <em>Empfindlichkeit</em> steuert, wie empfindlich die Erkennung ist (höher = schnellere Freigabe, niedriger = strenger). <strong>Nächste</strong>/<strong>Stopp</strong> geht jederzeit manuell.</p>
<h2>Dateinamen</h2>
<p>Dateibenennung:</p>
<div class="tip-box"><code>036-c.mp3</code>, <code>037-c#.mp3</code>, <code>038-d.mp3</code>, ..., <code>096-c.mp3</code><br>Format: <code>{MIDI-Nummer}-{Notenname}.mp3</code></div>
<h2>Ordnerstruktur</h2>
<div class="tip-box"><code>Speicherort / Orgel / Manual / Register / 036-c.mp3</code><br>Bei Multi-Mikrofon: <code>... / Register / Position / 036-c.mp3</code></div>
<h2>Export nach JM-Orgue (.organ)</h2>
<p>Über <strong>Einstellungen &rarr; Exportieren</strong> erstellen Sie eine <code>.organ</code>-Definitionsdatei im Projektordner. Sie enthält alle Manuale, Register (mit Fußlage), Schwellkästen, Tremulanten und Koppeln, mit Verweisen auf die aufgenommenen Samples. JM-Orgue lädt diese Datei direkt; fehlende Töne bleiben stumm und können später aufgenommen werden (danach erneut exportieren). Bei Multi-Mikrofon wird die zuerst konfigurierte Position verwendet.</p>
<h2>Einstellbare Parameter</h2>
<table>
<tr><th>Parameter</th><th>Standard</th><th>Optionen</th></tr>
<tr><td>Abtastrate</td><td>44100 Hz</td><td>44100 / 48000 / 96000</td></tr>
<tr><td>Bittiefe</td><td>16-bit</td><td>16 / 24</td></tr>
<tr><td>Kanäle</td><td>Mono</td><td>Mono / Stereo</td></tr>
<tr><td>MP3-Bitrate</td><td>192 kbps</td><td>128 / 192 / 256 / 320</td></tr>
<tr><td>Countdown</td><td>5 Sek.</td><td>1 – 30</td></tr>
<tr><td>Aufnahmedauer</td><td>5 Sek.</td><td>1 – 60</td></tr>
<tr><td>Startnote</td><td>MIDI 36 (C2)</td><td>0 – 127</td></tr>
<tr><td>Endnote</td><td>MIDI 96 (C7)</td><td>0 – 127</td></tr>
</table>
<h2>Aufnahmetipps</h2>
<ul>
<li>Verwenden Sie ein <strong>Kondensatormikrofon</strong> für beste Qualität</li>
<li>Nehmen Sie in <strong>24-bit</strong> für maximale Dynamik auf</li>
<li>Verwenden Sie <strong>Stereo</strong> bei einer AB- oder ORTF-Aufstellung</li>
<li>Stellen Sie die Aufnahmedauer lang genug für langsam ansprechende Pfeifen ein (<strong>10+ Sek.</strong> für 16')</li>
<li>Halten Sie den <strong>Winddruck konstant</strong> — warten Sie, bis die Orgel stabil ist</li>
<li>Nehmen Sie in <strong>ruhiger Umgebung</strong> auf — vermeiden Sie Verkehr, Wind und Kirchenglocken</li>
<li>Platzieren Sie das Mikrofon <strong>1-2 Meter</strong> von den Pfeifen entfernt</li>
</ul>
<h2>Konvertierung zu WAV</h2>
<div class="tip-box">MP3 zu WAV konvertieren:<br><br><code>for %f in (*.mp3) do ffmpeg -i "%f" "%~nf.wav"</code></div>
<h2>Netzwerk &amp; Verbindung</h2>
<div class="warn-box">Ihr Telefon und dieser PC müssen im <strong>selben Netzwerk</strong> sein (WLAN).<br>Alternativen: USB-Tethering oder ein mobiler Hotspot.</div>
<p style="color:var(--dim);margin-top:20px;font-size:0.8rem;text-align:center;">JM-Rec v3.7</p>`,
    "fr": `<div class="modal-title">JM-Rec — Manuel</div>
<h2>Démarrage rapide</h2>
<ul>
<li>Au démarrage, suivez l'<strong>assistant</strong> (10 étapes) pour définir l'orgue + les jeux — ou choisissez <strong>Continuer</strong> avec le dernier orgue.</li>
<li>Sur l'<strong>écran principal</strong> : choisissez un jeu et appuyez sur <strong>Enregistrer</strong>.</li>
<li>Scannez le <strong>QR code</strong> (QR Remote) pour commander depuis votre téléphone.</li>
<li>Modifier ensuite : bouton <strong>Jeux</strong> (ajouter/supprimer, marquer contrôlé) ou <strong>Nouvel orgue</strong>.</li>
</ul>
<h2>Commande</h2>
<table>
<tr><th>Bouton</th><th>Fonction</th></tr>
<tr><td><code>Enregistrer</code></td><td>Démarre le cycle d'enregistrement automatique du jeu choisi</td></tr>
<tr><td><code>Pause</code></td><td>Met en pause après la note actuelle</td></tr>
<tr><td><code>Arrêt</code></td><td>Arrête immédiatement</td></tr>
<tr><td><code>Note précédente / Note suivante</code></td><td>Aller à une autre note</td></tr>
<tr><td><code>Refaire</code></td><td>Réenregistrer la note actuelle</td></tr>
</table>
<h2>Codes couleur par jeu</h2>
<table>
<tr><th>Couleur</th><th>Signification</th></tr>
<tr><td>🔴 rouge</td><td>à enregistrer (0 note)</td></tr>
<tr><td>🟠 orange</td><td>commencé, pas encore complet</td></tr>
<tr><td>🟣 violet</td><td>entièrement enregistré, à contrôler</td></tr>
<tr><td>🟢 vert</td><td>contrôlé et validé</td></tr>
</table>
<p>Marquez un jeu comme <strong>contrôlé</strong> (violet → vert) via le bouton <strong>Jeux</strong>.</p>
<p>Dès qu'un jeu est entièrement enregistré, la question <strong>Contrôler maintenant / Validé / Plus tard</strong> apparaît automatiquement — sur le PC et la télécommande.</p>
<h2>Cycle d'enregistrement</h2>
<p>Par note : <strong>Compte à rebours</strong> (5s par défaut) → <strong>Enregistrer</strong> (5s par défaut) → <strong>Note suivante</strong>. Cela se répète automatiquement jusqu'à la dernière note.</p>
<h2>Enregistrement intelligent (assisté)</h2>
<p>Réglez le <strong>Mode d'enregistrement</strong> sur <em>Intelligent (assisté)</em> dans les réglages (entrée microphone uniquement). L'enregistreur mesure le bruit de fond, attend le son et vérifie s'il est <strong>stable et bouclable</strong>. Dès qu'il y a assez de bon son, un signal vert <strong>&ldquo;Assez — relâchez&rdquo;</strong> apparaît. Relâchez la touche : la <strong>résonance</strong> est capturée jusqu'au silence et l'enregistreur passe à la note suivante. Pour une série <em>tremblant</em>, il attend une modulation de tremblant stable au lieu d'un son plat. <em>Son stable min.</em> définit la quantité de bon son requise, <em>Durée max.</em> est une limite de sécurité et <em>Sensibilité</em> règle la sensibilité de la détection (plus haut = validation plus rapide, plus bas = plus strict). <strong>Suivante</strong>/<strong>Stop</strong> restent disponibles manuellement.</p>
<h2>Noms de fichiers</h2>
<p>Nommage des fichiers :</p>
<div class="tip-box"><code>036-c.mp3</code>, <code>037-c#.mp3</code>, <code>038-d.mp3</code>, ..., <code>096-c.mp3</code><br>Format : <code>{numéro-MIDI}-{nom-de-note}.mp3</code></div>
<h2>Structure des dossiers</h2>
<div class="tip-box"><code>Stockage / Orgue / Clavier / Jeu / 036-c.mp3</code><br>Avec multi-micro : <code>... / Jeu / Position / 036-c.mp3</code></div>
<h2>Export vers JM-Orgue (.organ)</h2>
<p>Via <strong>Réglages &rarr; Exporter</strong>, vous créez un fichier de définition <code>.organ</code> dans le dossier du projet. Il contient tous les claviers, jeux (avec hauteur en pieds), boîtes expressives, tremblants et accouplements, avec les références aux échantillons enregistrés. JM-Orgue charge ce fichier directement ; les notes manquantes restent muettes et peuvent être enregistrées plus tard (puis réexportez). En multi-micro, la première position configurée est utilisée.</p>
<h2>Paramètres réglables</h2>
<table>
<tr><th>Paramètre</th><th>Défaut</th><th>Options</th></tr>
<tr><td>Fréquence</td><td>44100 Hz</td><td>44100 / 48000 / 96000</td></tr>
<tr><td>Profondeur</td><td>16 bits</td><td>16 / 24</td></tr>
<tr><td>Canaux</td><td>Mono</td><td>Mono / Stéréo</td></tr>
<tr><td>Débit MP3</td><td>192 kbps</td><td>128 / 192 / 256 / 320</td></tr>
<tr><td>Compte à rebours</td><td>5 s</td><td>1 – 30</td></tr>
<tr><td>Durée d'enregistrement</td><td>5 s</td><td>1 – 60</td></tr>
<tr><td>Note de départ</td><td>MIDI 36 (do2)</td><td>0 – 127</td></tr>
<tr><td>Note de fin</td><td>MIDI 96 (do7)</td><td>0 – 127</td></tr>
</table>
<h2>Conseils d'enregistrement</h2>
<ul>
<li>Utilisez un <strong>microphone à condensateur</strong> pour la meilleure qualité</li>
<li>Enregistrez en <strong>24 bits</strong> pour une dynamique maximale</li>
<li>Utilisez la <strong>stéréo</strong> avec une prise AB ou ORTF</li>
<li>Réglez une durée suffisante pour les tuyaux à parole lente (<strong>10+ s</strong> pour 16')</li>
<li>Maintenez une <strong>pression de vent constante</strong> — attendez que l'orgue soit stable</li>
<li>Enregistrez dans un <strong>environnement calme</strong> — évitez le trafic, le vent et les cloches</li>
<li>Placez le micro à <strong>1-2 mètres</strong> des tuyaux pour un son naturel</li>
</ul>
<h2>Conversion en WAV</h2>
<div class="tip-box">Convertir MP3 en WAV :<br><br><code>for %f in (*.mp3) do ffmpeg -i "%f" "%~nf.wav"</code></div>
<h2>Réseau &amp; Connexion</h2>
<div class="warn-box">Votre téléphone et ce PC doivent être sur le <strong>même réseau</strong> (WiFi).<br>Alternatives : partage USB ou point d'accès mobile.</div>
<p style="color:var(--dim);margin-top:20px;font-size:0.8rem;text-align:center;">JM-Rec v3.7</p>`
  };
  let LANG = 'nl';
  window.jmLangs = ['nl','en','de','fr'];
  window.jmInitLang = function(){
    let l = null; try { l = localStorage.getItem('jmLang'); } catch(e){}
    if(!l){ const n=(navigator.language||'en').slice(0,2).toLowerCase(); l = window.jmLangs.indexOf(n)>=0 ? n : 'en'; }
    LANG = l; window.LANG = l; return l;
  };
  window.tr = function(s){
    if(LANG==='nl' || s==null) return s;
    const m=I18N[LANG]; if(!m) return s;
    const k=(''+s).trim();
    return (m[k]!==undefined) ? m[k] : s;
  };
  window.translateTree = function(root){
    if(LANG==='nl') return; const m=I18N[LANG]; if(!m) return;
    root = root||document.body;
    try {
      const w=document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
      const arr=[]; while(w.nextNode()) arr.push(w.currentNode);
      arr.forEach(n=>{
        const raw=n.nodeValue; const t=raw.trim(); if(!t) return;
        if(m[t]!==undefined){ n.nodeValue=raw.replace(t, function(){return m[t];}); return; }
        // strip leading/trailing symbols/emoji (▶ ■ ◀ ↻ 🔍 ✓ 🎉 →) and match the core text
        let core=t;
        try { core = t.replace(/^[^\p{L}\p{N}]+/u,'').replace(/[^\p{L}\p{N}?!.)]+$/u,''); } catch(e){ core = t.replace(/^[^A-Za-z0-9À-ÿ]+/,'').replace(/[^A-Za-z0-9À-ÿ?!.)]+$/,''); }
        core=core.trim();
        if(core && core!==t && m[core]!==undefined){ n.nodeValue=raw.replace(core, function(){return m[core];}); }
      });
      root.querySelectorAll('[placeholder]').forEach(e=>{ const t=(e.getAttribute('placeholder')||'').trim(); if(m[t]!==undefined) e.setAttribute('placeholder', m[t]); });
      root.querySelectorAll('[title]').forEach(e=>{ const t=(e.getAttribute('title')||'').trim(); if(m[t]!==undefined) e.setAttribute('title', m[t]); });
    } catch(e){}
  };
  window.jmSetLang = function(l){ try { localStorage.setItem('jmLang', l); } catch(e){} location.reload(); };
  window.jmLangSelectorHtml = function(){
    return '<select onchange="jmSetLang(this.value)" title="Taal / Language" style="font-family:inherit;font-size:0.78rem;background:transparent;color:inherit;border:1px solid currentColor;border-radius:6px;padding:3px 6px;opacity:0.75;cursor:pointer;">'+
      window.jmLangs.map(function(l){return '<option value="'+l+'" style="color:#111;"'+(l===window.LANG?' selected':'')+'>'+l.toUpperCase()+'</option>';}).join('')+'</select>';
  };
  window.applyHandleiding = function(){
    if(LANG==='nl') return;
    const h=HANDLEIDING[LANG]; const el=document.getElementById('readmeBody');
    if(h && el) el.innerHTML=h;
  };
  window.jmApplyLang = function(l){ LANG=l; window.LANG=l; try{translateTree(document.body);}catch(e){} try{applyHandleiding();}catch(e){} };
})();
'''


def default_folder_code(plaats, bouwer):
    """Project folder code = first 4 letters of place + first 4 of organ builder."""
    p = sanitize_path_component(plaats or "").replace(" ", "")[:4]
    b = sanitize_path_component(bouwer or "").replace(" ", "")[:4]
    return (p + b) or "Orgel"


def _last_project_pointer_path():
    return os.path.join(str(Path.home()), ".jm-rec", "last_project.json")


def get_last_project():
    """Return the saved 'last project' pointer dict, or None if missing/stale."""
    try:
        p = _last_project_pointer_path()
        if not os.path.isfile(p):
            return None
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("path") and os.path.exists(data["path"]):
            return data
    except Exception:
        pass
    return None


def pick_folder_dialog(title="Kies opslagmap"):
    """Open a native Windows folder-picker and return the chosen path (or None).
    Pure ctypes (SHBrowseForFolder) — no extra dependency, works windowless."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32

    class BROWSEINFO(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", ctypes.c_void_p),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        ]

    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFO)]
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]

    try:
        ole32.CoInitialize(None)
    except Exception:
        pass
    disp = ctypes.create_unicode_buffer(260)
    bi = BROWSEINFO()
    bi.hwndOwner = 0
    bi.pidlRoot = None
    bi.pszDisplayName = ctypes.cast(disp, wintypes.LPWSTR)
    bi.lpszTitle = title
    bi.ulFlags = 0x00000001 | 0x00000040  # BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE
    pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
    if not pidl:
        return None
    path_buf = ctypes.create_unicode_buffer(260)
    ok = shell32.SHGetPathFromIDListW(ctypes.c_void_p(pidl), path_buf)
    try:
        ole32.CoTaskMemFree(pidl)
    except Exception:
        pass
    return path_buf.value if ok else None


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
        # keyboards: [{"name","zwelwerk","tremulant","registers":[...]}, ...]
        #   register: {"name","display","foot","begin_note","end_note","bass_treble"}
        self.keyboards = []
        self.pedal_registers = []  # registers for the pedal (same register shape)
        self.couplers = []         # [{"source": "Zwelwerk", "target": "Hoofdwerk"}, ...]
        self.has_pedal = False
        self.current_keyboard = "" # selected keyboard/pedal name
        self.tremulant = False     # append _trem to register folder

        # Orgel-metadata (wizard)
        self.plaats = ""           # plaatsnaam
        self.kerk = ""             # kerknaam
        self.bouwer = ""           # orgelbouwer
        self.tremulant_scope = "none"  # "none" | "organ" | "keyboard"

        # Actieve registerselectie (door wizard/afstandsbediening gekozen reeks)
        self.active_keyboard = ""
        self.active_register = ""
        self.active_variant = "normal"  # "normal" | "trem"

        # Controle-prompt: gezet zodra een reeks 100% is opgenomen
        self.check_prompt = None  # {keyboard, register, variant, display, recorded, expected}

        # Plan-cache (disk-scan voortgang) — bijgewerkt door een achtergrond-thread
        # zodat get_state nooit schijf-I/O doet in de poll-hot-path (anti-hang).
        self._plan_cache = None
        self._plan_stop = False

        # Recording workflow settings
        self.countdown_seconds = 5
        self.record_seconds = 5

        # Intelligent (assistive) auto-record mode
        self.record_mode = "fixed"        # "fixed" | "auto"
        self.min_stable_seconds = 2.0     # required stable/loopable sustain before the "release" cue
        self.max_record_seconds = 20.0    # safety cap (from onset) so a non-stabilizing pipe can't hang the cycle
        self.auto_sensitivity = 1.0       # 0.3 (streng/traag) .. 3.0 (los/gevoelig) — deelt onset-drempel, schaalt CoV-tolerantie
        self.noise_floor_rms = 0.0        # measured from pre-onset audio each note
        self.auto_phase = "idle"          # idle | waiting | stabilizing | hold | release
        self.hold_release_cue = False     # True → UI shows the green "genoeg — laat los" cue
        self.stable_progress = 0.0        # 0..1 progress toward min_stable_seconds
        self._abort_take = False          # set by next/prev/set_note to abort the running take unsaved
        self.pause_requested = False      # deferred pause: finish + save the current take first

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
        self.last_error = ""   # last recording error, surfaced in UI

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

        # Background thread that refreshes the disk-scan progress cache.
        threading.Thread(target=self._plan_refresh_loop, daemon=True).start()

    def _plan_refresh_loop(self):
        """Refresh the plan/progress cache off the request path so get_state()
        never blocks on disk I/O (prevents the UI from hanging on big organs)."""
        while not self._plan_stop:
            try:
                if self.project_name:
                    self._plan_cache = self.build_plan()
            except Exception:
                pass
            time.sleep(2.0)

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
        """Convert keyboard list to objects if needed. Accepts strings or dicts.
        Tolerates old dicts lacking 'tremulant'/'registers'."""
        result = []
        for kb in keyboards:
            if isinstance(kb, str):
                result.append({"name": kb, "zwelwerk": False, "tremulant": False, "registers": []})
            elif isinstance(kb, dict):
                result.append({
                    "name": kb.get("name", ""),
                    "zwelwerk": bool(kb.get("zwelwerk", False)),
                    "tremulant": bool(kb.get("tremulant", False)),
                    "registers": self._normalize_registers(kb.get("registers", [])),
                })
        return result

    def _normalize_registers(self, registers):
        """Normalize a list of register dicts to the canonical shape."""
        result = []
        for r in registers or []:
            if not isinstance(r, dict):
                continue
            display = r.get("display") or r.get("name") or ""
            name = sanitize_path_component(r.get("name") or format_register_name(display))
            if not name:
                continue
            try:
                begin = int(r.get("begin_note", self.start_note))
                end = int(r.get("end_note", self.end_note))
            except (ValueError, TypeError):
                begin, end = self.start_note, self.end_note
            if end < begin:
                begin, end = end, begin
            checked = r.get("checked", {})
            if not isinstance(checked, dict):
                checked = {}
            result.append({
                "name": name,
                "display": display or name,
                "foot": str(r.get("foot", "")),
                "begin_note": begin,
                "end_note": end,
                "bass_treble": bool(r.get("bass_treble", False)),
                "checked": {"normal": bool(checked.get("normal", False)),
                            "trem": bool(checked.get("trem", False))},
            })
        return result

    def _kb_names(self):
        """Get list of keyboard names."""
        return [kb["name"] for kb in self.keyboards]

    def _find_keyboard(self, name):
        """Find a keyboard dict by name. 'Pedaal' returns a synthetic entry
        backed by self.pedal_registers."""
        if name == "Pedaal" and self.has_pedal:
            return {"name": "Pedaal", "zwelwerk": False,
                    "tremulant": False, "registers": self.pedal_registers}
        for kb in self.keyboards:
            if kb["name"] == name:
                return kb
        return None

    def _find_register(self, kb, register_name):
        """Find a register dict within a keyboard by its (sanitized) name."""
        if not kb:
            return None
        for r in kb.get("registers", []):
            if r["name"] == register_name:
                return r
        return None

    def setup_organ(self, organ_name, keyboards, has_pedal, output_dir=None):
        """Set up organ project: creates main folder + keyboard/pedal subfolders."""
        self.project_name = sanitize_path_component(organ_name)
        self.keyboards = self._normalize_keyboards(keyboards)
        # Keep stored names filesystem-safe so folders match the save path.
        for kb in self.keyboards:
            kb["name"] = sanitize_path_component(kb["name"])
        self.has_pedal = has_pedal
        if output_dir:
            self.output_dir = normalize_output_dir(output_dir)
        base = os.path.join(self.output_dir, self.project_name)
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
        reg_name = sanitize_path_component(self.register_name)
        if self.tremulant and not reg_name.endswith("_trem"):
            reg_name += "_trem"
        # Sanitize every component so a stray illegal char in the organ,
        # keyboard or register name can't break the path (WinError 123).
        base = os.path.join(self.output_dir,
                            sanitize_path_component(self.project_name),
                            sanitize_path_component(self.current_keyboard),
                            reg_name)
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
        self.project_name = sanitize_path_component(project_name)
        self.register_name = sanitize_path_component(register_name)
        if output_dir:
            self.output_dir = normalize_output_dir(output_dir)

        # Create directories
        path = self.get_current_register_path()
        os.makedirs(path, exist_ok=True)
        # Create multi-mic subdirs if applicable
        if len(self.device_indices) > 1:
            for idx in self.device_indices:
                sub = sanitize_path_component(self.device_names.get(idx, f"Mic_{idx}"))
                os.makedirs(os.path.join(path, sub), exist_ok=True)
        return path
    
    def start_recording_cycle(self):
        """Start the countdown → record → next cycle."""
        with self.lock:
            if self.state != "idle" and self.state != "paused":
                return
            self.state = "countdown"
            self.is_running = True
            self.last_error = ""
            self.check_prompt = None
            self._abort_take = False
            self.pause_requested = False
        thread = threading.Thread(target=self._recording_cycle, daemon=True)
        thread.start()

    def _check_completion_prompt(self):
        """If the active register series is now fully recorded (and not yet
        approved), set check_prompt so the UI can offer to review it."""
        if not self.active_register:
            return
        prog = self.register_progress(self.active_keyboard, self.active_register, self.active_variant)
        if prog["expected"] <= 0 or prog["recorded"] < prog["expected"]:
            return
        reg = self._find_register(self._find_keyboard(self.active_keyboard), self.active_register)
        if reg and reg.get("checked", {}).get(self.active_variant, False):
            return  # already approved
        self.check_prompt = {
            "keyboard": self.active_keyboard,
            "register": self.active_register,
            "variant": self.active_variant,
            "display": (reg.get("display", self.active_register) if reg else self.active_register),
            "recorded": prog["recorded"], "expected": prog["expected"],
        }
        self._refresh_plan_cache()
    
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
            # Deferred pause aangevraagd in de gap tussen twee noten: honoreer
            # hem hier, anders wordt eerst nog een volledige noot opgenomen.
            if self.pause_requested:
                self.pause_requested = False
                self.is_running = False
                self.state = "paused"
                self._check_completion_prompt()
                self._notify()
                return

            # Skip notes not in selected split range
            if self._should_skip_note():
                if self.auto_advance and self.current_note < self.end_note:
                    self.current_note += 1
                    continue
                else:
                    self.state = "idle"
                    self.is_running = False
                    self._check_completion_prompt()
                    self._notify()
                    return

            # Countdown phase. In auto mode the countdown runs inside
            # _do_record_auto (streams open) so the noise floor is measured
            # from genuinely pre-tone audio.
            auto = self.record_mode == "auto" and self.input_mode == "mic"
            self.state = "countdown"
            self._notify()

            if not auto:
                for i in range(self.countdown_seconds, 0, -1):
                    if not self.is_running:
                        return
                    self.countdown_value = i
                    self._notify()
                    time.sleep(1)
                # Stop tijdens de laatste aftelseconde niet overschrijven.
                if not self.is_running:
                    return
                self.countdown_value = 0
                self._notify()

            # Recording phase
            self._abort_take = False
            if auto:
                # Assistive intelligent mode: adaptive capture (mic input only).
                # Loopback stays on fixed duration in this version.
                self._do_record_auto()
            else:
                self.state = "recording"
                self._notify()
                self._do_record()

            if not self.is_running:
                return

            # Deferred pause: the current take was finished and saved first.
            if self.pause_requested:
                self.pause_requested = False
                self.is_running = False
                self.state = "paused"
                self._check_completion_prompt()
                self._notify()
                return

            # Auto-advance or wait. When the user jumped to another note via
            # Volgende/Vorige during the take, that take was aborted (unsaved)
            # and the cycle continues at the user-chosen note without advancing.
            # Read-and-clear onder de lock: een next_note die precies tussen de
            # read en de clear valt zou anders verloren gaan (dubbele advance).
            with self.lock:
                aborted = self._abort_take
                self._abort_take = False
            if aborted:
                time.sleep(0.2)
            elif self.auto_advance and self.current_note < self.end_note:
                self.current_note += 1
                # Brief pause between notes
                time.sleep(0.5)
            else:
                self.state = "paused"
                self._check_completion_prompt()
                self._notify()
                return

        # All done
        self.state = "idle"
        self.is_running = False
        self._check_completion_prompt()
        self._notify()
    
    def _resolve_record_params(self, device_index):
        """Return a (samplerate, channels) the device actually supports.

        WASAPI rejects mismatched settings outright (e.g. 44100 Hz on a mic that
        only runs at 48000), which raises an error the instant recording starts.
        Prefer the configured values, but fall back to the device's native rate
        and channel count so recording never fails silently.
        """
        sr, ch = self.sample_rate, self.channels
        dtype = 'float32' if self.bit_depth == 24 else 'int16'
        try:
            if device_index is not None:
                info = sd.query_devices(device_index)
            else:
                info = sd.query_devices(kind='input')
            native_sr = int(info.get('default_samplerate', sr))
            max_ch = int(info.get('max_input_channels', ch)) or ch
        except Exception:
            native_sr, max_ch = sr, ch
        # 1) configured combo as-is
        try:
            sd.check_input_settings(device=device_index, samplerate=sr, channels=ch, dtype=dtype)
            return sr, ch
        except Exception:
            pass
        # 2) device's native rate, keep configured channel count
        sr = native_sr
        try:
            sd.check_input_settings(device=device_index, samplerate=sr, channels=ch, dtype=dtype)
            return sr, ch
        except Exception:
            pass
        # 3) native rate + device's own channel count
        return sr, max_ch

    def _do_record(self):
        """Record audio from selected device(s)."""
        if self.input_mode == "loopback":
            self._do_record_loopback()
            return

        dev = self.device_index  # None or single index

        # Auto-adjust sample rate / channels to what the device supports, so a
        # WASAPI rate mismatch can't make recording fail silently.
        sr, ch = self._resolve_record_params(dev)
        if sr != self.sample_rate or ch != self.channels:
            self.sample_rate = sr
            self.channels = ch
            self._notify()

        frames = int(self.sample_rate * self.record_seconds)
        channels = self.channels
        dtype = 'float32' if self.bit_depth == 24 else 'int16'

        if len(self.device_indices) > 1:
            self._do_record_multi(frames, channels, dtype)
        else:
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
                if not self.is_running or self._abort_take:
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
            self.last_error = ""

        except Exception as e:
            print(f"Recording error: {e}", flush=True)
            import traceback; traceback.print_exc()
            self.current_level = 0.0
            self.last_error = f"Opname mislukt: {e}"
            self._notify()

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
                    if not self.is_running or self._abort_take:
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
            self.last_error = ""

        except Exception as e:
            print(f"Loopback recording error: {e}")
            self.current_level = 0.0
            self.last_error = f"Loopback-opname mislukt: {e}"
            self._notify()

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

        failed = []
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
                    failed.append(self.device_names.get(dev_idx, f"Mic_{dev_idx}"))

            if not streams:
                print("No devices could be opened for multi-mic recording")
                self.last_error = "Opname mislukt: geen van de microfoons kon worden geopend"
                self._notify()
                return
            if failed:
                # Zichtbaar maken welke mic ontbreekt — anders mist een hele
                # positie stilletjes in de sampleset.
                self.last_error = "Let op: microfoon niet geopend: " + ", ".join(failed)
                self._notify()

            # Start all streams
            for stream in streams.values():
                stream.start()

            # Wait for recording duration
            start_time = time.time()
            while time.time() - start_time < self.record_seconds:
                if not self.is_running or self._abort_take:
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

            if not self.is_running or self._abort_take:
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
                    sub = sanitize_path_component(self.device_names.get(dev_idx, f"Mic_{dev_idx}"))
                    self._save_audio(audio_data, subdirectory=sub)
                except Exception as e:
                    print(f"Save error for device {dev_idx}: {e}")

            self.current_level = 0.0
            self.current_levels.clear()
            if not failed:
                self.last_error = ""

        except Exception as e:
            print(f"Multi-recording error: {e}")
            self.current_level = 0.0
            self.last_error = f"Multi-opname mislukt: {e}"
            self._notify()
            for stream in streams.values():
                try:
                    stream.stop()
                    stream.close()
                except:
                    pass
    
    # ─── Assistive intelligent recording (auto mode) ──────────

    def _to_mono_f(self, block):
        """Normalized float mono view of a raw audio block (for analysis)."""
        b = block.astype(np.float64)
        if self.bit_depth != 24:          # int16 samples
            b = b / 32768.0
        if b.ndim > 1:
            b = b.mean(axis=1)
        return b

    def _dominant_freq(self, mono, sr):
        """Dominant frequency (Hz) of a mono float window, ignoring sub-20 Hz."""
        n = len(mono)
        if n < 512:
            return 0.0
        w = mono * np.hanning(n)
        spec = np.abs(np.fft.rfft(w))
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        lo = int(np.searchsorted(freqs, 20.0))
        if lo >= len(spec):
            return 0.0
        i = lo + int(np.argmax(spec[lo:]))
        # Parabolische interpolatie over de piek-bins: zonder deze stap is het
        # ~11 Hz-binrooster (0.09 s venster) grover dan 100 cents onder ~90 Hz,
        # waardoor lage noten (o.a. MIDI 40/E2) de stemtoets nooit kunnen halen.
        if 0 < i < len(spec) - 1:
            a, b, c = float(spec[i - 1]), float(spec[i]), float(spec[i + 1])
            denom = a - 2.0 * b + c
            if abs(denom) > 1e-12:
                i = i + float(np.clip(0.5 * (a - c) / denom, -0.5, 0.5))
        return float(i * sr / n)

    def _trem_periodic(self, mono, sr):
        """Detect tremulant-style amplitude modulation (~4.5–8.5 Hz) in a mono window.
        Returns (is_periodic, depth) via a fine RMS envelope + autocorrelation."""
        frame = max(1, int(sr * 0.01))          # 10 ms envelope frames → ~100 Hz env rate
        n = (len(mono) // frame) * frame
        if n < frame * 20:
            return False, 0.0
        env = np.sqrt(np.mean(mono[:n].reshape(-1, frame) ** 2, axis=1))
        env_sr = sr / frame
        mean = float(np.mean(env))
        if mean <= 1e-9:
            return False, 0.0
        depth = float(np.std(env) / mean)
        e = env - np.mean(env)
        ac = np.correlate(e, e, mode='full')[len(e) - 1:]
        if ac[0] <= 0:
            return False, depth
        ac = ac / ac[0]
        lo, hi = int(env_sr / 8.5), int(env_sr / 4.5)
        if hi <= lo or hi >= len(ac):
            return False, depth
        peak = float(np.max(ac[lo:hi]))
        return (peak > 0.35 and 0.04 < depth < 0.8), depth

    def _do_record_auto(self):
        """Assistive intelligent capture for one note.

        Flow: run the countdown while sampling the room noise floor (streams
        already open, so the floor is genuinely pre-tone) → wait for the pipe to
        speak (onset) → wait until a stable, loopable sustain is captured (flat RMS +
        locked pitch, or a stable tremulant modulation) → raise the 'laat los' cue →
        capture the release tail down to silence → save. A max-duration cap (counted
        from onset) stops a non-stabilizing pipe, a waiting-timeout stops a silent
        take; Stop/Pauze/Volgende always override.
        """
        devs = list(self.device_indices) if self.device_indices else [self.device_index]
        primary = devs[0]

        sr, ch = self._resolve_record_params(primary)
        if sr != self.sample_rate or ch != self.channels:
            self.sample_rate, self.channels = sr, ch
            self._notify()
        sr, channels = self.sample_rate, self.channels
        dtype = 'float32' if self.bit_depth == 24 else 'int16'
        sens = max(0.3, min(3.0, self.auto_sensitivity))
        is_trem = bool(self.tremulant)

        # Expected sounding pitch: an N-foot register sounds a factor 8/N vs the
        # key (16' = octave down, 4' = octave up, mutations like "2 2/3" a quint).
        # Unparseable foot (mixtures like "4st") → amplitude-only stability check.
        f0 = 440.0 * (2.0 ** ((self.current_note - 69) / 12.0))
        pitch_check = True
        reg = self._find_register(self._find_keyboard(self.active_keyboard), self.active_register)
        ft = (str(reg.get("foot", "")).strip().replace(",", ".")
              .replace("'", "").replace("’", "").replace("′", "").strip()) if reg else ""
        if ft:
            try:
                parts = ft.split()
                val = float(parts[0])
                if len(parts) > 1 and "/" in parts[1]:
                    num, den = parts[1].split("/")
                    val += float(num) / float(den)
                f0 *= 8.0 / val
            except (ValueError, ZeroDivisionError):
                pitch_check = False

        buffers = {d: [] for d in devs}
        dropped = {d: 0 for d in devs}     # pre-onset samples discarded per device
        self.current_levels.clear()

        def make_cb(d):
            def cb(indata, frames, time_info, status):
                buffers[d].append(indata.copy())
            return cb

        streams = {}
        failed = []
        try:
            for d in devs:
                try:
                    streams[d] = sd.InputStream(device=d, samplerate=sr, channels=channels,
                                                dtype=dtype, callback=make_cb(d))
                except Exception as e:
                    print(f"Auto: could not open device {d}: {e}", flush=True)
                    failed.append(self.device_names.get(d, f"Mic_{d}"))
            if not streams:
                # Pauzeer de cyclus: anders marcheert hij foutend door alle noten.
                self.last_error = "Opname mislukt: geen microfoon kon worden geopend"
                self.state = "paused"
                self.is_running = False
                self._notify()
                return
            if failed:
                self.last_error = "Let op: microfoon niet geopend: " + ", ".join(failed)
                self._notify()
            if primary not in streams:
                primary = next(iter(streams))
            for s in streams.values():
                s.start()

            def primary_tail(seconds):
                """Recent normalized mono float window of `seconds` from the primary buffer."""
                if not buffers[primary]:
                    return np.zeros(0)
                want = int(sr * seconds)
                chunks, got = [], 0
                for arr in reversed(buffers[primary]):
                    chunks.append(arr)
                    got += len(arr)
                    if got >= want:
                        break
                block = np.concatenate(list(reversed(chunks)), axis=0)[-want:]
                return self._to_mono_f(block)

            def trim_pre_roll():
                """Pre-onset: keep only ~2 s per device so waiting can't grow RAM."""
                keep = int(sr * 2.0)
                for d in devs:
                    buf = buffers[d]
                    total = sum(len(a) for a in buf)
                    while len(buf) > 1 and total - len(buf[0]) >= keep:
                        total -= len(buf[0])
                        dropped[d] += len(buf[0])
                        buf.pop(0)

            LOOP = 0.05
            noise_rms = []

            def sample_noise(r):
                """Guarded floor sampling: once seeded, ignore tone-level frames so
                an early-pressed key can't poison the median."""
                if len(noise_rms) < 6 or r <= max(
                        float(np.median(noise_rms)) * 4.0 / sens, 0.01):
                    noise_rms.append(r)
                    if len(noise_rms) > 40:
                        noise_rms.pop(0)

            # ── Countdown (streams open → genuine pre-tone noise floor) ──
            for i in range(self.countdown_seconds, 0, -1):
                if not self.is_running or self._abort_take:
                    return
                self.countdown_value = i
                self._notify()
                t_end = time.time() + 1.0
                while time.time() < t_end:
                    time.sleep(LOOP)
                    w = primary_tail(0.093)
                    sample_noise(float(np.sqrt(np.mean(w ** 2))) if w.size else 0.0)
                    trim_pre_roll()
            # Stop/Pauze tijdens de laatste aftelseconde mag de door stop()
            # gezette state niet met "recording" overschrijven.
            if not self.is_running or self._abort_take:
                return
            self.countdown_value = 0
            self.state = "recording"

            # ── Detector state ──
            self.auto_phase = phase = "waiting"
            self.hold_release_cue = False
            self.stable_progress = 0.0
            self.noise_floor_rms = 0.0
            env = []
            onset_hits = release_hits = silent_hits = 0
            peak = stable_run = 0.0
            onset_by_dev = {}
            onset_time = None
            capped = False
            rel_thr = None
            wait_deadline = time.time() + max(30.0, 2.0 * self.max_record_seconds)
            # Continuous stable sustain required, scaled up for low notes (longer wavelength).
            need = max(self.min_stable_seconds, (80.0 / f0) if f0 > 0 else self.min_stable_seconds)
            self._notify()

            while self.is_running and not self._abort_take:
                time.sleep(LOOP)
                win = primary_tail(0.093)
                rms = float(np.sqrt(np.mean(win ** 2))) if win.size else 0.0
                self.current_level = min(1.0, rms * 3)
                over_cap = onset_time and (time.time() - onset_time) > self.max_record_seconds

                if phase == "waiting":
                    # A deferred pause may stop here directly: nothing was played yet.
                    if self.pause_requested:
                        self.pause_requested = False
                        self.is_running = False
                        self.state = "paused"
                        return
                    floor = float(np.median(noise_rms)) if len(noise_rms) >= 6 else 0.02
                    self.noise_floor_rms = floor
                    onset_thr = max(floor * 4.0 / sens, 0.01)
                    sample_noise(rms)
                    trim_pre_roll()
                    if len(noise_rms) >= 6 and rms > onset_thr:
                        onset_hits += 1
                        if onset_hits >= 3:                      # ~150 ms sustained → spoken
                            phase = self.auto_phase = "stabilizing"
                            # Onset-index per device (eigen tijdlijn): streams
                            # starten gestaffeld, dus één primary-teller zou de
                            # attack van latere mics afknippen.
                            onset_by_dev = {
                                d: max(0, dropped[d] + sum(len(a) for a in buffers[d])
                                       - int(sr * 0.20))
                                for d in streams}
                            onset_time = time.time()
                            env, stable_run, peak = [], 0.0, 0.0
                            self._notify()
                    else:
                        onset_hits = 0
                        if time.time() > wait_deadline:
                            self.last_error = ("Geen toon gedetecteerd — controleer de "
                                               "microfoon of speel pas na het aftellen")
                            self.state = "paused"
                            self.is_running = False
                            self._notify()
                            return

                elif phase == "stabilizing":
                    env.append(rms)
                    peak = max(peak, rms)
                    if is_trem:
                        stable, _ = self._trem_periodic(primary_tail(1.5), sr)
                    else:
                        stable = False
                        if len(env) >= 8:
                            w = np.array(env[-8:])
                            m = float(np.mean(w))
                            cov = float(np.std(w) / m) if m > 1e-9 else 1.0
                            ok_pitch = True
                            if pitch_check:
                                dom = self._dominant_freq(primary_tail(0.09), sr)
                                ok_pitch = False
                                if dom > 0 and f0 > 0:
                                    # Elke harmonische van de verwachte toon mag de
                                    # dominante partiaal zijn (quintadeen, gedekt).
                                    k = round(dom / f0)
                                    if 1 <= k <= 8:
                                        cents = abs(1200.0 * np.log2(dom / (k * f0)))
                                        ok_pitch = cents <= 100.0
                            stable = cov < (0.09 * sens) and ok_pitch
                    stable_run = stable_run + LOOP if stable else 0.0
                    self.stable_progress = max(0.0, min(1.0, stable_run / need))
                    if over_cap:
                        capped = True          # geen groene flits: direct afronden
                        break
                    if stable_run >= need:
                        phase = self.auto_phase = "hold"
                        self.hold_release_cue = True
                        # Release-drempel verankerd tussen ruisvloer en sustain-
                        # niveau, zodat ook zachte registers loslaten detecteren.
                        sustain_ref = float(np.median(env[-8:])) if len(env) >= 8 else peak
                        rel_thr = self.noise_floor_rms + 0.35 * max(
                            sustain_ref - self.noise_floor_rms, 0.0)
                        self._notify()

                elif phase == "hold":
                    peak = max(peak, rms)
                    if rms < (rel_thr if rel_thr is not None else peak * 0.35):
                        release_hits += 1
                        if release_hits >= 3:
                            phase = self.auto_phase = "release"
                            self.hold_release_cue = False
                            self._notify()
                    else:
                        release_hits = 0
                    if over_cap:
                        capped = True
                        break

                elif phase == "release":
                    # Stilte-drempel relatief aan de toonpiek (gain-invariant),
                    # met de ruisvloer als ondergrens zodat hij altijd termineert.
                    if rms <= max(self.noise_floor_rms * 1.5, peak * 0.02, 0.0015):
                        silent_hits += 1
                        if silent_hits >= 6:                      # ~300 ms silence → tail done
                            break
                    else:
                        silent_hits = 0
                    if over_cap:
                        capped = True
                        break

                self._notify()

            # ── Stop & save ──
            for s in streams.values():
                try:
                    s.stop(); s.close()
                except Exception:
                    pass

            if not self.is_running or self._abort_take:
                return

            for d in streams:
                try:
                    if not buffers[d]:
                        continue
                    audio = np.concatenate(buffers[d], axis=0)
                    trim = max(0, onset_by_dev.get(d, 0) - dropped[d])
                    if trim and trim < len(audio):
                        audio = audio[trim:]
                    if len(devs) > 1:
                        self._save_audio(audio, subdirectory=sanitize_path_component(
                            self.device_names.get(d, f"Mic_{d}")))
                    else:
                        self._save_audio(audio)
                except Exception as e:
                    print(f"Auto save error (device {d}): {e}", flush=True)
            if capped:
                self.last_error = (f"Maximumduur bereikt ({int(self.max_record_seconds)}s): "
                                   f"{midi_to_filename(self.current_note)} is zonder uitklank "
                                   "opgeslagen — controleer deze opname")
            elif not failed:
                self.last_error = ""

        except Exception as e:
            print(f"Auto recording error: {e}", flush=True)
            import traceback; traceback.print_exc()
            self.last_error = f"Intelligente opname mislukt: {e}"
            # Pauzeren: anders schuift de cyclus door en wordt deze noot
            # stilletjes overgeslagen zonder opname.
            self.state = "paused"
            self.is_running = False
        finally:
            for s in streams.values():
                try:
                    s.stop(); s.close()
                except Exception:
                    pass
            self.auto_phase = "idle"
            self.hold_release_cue = False
            self.stable_progress = 0.0
            self.current_level = 0.0
            self.current_levels.clear()
            self._notify()

    def _save_audio(self, audio_data, subdirectory=None):
        """Save recorded audio in chosen format (mp3/wav/flac)."""
        path = self.get_current_register_path()
        if subdirectory:
            path = os.path.join(path, subdirectory)
        # Ensure the target folder exists (covers bass/treble split subfolders too)
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
        if self.record_mode == "auto":
            # Auto-opnames hebben variabele duur; alleen echt te korte fragmenten melden.
            if duration < self.min_stable_seconds:
                return {"issue": "short", "detail": f"Te kort: {duration:.1f}s (minimaal {self.min_stable_seconds:.1f}s stabiel verwacht)", "severity": "warning"}
            return None
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
        self.pause_requested = False
        self.state = "idle"
        self.current_level = 0.0
        self.current_levels.clear()
        try:
            sd.stop()
        except:
            pass
        self._notify()

    def pause(self):
        """Pause after the current recording (a running take is finished and saved)."""
        if self.state == "recording" and self.is_running:
            self.pause_requested = True
        else:
            self.is_running = False
            self.state = "paused"
            self.current_level = 0.0
            self.current_levels.clear()
        self._notify()

    def next_note(self):
        """Move to next note (aborts a running take unsaved)."""
        if self.current_note < self.end_note:
            with self.lock:   # samen met de read-and-clear in _recording_cycle
                self._abort_take = True
                self.current_note += 1
            self._notify()

    def prev_note(self):
        """Move to previous note (aborts a running take unsaved)."""
        if self.current_note > self.start_note:
            with self.lock:
                self._abort_take = True
                self.current_note -= 1
            self._notify()

    def redo_note(self):
        """Re-record current note (don't advance)."""
        self.auto_advance = False
        self.start_recording_cycle()

    def set_note(self, midi_num):
        """Jump to specific note (aborts a running take unsaved)."""
        if self.start_note <= midi_num <= self.end_note:
            with self.lock:
                self._abort_take = True
                self.current_note = midi_num
            self._notify()
    
    def _ensure_folders(self, keyboard, register_name, tremulant=False, bass_treble=False):
        """Create the folder(s) for a register series on disk (idempotent).
        Handles the _trem variant, _bas/_dis split and multi-mic subfolders."""
        reg_name = sanitize_path_component(register_name)
        if tremulant and not reg_name.endswith("_trem"):
            reg_name += "_trem"
        base = os.path.join(self.output_dir,
                            sanitize_path_component(self.project_name),
                            sanitize_path_component(keyboard),
                            reg_name)
        targets = []
        if bass_treble:
            if self.split_record_bas:
                targets.append(os.path.join(base, reg_name + "_bas"))
            if self.split_record_disc:
                targets.append(os.path.join(base, reg_name + "_dis"))
            if not targets:
                targets.append(base)
        else:
            targets.append(base)
        for t in targets:
            os.makedirs(t, exist_ok=True)
            if len(self.device_indices) > 1:
                for idx in self.device_indices:
                    mic = sanitize_path_component(self.device_names.get(idx, f"Mic_{idx}"))
                    os.makedirs(os.path.join(t, mic), exist_ok=True)

    def new_register(self, register_name, tremulant=False):
        """Start a new register (legacy / master-side ad-hoc creation)."""
        self.stop()
        self.register_name = register_name
        self.tremulant = tremulant
        self.current_note = self.start_note
        self._ensure_folders(self.current_keyboard, register_name,
                             tremulant, self.bass_treble_split)
        self._notify()
        return self.get_current_register_path()

    def select_register(self, keyboard, register_name, variant="normal"):
        """Load a defined register series as the active recording target.
        Drives the existing recording-cycle inputs; the cycle itself is untouched."""
        self.stop()
        kb = self._find_keyboard(keyboard)
        reg = self._find_register(kb, register_name)
        if not reg:
            return False
        self.current_keyboard = self.active_keyboard = keyboard
        self.register_name = self.active_register = register_name
        self.active_variant = "trem" if variant == "trem" else "normal"
        self.tremulant = (self.active_variant == "trem")
        self.start_note = int(reg["begin_note"])
        self.end_note = int(reg["end_note"])
        self.current_note = self.start_note
        self.bass_treble_split = bool(reg.get("bass_treble", False))
        self._ensure_folders(keyboard, register_name, self.tremulant, self.bass_treble_split)
        self._notify()
        return True

    def _count_samples_in(self, base):
        """Count distinct recorded notes under base. Recurses into _bas/_dis and
        mic subfolders, counting unique note basenames so multi-mic / split don't
        inflate the count."""
        exts = ('.mp3', '.wav', '.flac')
        if not os.path.isdir(base):
            return 0
        notes = set()

        def collect(folder):
            try:
                entries = os.listdir(folder)
            except OSError:
                return
            for e in entries:
                full = os.path.join(folder, e)
                if os.path.isfile(full) and e.lower().endswith(exts):
                    notes.add(os.path.splitext(e)[0])
                elif os.path.isdir(full):
                    collect(full)

        collect(base)
        return len(notes)

    def register_progress(self, keyboard, register_name, variant="normal"):
        """Return {recorded, expected} for one register series."""
        reg = self._find_register(self._find_keyboard(keyboard), register_name)
        if not reg:
            return {"recorded": 0, "expected": 0}
        reg_name = sanitize_path_component(register_name)
        if variant == "trem" and not reg_name.endswith("_trem"):
            reg_name += "_trem"
        base = os.path.join(self.output_dir,
                            sanitize_path_component(self.project_name),
                            sanitize_path_component(keyboard),
                            reg_name)
        expected = int(reg["end_note"]) - int(reg["begin_note"]) + 1
        return {"recorded": self._count_samples_in(base), "expected": expected}

    # ─── Organ plan: commit / persist / load ───────────────

    def _apply_settings_dict(self, s):
        """Apply a settings dict (subset of /api/settings keys) to the engine."""
        if not s:
            return
        if 'sample_rate' in s: self.sample_rate = int(s['sample_rate'])
        if 'bit_depth' in s: self.bit_depth = int(s['bit_depth'])
        if 'channels' in s: self.channels = int(s['channels'])
        if s.get('output_format') in ('mp3', 'wav', 'flac'):
            self.output_format = s['output_format']
        if 'mp3_bitrate' in s: self.mp3_bitrate = int(s['mp3_bitrate'])
        if 'countdown_seconds' in s: self.countdown_seconds = int(s['countdown_seconds'])
        if 'record_seconds' in s: self.record_seconds = int(s['record_seconds'])
        if 'record_gain' in s: self.record_gain = max(0.0, min(2.0, float(s['record_gain'])))
        if s.get('record_mode') in ('fixed', 'auto'): self.record_mode = s['record_mode']
        if s.get('min_stable_seconds') is not None: self.min_stable_seconds = max(0.5, min(10.0, float(s['min_stable_seconds'])))
        if s.get('max_record_seconds') is not None: self.max_record_seconds = max(3.0, min(60.0, float(s['max_record_seconds'])))
        if s.get('auto_sensitivity') is not None: self.auto_sensitivity = max(0.3, min(3.0, float(s['auto_sensitivity'])))
        if 'device_indices' in s:
            self.device_indices = [int(i) for i in s['device_indices']] if s['device_indices'] else []
        if 'device_names' in s and isinstance(s['device_names'], dict):
            self.device_names = {int(k): v for k, v in s['device_names'].items()}
        if 'input_mode' in s:
            self.input_mode = s['input_mode'] if s['input_mode'] in ('mic', 'loopback') else 'mic'
        if 'loopback_device_id' in s:
            self.loopback_device_id = s['loopback_device_id']

    def _keyboard_entries(self):
        """All keyboards incl. a synthetic Pedaal entry (backed by pedal_registers)."""
        entries = [(kb["name"], kb) for kb in self.keyboards]
        if self.has_pedal:
            entries.append(("Pedaal", {"name": "Pedaal", "zwelwerk": False,
                                       "tremulant": False, "registers": self.pedal_registers}))
        return entries

    @staticmethod
    def _series_status(recorded, expected, checked):
        """Colour status: todo(red) / partial(orange) / review(purple) / done(green)."""
        if expected <= 0 or recorded <= 0:
            return "todo"
        if recorded < expected:
            return "partial"
        return "done" if checked else "review"

    def _series_entry(self, kb_name, reg, variant):
        prog = self.register_progress(kb_name, reg["name"], variant)
        checked = bool(reg.get("checked", {}).get(variant, False))
        folder = reg["name"] + ("_trem" if variant == "trem" else "")
        return dict(variant=variant, folder=folder, checked=checked,
                    status=self._series_status(prog["recorded"], prog["expected"], checked),
                    **prog)

    def build_plan(self):
        """Per-keyboard register list with per-series recorded/expected progress."""
        plan = []
        for name, kb in self._keyboard_entries():
            regs = []
            for r in kb.get("registers", []):
                series = [self._series_entry(name, r, "normal")]
                if kb.get("tremulant"):
                    series.append(self._series_entry(name, r, "trem"))
                regs.append({
                    "name": r["name"], "display": r.get("display", r["name"]),
                    "foot": r.get("foot", ""), "begin_note": r["begin_note"],
                    "end_note": r["end_note"], "bass_treble": r.get("bass_treble", False),
                    "series": series,
                })
            plan.append({"name": name, "zwelwerk": kb.get("zwelwerk", False),
                         "tremulant": kb.get("tremulant", False), "registers": regs})
        return plan

    def mark_register_checked(self, keyboard, register_name, variant, checked):
        """Mark a register series as checked ('gecontroleerd') → green when full."""
        reg = self._find_register(self._find_keyboard(keyboard), register_name)
        if not reg:
            return False
        if not isinstance(reg.get("checked"), dict):
            reg["checked"] = {}
        reg["checked"]["trem" if variant == "trem" else "normal"] = bool(checked)
        self._refresh_plan_cache()
        self.save_manifest()
        self._notify()
        return True

    def get_plan_cached(self):
        """Return the last computed plan/progress instantly (no disk I/O).
        The cache is refreshed by the background _plan_refresh_loop and by
        user actions (commit/add/edit/remove/mark/load)."""
        return self._plan_cache if self._plan_cache is not None else []

    def _refresh_plan_cache(self):
        """Rebuild the plan cache now (used after user actions for instant UI)."""
        try:
            self._plan_cache = self.build_plan()
        except Exception:
            pass

    def build_manifest(self):
        """Full serializable project plan (no progress) for the manifest file."""
        return {
            "jm_rec_version": JM_REC_VERSION,
            "organ": self.project_name,
            "plaats": self.plaats, "kerk": self.kerk, "bouwer": self.bouwer,
            "tremulant_scope": self.tremulant_scope,
            "has_pedal": self.has_pedal,
            "split_note": self.split_note,
            "split_record_bas": self.split_record_bas,
            "split_record_disc": self.split_record_disc,
            "couplers": self.couplers,
            "keyboards": self.keyboards,
            "pedal_registers": self.pedal_registers,
            "settings": {
                "sample_rate": self.sample_rate, "bit_depth": self.bit_depth,
                "channels": self.channels, "output_format": self.output_format,
                "mp3_bitrate": self.mp3_bitrate, "countdown_seconds": self.countdown_seconds,
                "record_seconds": self.record_seconds, "record_gain": self.record_gain,
                "record_mode": self.record_mode,
                "min_stable_seconds": self.min_stable_seconds,
                "max_record_seconds": self.max_record_seconds,
                "auto_sensitivity": self.auto_sensitivity,
                "device_indices": list(self.device_indices),
                "device_names": {str(k): v for k, v in self.device_names.items()},
                "input_mode": self.input_mode, "loopback_device_id": self.loopback_device_id,
            },
        }

    # ─── .organ (ODF) export voor JM-Orgue ─────────────────

    @staticmethod
    def _foot_to_harmonic(foot):
        """GrandOrgue HarmonicNumber uit een voetmaat-string: 8'→8, 16'→4,
        4'→16, "2 2/3"→24 (harmonic = 64/voet). Niet-parseerbaar (mixturen
        zoals "4st", leeg) → 8 (unison)."""
        # Voetteken-apostrofs strippen: de wizard-hint suggereert zelf "8'".
        ft = (str(foot or "").strip().replace(",", ".")
              .replace("'", "").replace("’", "").replace("′", "").strip())
        if not ft:
            return 8
        try:
            parts = ft.split()
            val = float(parts[0])
            if len(parts) > 1 and "/" in parts[1]:
                num, den = parts[1].split("/")
                val += float(num) / float(den)
            if val <= 0:
                return 8
            return max(1, int(round(64.0 / val)))
        except (ValueError, ZeroDivisionError):
            return 8

    def _index_register_samples(self, reg_dir):
        """Map bestandsnaam → relatieve paden (t.o.v. reg_dir) van alle samples
        in een registermap, incl. _bas/_dis-splitsen en multi-mic-submappen."""
        found = {}
        if not os.path.isdir(reg_dir):
            return found
        for root, _dirs, files in os.walk(reg_dir):
            rel_root = os.path.relpath(root, reg_dir)
            for fn in files:
                if fn.lower().endswith(('.wav', '.flac', '.mp3')):
                    rel = fn if rel_root == "." else os.path.join(rel_root, fn)
                    found.setdefault(fn.lower(), []).append(rel)
        return found

    def _pick_pipe_sample(self, index, midi_num, mic_pref, split_on=False):
        """Kies het beste sample-pad voor een noot uit de map-index.
        Voorkeur: wav > flac > mp3; bij bas/discant-splitsing de juiste
        _bas/_dis-map voor deze noot (verkeerde helft en verouderde
        root-takes verliezen); kortste pad boven submappen; bij multi-mic
        de eerst geconfigureerde positie (hoofdletterongevoelig)."""
        base = midi_to_filename(midi_num).lower()
        want = (("_dis" if midi_num >= self.split_note else "_bas")
                if split_on else None)
        for ext in ('.wav', '.flac', '.mp3'):
            paths = index.get(base + ext)
            if not paths:
                continue

            def score(rel):
                parts = rel.replace('\\', '/').split('/')
                depth = len(parts) - 1
                dirs = [p.lower() for p in parts[:-1]]
                in_split = any(d.endswith(('_bas', '_dis')) for d in dirs)
                if want:    # juiste helft eerst, dan root, dan verkeerde helft
                    split_rank = (0 if any(d.endswith(want) for d in dirs)
                                  else (2 if in_split else 1))
                else:       # geen splitsing actief: root wint van oude splitmappen
                    split_rank = 1 if in_split else 0
                parts_cf = [p.casefold() for p in parts]
                mic_rank = len(mic_pref)
                for i, m in enumerate(mic_pref):
                    if m.casefold() in parts_cf:
                        mic_rank = i
                        break
                return (split_rank, depth, mic_rank, rel.lower())
            return sorted(paths, key=score)[0]
        return None

    def export_organ_odf(self):
        """Schrijf een GrandOrgue-compatibel .organ-bestand (voor JM-Orgue) op
        basis van het huidige plan + de samples op schijf.

        Afgestemd op JM-Orgue's ODF-parser: Manual000 = pedaal, stops per
        divisie genummerd (pedaal 001+, klavier i → i*100+1), pipe-mapping via
        FirstAccessiblePipeLogicalKeyNumber, zwelkast via [Enclosure] +
        windchest-Name = klaviernaam, tremulant via [Tremulant]-referentie op
        het manual. Paden relatief t.o.v. het .organ-bestand (projectmap).
        """
        if not self.project_name:
            raise ValueError("Geen project ingesteld")
        base = os.path.join(self.output_dir, self.project_name)
        if not os.path.isdir(base):
            raise ValueError(f"Projectmap niet gevonden: {base}")

        # Divisies in manual-volgorde: Pedaal = Manual000, klavieren 001..N.
        divisions = []          # (manual_nr, naam, kb_dict)
        if self.has_pedal:
            divisions.append((0, "Pedaal", {"name": "Pedaal", "zwelwerk": False,
                                            "tremulant": False,
                                            "registers": list(self.pedal_registers)}))
        for i, kb in enumerate(list(self.keyboards)):
            divisions.append((i + 1, kb["name"], kb))

        mic_pref = [sanitize_path_component(self.device_names.get(i, f"Mic_{i}"))
                    for i in self.device_indices]

        enclosures = []     # (id, divisie-naam)
        tremulants = []     # (id, divisie-naam)
        windchests = []     # (id, divisie-naam, enclosure_id|None, tremulant_id|None)
        stops = []          # (id, naam, harmonic, wc_id, first_pipe_key, [(nr, relpad|None, verwacht_pad)])
        manual_sections = []
        pipes_found = pipes_missing = flac_pipes = 0
        default_ext = {'wav': '.wav', 'flac': '.flac'}.get(self.output_format, '.mp3')

        for div_idx, (m_nr, name, kb) in enumerate(divisions):
            wc_id = div_idx + 1
            enc_id = trem_id = None
            if kb.get("zwelwerk"):
                enc_id = len(enclosures) + 1
                enclosures.append((enc_id, name))
            if kb.get("tremulant"):
                trem_id = len(tremulants) + 1
                tremulants.append((trem_id, name))
            windchests.append((wc_id, name, enc_id, trem_id))

            # Momentopname van registers + nootranges: master-edits kunnen
            # tegelijk binnenkomen (Flask threaded) en anders zouden man_first
            # en de pipe-range uit verschillende versies kunnen komen.
            regs = list(kb.get("registers", []))
            ranges = [(int(r["begin_note"]), int(r["end_note"])) for r in regs]
            firsts = [b for b, _e in ranges] or [self.start_note]
            lasts = [e for _b, e in ranges] or [self.end_note]
            man_first, man_last = min(firsts), max(lasts)

            stop_ids = []
            for r_idx, reg in enumerate(regs):
                r_begin, r_end = ranges[r_idx]
                stop_id = (m_nr * 100 if m_nr else 0) + r_idx + 1
                reg_dir = os.path.join(base, name, reg["name"])
                index = self._index_register_samples(reg_dir)
                pipes = []
                for note in range(r_begin, r_end + 1):
                    rel = self._pick_pipe_sample(index, note, mic_pref,
                                                 bool(reg.get("bass_treble")))
                    if rel:
                        pipes_found += 1
                        if rel.lower().endswith('.flac'):
                            flac_pipes += 1
                        rel_full = os.path.join(name, reg["name"], rel)
                    else:
                        pipes_missing += 1
                        rel_full = os.path.join(name, reg["name"],
                                                midi_to_filename(note) + default_ext)
                    pipes.append((note, rel is not None, rel_full))
                stops.append((stop_id, reg.get("display", reg["name"]),
                              self._foot_to_harmonic(reg.get("foot")), wc_id,
                              r_begin - man_first + 1, pipes))
                stop_ids.append(stop_id)

            manual_sections.append((m_nr, name, man_first,
                                    man_last - man_first + 1, stop_ids, trem_id))

        # Koppels: {source S, target T} = "S aan T" → sectie onder manual van T,
        # DestinationManual = manualnummer van S.
        name_to_manual = {name: m_nr for m_nr, name, _kb in divisions}
        couplers = []       # (id, naam, onder_manual, destination_manual)
        for c in list(self.couplers):
            # Vangnet voor oudere manifests met nog niet-gesaneerde namen.
            src = sanitize_path_component(c.get("source") or "")
            tgt = sanitize_path_component(c.get("target") or "")
            if src in name_to_manual and tgt in name_to_manual and src != tgt:
                couplers.append((len(couplers) + 1, f"{src} - {tgt}",
                                 name_to_manual[tgt], name_to_manual[src]))

        # ── ODF-tekst opbouwen ──
        L = []
        L.append(f"; {self.project_name}.organ — gegenereerd door JM-Rec v{JM_REC_VERSION}")
        L.append("")
        L.append("[Organ]")
        L.append(f"ChurchName={self.kerk or self.project_name}")
        L.append(f"ChurchAddress={self.plaats}")
        L.append(f"OrganBuilder={self.bouwer}")
        L.append(f"OrganComments=Opgenomen met JM-Rec v{JM_REC_VERSION}")
        L.append(f"RecordingDetails={self.sample_rate} Hz, {self.bit_depth}-bit, "
                 f"{self.output_format}")
        L.append(f"NumberOfManuals={len(self.keyboards)}")
        L.append(f"HasPedals={'Y' if self.has_pedal else 'N'}")
        L.append(f"NumberOfEnclosures={len(enclosures)}")
        L.append(f"NumberOfTremulants={len(tremulants)}")
        L.append(f"NumberOfWindchestGroups={len(windchests)}")
        L.append("")

        for wc_id, wname, enc_id, trem_id in windchests:
            L.append(f"[WindchestGroup{wc_id:03d}]")
            L.append(f"Name={wname}")
            L.append(f"NumberOfEnclosures={1 if enc_id else 0}")
            if enc_id:
                L.append(f"Enclosure001={enc_id:03d}")
            L.append(f"NumberOfTremulants={1 if trem_id else 0}")
            if trem_id:
                L.append(f"Tremulant001={trem_id:03d}")
            L.append("")

        for enc_id, ename in enclosures:
            L.append(f"[Enclosure{enc_id:03d}]")
            L.append(f"Name=Zwelkast {ename}")
            L.append("AmpMinimumLevel=20")
            L.append(f"MIDIInputNumber={enc_id}")
            L.append("")

        for trem_id, tname in tremulants:
            L.append(f"[Tremulant{trem_id:03d}]")
            L.append(f"Name=Tremulant {tname}")
            L.append("Period=160")
            L.append("AmpModDepth=18")
            L.append("StartRate=8")
            L.append("StopRate=8")
            L.append("")

        for m_nr, mname, man_first, n_keys, stop_ids, trem_id in manual_sections:
            L.append(f"[Manual{m_nr:03d}]")
            L.append(f"Name={mname}")
            L.append(f"MIDIInputNumber={m_nr + 1}")
            L.append(f"NumberOfLogicalKeys={n_keys}")
            L.append(f"NumberOfAccessibleKeys={n_keys}")
            L.append(f"FirstAccessibleKeyMIDINoteNumber={man_first}")
            L.append(f"NumberOfStops={len(stop_ids)}")
            for i, sid in enumerate(stop_ids):
                L.append(f"Stop{i + 1:03d}={sid:03d}")
            m_couplers = [c for c in couplers if c[2] == m_nr]
            L.append(f"NumberOfCouplers={len(m_couplers)}")
            for i, c in enumerate(m_couplers):
                L.append(f"Coupler{i + 1:03d}={c[0]:03d}")
            L.append(f"NumberOfTremulants={1 if trem_id else 0}")
            if trem_id:
                L.append(f"Tremulant001={trem_id:03d}")
            L.append("")

        for stop_id, sname, harmonic, wc_id, first_key, pipes in stops:
            L.append(f"[Stop{stop_id:03d}]")
            L.append(f"Name={sname}")
            L.append(f"HarmonicNumber={harmonic}")
            L.append(f"WindchestGroup={wc_id}")
            L.append(f"FirstAccessiblePipeLogicalKeyNumber={first_key}")
            L.append(f"NumberOfLogicalPipes={len(pipes)}")
            L.append(f"NumberOfAccessiblePipes={len(pipes)}")
            L.append("AmplitudeLevel=100")
            L.append("Percussive=N")
            for i, (_note, _found, rel_path) in enumerate(pipes):
                L.append("Pipe%03d=%s" % (i + 1, rel_path.replace('/', '\\')))
            L.append("")

        for c_id, c_name, _under, dest in couplers:
            L.append(f"[Coupler{c_id:03d}]")
            L.append(f"Name={c_name}")
            L.append(f"DestinationManual={dest}")
            L.append("DestinationKeyshift=0")
            L.append("UnisonOff=N")
            L.append("CouplerType=Normal")
            L.append("")

        odf_path = os.path.join(base, self.project_name + ".organ")
        with open(odf_path, 'w', encoding='utf-8', newline='\r\n') as f:
            f.write("\n".join(L))

        return {
            "path": odf_path,
            "stops": len(stops),
            "pipes_found": pipes_found,
            "pipes_missing": pipes_missing,
            "couplers": len(couplers),
            "enclosures": len(enclosures),
            "tremulants": len(tremulants),
            # JM-Orgue kan (nog) geen FLAC decoderen — waarschuw de gebruiker.
            "flac_warning": flac_pipes > 0,
        }

    def _select_first_register(self):
        """Pick the first defined register as the active series (no auto-start)."""
        for name, kb in self._keyboard_entries():
            if kb.get("registers"):
                self.select_register(name, kb["registers"][0]["name"], "normal")
                return
        # Nothing defined yet
        self.active_keyboard = self.current_keyboard = (
            self._keyboard_entries()[0][0] if self._keyboard_entries() else "")
        self.active_register = self.register_name = ""

    def _build_folders(self):
        """(Re)create the full folder tree for the current plan (idempotent)."""
        base = os.path.join(self.output_dir, self.project_name)
        os.makedirs(base, exist_ok=True)
        for name, kb in self._keyboard_entries():
            os.makedirs(os.path.join(base, name), exist_ok=True)
            for r in kb.get("registers", []):
                self._ensure_folders(name, r["name"], False, r.get("bass_treble", False))
                if kb.get("tremulant"):
                    self._ensure_folders(name, r["name"], True, r.get("bass_treble", False))

    def commit_organ(self, plan):
        """Accept the full wizard plan, build folders, persist and select first register."""
        self.stop()
        self.plaats = (plan.get("plaats") or "").strip()
        self.kerk = (plan.get("kerk") or "").strip()
        self.bouwer = (plan.get("bouwer") or "").strip()
        self.tremulant_scope = plan.get("tremulant_scope", "none")
        folder = plan.get("folder_name") or default_folder_code(self.plaats, self.bouwer)
        self.project_name = sanitize_path_component(folder) or "Orgel"
        if plan.get("output_dir"):
            self.output_dir = normalize_output_dir(plan["output_dir"])
        self.has_pedal = bool(plan.get("has_pedal", False))
        self.split_note = int(plan.get("split_note", self.split_note))
        self.split_record_bas = bool(plan.get("split_record_bas", True))
        self.split_record_disc = bool(plan.get("split_record_disc", True))
        self.keyboards = self._normalize_keyboards(plan.get("keyboards", []))
        for kb in self.keyboards:
            kb["name"] = sanitize_path_component(kb["name"])
            if self.tremulant_scope == "organ":
                kb["tremulant"] = True
            elif self.tremulant_scope == "none":
                kb["tremulant"] = False
        self.pedal_registers = self._normalize_registers(plan.get("pedal_registers", []))
        # Koppelnamen dezelfde sanitize geven als de klaviernamen hierboven,
        # anders matcht een koppel zijn klavier niet meer (bv. bij een spatie).
        self.couplers = [{"source": sanitize_path_component(c.get("source", "")),
                          "target": sanitize_path_component(c.get("target", ""))}
                         for c in (plan.get("couplers") or [])
                         if c.get("source") and c.get("target")]
        self._apply_settings_dict(plan.get("settings") or {})
        self._build_folders()
        self._select_first_register()
        self._refresh_plan_cache()
        self.save_manifest()
        self._remember_last_project()
        self._notify()
        return os.path.join(self.output_dir, self.project_name)

    def add_register(self, keyboard, display, foot="", begin_note=None,
                     end_note=None, bass_treble=False):
        """Master-side: add a register to a keyboard (or Pedaal)."""
        reg = self._normalize_registers([{
            "display": display, "foot": foot,
            "begin_note": begin_note if begin_note is not None else self.start_note,
            "end_note": end_note if end_note is not None else self.end_note,
            "bass_treble": bass_treble,
        }])
        if not reg:
            return False
        reg = reg[0]
        kb = self._find_keyboard(keyboard)
        if kb is None:
            return False
        if self._find_register(kb, reg["name"]):
            return False  # already exists
        kb["registers"].append(reg)
        self._ensure_folders(keyboard, reg["name"], False, reg["bass_treble"])
        if kb.get("tremulant"):
            self._ensure_folders(keyboard, reg["name"], True, reg["bass_treble"])
        self._refresh_plan_cache()
        self.save_manifest()
        self._notify()
        return True

    def edit_register(self, keyboard, register_name, **fields):
        """Master-side: edit register fields (foot/begin/end/bass_treble/display)."""
        reg = self._find_register(self._find_keyboard(keyboard), register_name)
        if not reg:
            return False
        if 'foot' in fields: reg['foot'] = str(fields['foot'])
        if 'display' in fields and fields['display']: reg['display'] = fields['display']
        if 'begin_note' in fields: reg['begin_note'] = int(fields['begin_note'])
        if 'end_note' in fields: reg['end_note'] = int(fields['end_note'])
        if reg['end_note'] < reg['begin_note']:
            reg['begin_note'], reg['end_note'] = reg['end_note'], reg['begin_note']
        if 'bass_treble' in fields: reg['bass_treble'] = bool(fields['bass_treble'])
        self._refresh_plan_cache()
        self.save_manifest()
        self._notify()
        return True

    def remove_register(self, keyboard, register_name):
        """Master-side: remove a register from the plan (audio on disk left intact)."""
        kb = self._find_keyboard(keyboard)
        if not kb:
            return False
        before = len(kb["registers"])
        kb["registers"][:] = [r for r in kb["registers"] if r["name"] != register_name]
        if len(kb["registers"]) == before:
            return False
        self._refresh_plan_cache()
        self.save_manifest()
        self._notify()
        return True

    def save_manifest(self):
        """Persist the full plan to <output>/<project>/<project>.jm-rec.json."""
        if not self.project_name:
            return
        try:
            base = os.path.join(self.output_dir, self.project_name)
            os.makedirs(base, exist_ok=True)
            path = os.path.join(base, self.project_name + ".jm-rec.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.build_manifest(), f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"save_manifest error: {e}", flush=True)

    def _remember_last_project(self):
        try:
            ptr = _last_project_pointer_path()
            os.makedirs(os.path.dirname(ptr), exist_ok=True)
            manifest_path = os.path.join(self.output_dir, self.project_name,
                                         self.project_name + ".jm-rec.json")
            with open(ptr, "w", encoding="utf-8") as f:
                json.dump({"path": manifest_path, "organ": self.project_name,
                           "plaats": self.plaats, "kerk": self.kerk,
                           "bouwer": self.bouwer}, f, ensure_ascii=False)
        except Exception as e:
            print(f"remember_last_project error: {e}", flush=True)

    def _load_from_manifest(self, m):
        self.plaats = m.get("plaats", "")
        self.kerk = m.get("kerk", "")
        self.bouwer = m.get("bouwer", "")
        self.tremulant_scope = m.get("tremulant_scope", "none")
        self.has_pedal = bool(m.get("has_pedal", False))
        self.split_note = int(m.get("split_note", self.split_note))
        self.split_record_bas = bool(m.get("split_record_bas", True))
        self.split_record_disc = bool(m.get("split_record_disc", True))
        self.keyboards = self._normalize_keyboards(m.get("keyboards", []))
        self.pedal_registers = self._normalize_registers(m.get("pedal_registers", []))
        self.couplers = m.get("couplers", []) or []
        self._apply_settings_dict(m.get("settings") or {})

    def _infer_plan_from_disk(self, folder):
        """Build a minimal plan by scanning an existing project folder (no manifest)."""
        exts = ('.mp3', '.wav', '.flac')

        def note_range(regfolder):
            lo, hi = None, None
            for root, _dirs, files in os.walk(regfolder):
                for fn in files:
                    if fn.lower().endswith(exts):
                        try:
                            n = int(fn.split('-')[0])
                        except (ValueError, IndexError):
                            continue
                        lo = n if lo is None else min(lo, n)
                        hi = n if hi is None else max(hi, n)
            return lo, hi

        keyboards = []
        pedal_regs = []
        has_pedal = False
        for kb_entry in sorted(os.listdir(folder)):
            kb_path = os.path.join(folder, kb_entry)
            if not os.path.isdir(kb_path):
                continue
            regs = []
            kb_trem = False
            for reg_entry in sorted(os.listdir(kb_path)):
                reg_path = os.path.join(kb_path, reg_entry)
                if not os.path.isdir(reg_path):
                    continue
                is_trem = reg_entry.endswith("_trem")
                base_name = reg_entry[:-5] if is_trem else reg_entry
                if is_trem:
                    kb_trem = True
                    if any(r["name"] == base_name for r in regs):
                        continue
                lo, hi = note_range(reg_path)
                bass_treble = any(d.endswith(("_bas", "_dis"))
                                  for d in os.listdir(reg_path)
                                  if os.path.isdir(os.path.join(reg_path, d)))
                regs.append({
                    "name": base_name, "display": base_name, "foot": "",
                    "begin_note": lo if lo is not None else self.start_note,
                    "end_note": hi if hi is not None else self.end_note,
                    "bass_treble": bass_treble,
                })
            if kb_entry == "Pedaal":
                has_pedal = True
                pedal_regs = self._normalize_registers(regs)
            else:
                keyboards.append({"name": kb_entry, "zwelwerk": False,
                                  "tremulant": kb_trem,
                                  "registers": self._normalize_registers(regs)})
        self.keyboards = keyboards
        self.pedal_registers = pedal_regs
        self.has_pedal = has_pedal
        self.tremulant_scope = "keyboard"

    def load_project(self, path):
        """Load a project from a manifest file or a project folder. Rebuilds state
        and reconciles (recreates) any missing folders. Audio is left intact."""
        manifest = None
        if os.path.isfile(path):
            folder = os.path.dirname(path)
            try:
                with open(path, encoding="utf-8") as f:
                    manifest = json.load(f)
            except Exception:
                manifest = None
        elif os.path.isdir(path):
            folder = path
            for fn in os.listdir(folder):
                if fn.endswith(".jm-rec.json"):
                    try:
                        with open(os.path.join(folder, fn), encoding="utf-8") as f:
                            manifest = json.load(f)
                        break
                    except Exception:
                        pass
        else:
            return False
        self.output_dir = os.path.dirname(folder)
        self.project_name = sanitize_path_component(os.path.basename(folder))
        if manifest:
            self._load_from_manifest(manifest)
        else:
            self._infer_plan_from_disk(folder)
        self._build_folders()
        self._select_first_register()
        self._refresh_plan_cache()
        self._remember_last_project()
        self._notify()
        return True

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
                'last_error': self.last_error,
                'has_manifest': bool(self.project_name),
                'organ_meta': {
                    'plaats': self.plaats, 'kerk': self.kerk,
                    'bouwer': self.bouwer, 'tremulant_scope': self.tremulant_scope,
                },
                'active': {
                    'keyboard': self.active_keyboard,
                    'register': self.active_register,
                    'variant': self.active_variant,
                },
                'check_prompt': self.check_prompt,
                'record_mode': self.record_mode,
                'auto_phase': self.auto_phase,
                'hold_release_cue': self.hold_release_cue,
                'stable_progress': self.stable_progress,
                'plan': self.get_plan_cached(),
                'settings': {
                    'sample_rate': self.sample_rate,
                    'bit_depth': self.bit_depth,
                    'channels': self.channels,
                    'output_format': self.output_format,
                    'mp3_bitrate': self.mp3_bitrate,
                    'countdown_seconds': self.countdown_seconds,
                    'record_seconds': self.record_seconds,
                    'record_mode': self.record_mode,
                    'min_stable_seconds': self.min_stable_seconds,
                    'max_record_seconds': self.max_record_seconds,
                    'auto_sensitivity': self.auto_sensitivity,
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

def _route_ip():
    """IP of the interface that holds the default route (real WiFi/LAN when
    online). Returns None with no internet (e.g. hotspot-only)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _rank_ip(ip, route_ip=None):
    """Rank candidate IPs so the most useful one for remote access sorts first.
    Hotspot host wins; then the default-route interface (real WiFi/LAN)."""
    if ip.startswith("192.168.137."):
        return 0  # Windows Mobile Hotspot / ICS host
    if route_ip and ip == route_ip:
        return 1  # interface with the default route (real WiFi/LAN)
    if ip.startswith("127.") or ip.startswith("169.254."):
        return 9  # loopback / APIPA (only if nothing else)
    if ip.startswith("192.168.") or ip.startswith("10.") or \
       re.match(r"^172\.(1[6-9]|2[0-9]|3[01])\.", ip):
        return 2  # other private LAN
    return 3      # other routable


def _ips_from_getaddrinfo():
    """All local IPv4 addresses via DNS-free hostname resolution."""
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def _ips_from_ipconfig():
    """Fallback: parse `ipconfig` output for IPv4 addresses (Windows)."""
    ips = []
    if sys.platform != "win32":
        return ips
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        out = subprocess.run(["ipconfig"], capture_output=True, text=True,
                             creationflags=flags, timeout=5).stdout
        for m in re.finditer(r"IPv4.*?:\s*([\d.]+)", out):
            ip = m.group(1)
            if ip and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def get_local_ips():
    """Return all local IPv4 addresses, best-for-remote-access first.

    Merges socket.getaddrinfo and (Windows) ipconfig, drops loopback/APIPA
    unless nothing else is available, and ranks the hotspot host first.
    Works even with no internet connection (hotspot scenario).
    """
    ips = _ips_from_getaddrinfo()
    for ip in _ips_from_ipconfig():
        if ip not in ips:
            ips.append(ip)
    route_ip = _route_ip()
    if route_ip and route_ip not in ips:
        ips.append(route_ip)
    # Drop loopback/APIPA unless they are all we have.
    usable = [ip for ip in ips if _rank_ip(ip, route_ip) < 9]
    if usable:
        ips = usable
    ips.sort(key=lambda ip: _rank_ip(ip, route_ip))
    return ips or ["127.0.0.1"]


def get_hotspot_ip():
    """Return the Windows Mobile Hotspot host IP (192.168.137.x) if present."""
    for ip in get_local_ips():
        if ip.startswith("192.168.137."):
            return ip
    return None


def get_local_ip():
    """Get the single best local IP address for remote access."""
    return get_local_ips()[0]


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

    # ── i18n script (shared by display + remote) ──
    @app.route('/i18n.js')
    def i18n_js():
        return Response(I18N_JS, mimetype='application/javascript')
    
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
        if data.get('record_mode') in ('fixed', 'auto'):
            engine.record_mode = data['record_mode']
        # None-guard: een leeg number-veld levert JSON null op — dat mag de
        # rest van de settings-apply + save_manifest niet laten sneuvelen.
        if data.get('min_stable_seconds') is not None:
            engine.min_stable_seconds = max(0.5, min(10.0, float(data['min_stable_seconds'])))
        if data.get('max_record_seconds') is not None:
            engine.max_record_seconds = max(3.0, min(60.0, float(data['max_record_seconds'])))
        if data.get('auto_sensitivity') is not None:
            engine.auto_sensitivity = max(0.3, min(3.0, float(data['auto_sensitivity'])))
        if 'bass_treble_split' in data:
            engine.bass_treble_split = bool(data['bass_treble_split'])
        if 'split_note' in data:
            engine.split_note = int(data['split_note'])
        if 'split_record_bas' in data:
            engine.split_record_bas = bool(data['split_record_bas'])
        if 'split_record_disc' in data:
            engine.split_record_disc = bool(data['split_record_disc'])
        if engine.project_name:
            engine.save_manifest()
        return jsonify({'success': True, 'state': engine.get_state()})

    @app.route('/api/commit-organ', methods=['POST'])
    def api_commit_organ():
        plan = request.json or {}
        if not plan.get('keyboards') and not plan.get('has_pedal'):
            return jsonify({'success': False, 'error': 'Minstens één klavier of pedaal nodig'})
        try:
            path = engine.commit_organ(plan)
            return jsonify({'success': True, 'path': path, 'state': engine.get_state()})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/last-project')
    def api_last_project():
        lp = get_last_project()
        if lp:
            return jsonify({'exists': True, **lp})
        return jsonify({'exists': False})

    @app.route('/api/load-project', methods=['POST'])
    def api_load_project():
        data = request.json or {}
        path = data.get('path')
        if not path:
            lp = get_last_project()
            path = lp.get('path') if lp else None
        if not path:
            return jsonify({'success': False, 'error': 'Geen project om te laden'})
        try:
            ok = engine.load_project(path)
            return jsonify({'success': bool(ok), 'state': engine.get_state()})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/select-register', methods=['POST'])
    def api_select_register():
        data = request.json or {}
        kb = (data.get('keyboard') or '').strip()
        reg = (data.get('register') or '').strip()
        variant = data.get('variant', 'normal')
        ok = engine.select_register(kb, reg, variant)
        return jsonify({'success': bool(ok), 'state': engine.get_state()})

    @app.route('/api/mark-register', methods=['POST'])
    def api_mark_register():
        d = request.json or {}
        ok = engine.mark_register_checked((d.get('keyboard') or '').strip(),
                                          (d.get('register') or '').strip(),
                                          d.get('variant', 'normal'),
                                          bool(d.get('checked', True)))
        return jsonify({'success': bool(ok), 'state': engine.get_state()})

    @app.route('/api/dismiss-check', methods=['POST'])
    def api_dismiss_check():
        engine.check_prompt = None
        engine._notify()
        return jsonify({'success': True})

    @app.route('/api/add-register', methods=['POST'])
    def api_add_register():
        d = request.json or {}
        ok = engine.add_register(
            (d.get('keyboard') or engine.current_keyboard or '').strip(),
            d.get('name') or d.get('display') or '',
            foot=d.get('foot', ''),
            begin_note=d.get('begin_note'),
            end_note=d.get('end_note'),
            bass_treble=bool(d.get('bass_treble', False)),
        )
        return jsonify({'success': bool(ok), 'state': engine.get_state()})

    @app.route('/api/edit-register', methods=['POST'])
    def api_edit_register():
        d = request.json or {}
        kb = (d.get('keyboard') or '').strip()
        name = (d.get('register') or '').strip()
        fields = {k: d[k] for k in ('foot', 'display', 'begin_note', 'end_note', 'bass_treble') if k in d}
        ok = engine.edit_register(kb, name, **fields)
        return jsonify({'success': bool(ok), 'state': engine.get_state()})

    @app.route('/api/remove-register', methods=['POST'])
    def api_remove_register():
        d = request.json or {}
        ok = engine.remove_register((d.get('keyboard') or '').strip(),
                                    (d.get('register') or '').strip())
        return jsonify({'success': bool(ok), 'state': engine.get_state()})

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

    def _request_port():
        return request.host.split(':')[-1] if ':' in request.host else '5555'

    def _build_qr_svg(url):
        """Render a QR code SVG for the given URL (or a placeholder)."""
        if HAS_QRCODE:
            qr = qrcode.QRCode(version=1, box_size=10, border=2)
            qr.add_data(url)
            qr.make(fit=True)
            factory = qrcode.image.svg.SvgPathImage
            img = qr.make_image(image_factory=factory, fill_color="#000000", back_color="#ffffff")
            buf = io.BytesIO()
            img.save(buf)
            return Response(buf.getvalue(), mimetype='image/svg+xml')
        svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="200" height="60">
                <rect width="200" height="60" rx="8" fill="#12121a" stroke="#1e1e2e"/>
                <text x="100" y="35" text-anchor="middle" fill="#6b6b8a" font-family="monospace" font-size="11">QR niet beschikbaar</text>
            </svg>'''
        return Response(svg, mimetype='image/svg+xml')

    @app.route('/api/qr.svg')
    def api_qr_svg():
        """Generate QR code SVG for the remote control URL.
        Optional ?ip= selects a specific (detected) local IP for the URL."""
        ips = get_local_ips()
        requested = request.args.get('ip')
        local_ip = requested if requested in ips else (ips[0] if ips else get_local_ip())
        port = _request_port()
        return _build_qr_svg(f"http://{local_ip}:{port}")

    @app.route('/api/remote-url')
    def api_remote_url():
        """Get the remote control URL."""
        port = _request_port()
        return jsonify({'url': f"http://{get_local_ip()}:{port}"})

    @app.route('/api/network-info')
    def api_network_info():
        """List candidate local IPs (hotspot-aware) for the remote URL/QR."""
        ips = get_local_ips()
        hotspot = get_hotspot_ip()
        port = _request_port()
        best = ips[0] if ips else '127.0.0.1'
        return jsonify({
            'ips': ips,
            'hotspot_ip': hotspot,
            'port': port,
            'url': f"http://{best}:{port}",
        })

    @app.route('/api/open-hotspot-settings', methods=['POST'])
    def api_open_hotspot_settings():
        """Open the Windows Mobile Hotspot settings page so the user can
        enable it. Works from the windowless build via ShellExecute."""
        if sys.platform != 'win32':
            return jsonify({'success': False, 'error': 'Alleen beschikbaar op Windows'})
        try:
            os.startfile('ms-settings:network-mobilehotspot')
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/pick-folder', methods=['POST'])
    def api_pick_folder():
        """Open a native folder-picker on the host PC and return the path."""
        try:
            path = pick_folder_dialog("Kies opslagmap voor opnames")
            return jsonify({'success': bool(path), 'path': path or ''})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})

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
            "jm_rec_version": JM_REC_VERSION,
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

    @app.route('/api/export-organ', methods=['POST'])
    def api_export_organ():
        """Export a GrandOrgue-compatible .organ definition for JM-Orgue."""
        try:
            result = engine.export_organ_odf()
            return jsonify({'success': True, **result})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})

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
            if engine.project_name:
                engine.save_manifest()
            engine._notify()
        return jsonify({'success': True, 'couplers': engine.couplers})

    @app.route('/api/remove-coupler', methods=['POST'])
    def api_remove_coupler():
        data = request.json or {}
        idx = data.get('index')
        if idx is not None and 0 <= idx < len(engine.couplers):
            engine.couplers.pop(idx)
            if engine.project_name:
                engine.save_manifest()
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
<script src="/i18n.js"></script>
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
/* Error banner */
.error-banner {
    display: none;
    max-width: 90%;
    text-align: center;
    font-size: 0.95rem;
    padding: 10px 18px;
    border-radius: 10px;
    background: rgba(239, 68, 68, 0.12);
    border: 1px solid var(--recording);
    color: var(--recording);
    overflow-wrap: anywhere;
    word-break: break-word;
}
.error-banner.show { display: block; }
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
.qr-modal select {
    font-family: inherit;
    font-size: 0.85rem;
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 6px 10px;
    margin-top: 6px;
    max-width: 100%;
}
.qr-ip-row { margin-top: 12px; font-size: 0.8rem; color: var(--dim); }
.qr-ip-row label { display: block; margin-bottom: 4px; }
.qr-direct {
    margin-top: 18px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
    text-align: left;
}
.qr-direct-title {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--accent);
    margin-bottom: 6px;
    text-align: center;
}
.qr-direct-intro { font-size: 0.82rem; color: var(--dim); margin-bottom: 8px; }
.qr-direct-steps {
    font-size: 0.82rem;
    color: var(--text);
    margin: 0 0 10px 18px;
    padding: 0;
    line-height: 1.5;
}
.qr-direct-steps li { margin-bottom: 3px; }
.qr-direct-btn {
    display: block;
    width: 100%;
    font-family: inherit;
    font-size: 0.9rem;
    font-weight: 600;
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 10px;
    padding: 10px;
    cursor: pointer;
}
.qr-direct-btn:hover { opacity: 0.9; }
.qr-direct-foot { font-size: 0.75rem; color: var(--dim); margin-top: 10px; text-align: center; }

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

<style>
.startup-overlay, .wiz-overlay {
    position: fixed; inset: 0; z-index: 5000;
    background: rgba(8,8,14,0.97); display: none;
    align-items: center; justify-content: center; padding: 20px;
}
.wiz-card, .startup-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 16px; padding: 26px 30px; width: 100%; max-width: 780px;
    max-height: 92vh; overflow-y: auto; box-shadow: 0 20px 60px rgba(0,0,0,0.5);
}
.startup-card { max-width: 520px; text-align: center; }
.wiz-head { display:flex; align-items:baseline; justify-content:space-between; margin-bottom: 4px; }
.wiz-title { font-family:'JetBrains Mono',monospace; font-size:1.2rem; color:var(--accent); }
.wiz-steplabel { font-family:'JetBrains Mono',monospace; font-size:0.8rem; color:var(--dim); }
.wiz-progress-bg { height:6px; background:var(--bg); border-radius:6px; margin:10px 0 20px; overflow:hidden; }
.wiz-progress { height:100%; background:linear-gradient(90deg,var(--accent),var(--success)); transition:width .25s; }
.wiz-step { display:none; }
.wiz-step h2 { font-size:1.05rem; margin:0 0 6px; color:var(--text); }
.wiz-step p.hint { font-size:0.85rem; color:var(--dim); margin:0 0 14px; }
.wiz-label { display:block; font-size:0.8rem; color:var(--dim); margin:12px 0 4px; }
.wiz-input, .wiz-select {
    width:100%; box-sizing:border-box; font-family:inherit; font-size:0.95rem;
    background:var(--bg); color:var(--text); border:1px solid var(--border);
    border-radius:8px; padding:9px 12px; outline:none;
}
.wiz-input:focus, .wiz-select:focus { border-color:var(--accent); }
.wiz-foot { font-family:'JetBrains Mono',monospace; color:var(--accent); font-size:0.9rem; }
.wiz-nav { display:flex; justify-content:space-between; gap:10px; margin-top:22px; }
.wiz-btn {
    font-family:inherit; font-size:0.95rem; font-weight:600; border:none; cursor:pointer;
    border-radius:10px; padding:11px 20px; background:var(--bg); color:var(--text);
    border:1px solid var(--border);
}
.wiz-btn.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
.wiz-btn:disabled { opacity:0.4; cursor:not-allowed; }
.wiz-row { display:flex; gap:8px; align-items:center; margin:6px 0; }
.wiz-check { display:flex; align-items:center; gap:8px; font-size:0.9rem; color:var(--text); margin:8px 0; }
.wiz-kb-block { border:1px solid var(--border); border-radius:10px; padding:12px 14px; margin:12px 0; }
.wiz-kb-block h3 { margin:0 0 8px; font-size:0.95rem; color:var(--accent); }
.wiz-reg-table { width:100%; border-collapse:collapse; font-size:0.8rem; }
.wiz-reg-table th { text-align:left; color:var(--dim); font-weight:500; padding:2px 4px; }
.wiz-reg-table td { padding:2px 4px; }
.wiz-reg-table input[type=text], .wiz-reg-table input[type=number] {
    width:100%; box-sizing:border-box; background:var(--bg); color:var(--text);
    border:1px solid var(--border); border-radius:6px; padding:5px 6px; font-family:inherit; font-size:0.8rem;
}
.wiz-notelbl { font-family:'JetBrains Mono',monospace; color:var(--dim); font-size:0.7rem; }
.wiz-mini-btn { background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:4px 10px; cursor:pointer; font-size:0.8rem; }
.wiz-del { color:var(--recording); cursor:pointer; font-weight:700; border:none; background:none; font-size:1rem; }
.wiz-grid2 { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.startup-card h2 { color:var(--accent); font-family:'JetBrains Mono',monospace; margin:0 0 8px; }
.startup-card p { color:var(--dim); font-size:0.9rem; margin:0 0 18px; }
.startup-btns { display:flex; flex-direction:column; gap:10px; }
</style>

<!-- Startup choice overlay -->
<div class="startup-overlay" id="startupOverlay">
    <div class="startup-card">
        <h2>JM-Rec</h2>
        <p id="startupText">Welkom</p>
        <div class="startup-btns">
            <button class="wiz-btn primary" id="startupContinueBtn" style="display:none;" onclick="startupContinue()">Doorgaan met dit orgel</button>
            <button class="wiz-btn" onclick="wizStartNew()">Nieuw orgel instellen</button>
        </div>
    </div>
</div>

<!-- Setup wizard overlay -->
<div class="wiz-overlay" id="wizOverlay">
    <div class="wiz-card">
        <div class="wiz-head">
            <div class="wiz-title">Orgel instellen</div>
            <div class="wiz-steplabel" id="wizStepLabel">Stap 1 / 10</div>
        </div>
        <div class="wiz-progress-bg"><div class="wiz-progress" id="wizProgress" style="width:10%;"></div></div>

        <div class="wiz-step" id="wizStep1">
            <h2>1. Opslaglocatie</h2>
            <p class="hint">Map waarin alle opnames worden bewaard.</p>
            <label class="wiz-label">Opslagmap</label>
            <div class="wiz-row">
                <input class="wiz-input" id="wizOutputDir" placeholder="bijv. D:\Opnames" style="flex:1;">
                <button class="wiz-mini-btn" onclick="wizPickFolder()">Bladeren…</button>
            </div>
        </div>

        <div class="wiz-step" id="wizStep2">
            <h2>2. Microfoon(s)</h2>
            <p class="hint">Kies de opnamebron. Bij meerdere microfoons krijgt elke een eigen positienaam (submap).</p>
            <label class="wiz-label">Bron</label>
            <select class="wiz-select" id="wizMicMode" onchange="wizToggleMicMode()">
                <option value="mic">Microfoon(s)</option>
                <option value="loopback">Wat je hoort (loopback)</option>
            </select>
            <div id="wizMicList" style="margin-top:10px;"></div>
            <div id="wizLoopbackList" style="margin-top:10px; display:none;"></div>
        </div>

        <div class="wiz-step" id="wizStep3">
            <h2>3. Plaatsnaam</h2>
            <p class="hint">Plaats waar het orgel staat.</p>
            <input class="wiz-input" id="wizPlaats" oninput="wizUpdateFolder()" placeholder="bijv. Puttershoek">
            <p class="hint" style="margin-top:12px;">Mapcode wordt: <span class="wiz-foot" id="wizFolderPrev1">—</span></p>
        </div>

        <div class="wiz-step" id="wizStep4">
            <h2>4. Kerknaam</h2>
            <p class="hint">Naam van de kerk/gebouw (wordt als info bewaard).</p>
            <input class="wiz-input" id="wizKerk" placeholder="bijv. Hervormde Kerk">
        </div>

        <div class="wiz-step" id="wizStep5">
            <h2>5. Orgelbouwer</h2>
            <p class="hint">De bouwer van het orgel.</p>
            <input class="wiz-input" id="wizBouwer" oninput="wizUpdateFolder()" placeholder="bijv. Muller">
            <label class="wiz-label">Mapnaam (4 letters plaats + 4 letters bouwer, aanpasbaar)</label>
            <input class="wiz-input wiz-foot" id="wizFolder" oninput="wizData.folder_edited=true">
        </div>

        <div class="wiz-step" id="wizStep6">
            <h2>6. Klavieren en pedaal</h2>
            <p class="hint">Hoeveel klavieren (manualen) heeft het orgel?</p>
            <label class="wiz-label">Aantal klavieren</label>
            <input class="wiz-input" id="wizNkb" type="number" min="1" max="6" value="2">
            <label class="wiz-check"><input type="checkbox" id="wizHasPedal" checked> Pedaal aanwezig</label>
        </div>

        <div class="wiz-step" id="wizStep7">
            <h2>7. Naam per klavier</h2>
            <p class="hint">Geef elk klavier een naam.</p>
            <div id="wizKbNames"></div>
        </div>

        <div class="wiz-step" id="wizStep8">
            <h2>8. Tremulant en zwelkast</h2>
            <p class="hint">Bij een tremulant wordt elk register 2x opgenomen (normaal én _trem).</p>
            <label class="wiz-label">Tremulant</label>
            <select class="wiz-select" id="wizTremScope" onchange="wizBuildTrem()">
                <option value="none">Geen tremulant</option>
                <option value="organ">Heel het orgel</option>
                <option value="keyboard">Per klavier</option>
            </select>
            <div id="wizTremPerKb" style="margin-top:10px;"></div>
            <label class="wiz-label" style="margin-top:14px;">Zwelkast (zwelwerk) per klavier</label>
            <div id="wizZwelkast"></div>
        </div>

        <div class="wiz-step" id="wizStep9">
            <h2>9. Registers per klavier</h2>
            <p class="hint">Per register: naam, voetmaat (8' of 4st), begin- en eindnoot, en bas/disc-splitsing. Geheugensteun: <b>C-groot = MIDI 36</b>.</p>
            <div id="wizRegisters"></div>
        </div>

        <div class="wiz-step" id="wizStep10">
            <h2>10. Opname-instellingen en koppels</h2>
            <div class="wiz-grid2">
                <div><label class="wiz-label">Samplerate</label>
                    <select class="wiz-select" id="wizSampleRate"><option>44100</option><option selected>48000</option><option>96000</option></select></div>
                <div><label class="wiz-label">Bitdiepte</label>
                    <select class="wiz-select" id="wizBitDepth"><option value="16" selected>16-bit</option><option value="24">24-bit</option></select></div>
                <div><label class="wiz-label">Kanalen</label>
                    <select class="wiz-select" id="wizChannels"><option value="1" selected>Mono</option><option value="2">Stereo</option></select></div>
                <div><label class="wiz-label">Formaat</label>
                    <select class="wiz-select" id="wizFormat"><option value="mp3" selected>MP3</option><option value="wav">WAV</option><option value="flac">FLAC</option></select></div>
                <div><label class="wiz-label">Aftellen (sec)</label>
                    <input class="wiz-input" id="wizCountdown" type="number" min="1" max="30" value="5"></div>
                <div><label class="wiz-label">Opnameduur (sec)</label>
                    <input class="wiz-input" id="wizRecDur" type="number" min="1" max="60" value="5"></div>
                <div><label class="wiz-label">Splitstoets bas/disc (MIDI)</label>
                    <input class="wiz-input" id="wizSplitNote" type="number" min="0" max="127" value="60" oninput="document.getElementById('wizSplitLbl').textContent=dMidiToName(parseInt(this.value)||0)"></div>
                <div><label class="wiz-label">&nbsp;</label><div class="wiz-notelbl" id="wizSplitLbl" style="padding-top:10px;">C4</div></div>
            </div>
            <label class="wiz-label" style="margin-top:14px;">Opnamemodus</label>
            <select class="wiz-select" id="wizRecordMode" onchange="document.getElementById('wizAutoGroup').style.display=this.value==='auto'?'':'none'">
                <option value="fixed" selected>Vaste duur</option>
                <option value="auto">Intelligent (assisterend)</option>
            </select>
            <div id="wizAutoGroup" style="display:none;margin-top:8px;">
                <div style="font-size:0.8rem;color:var(--dim);margin-bottom:6px;">Neemt automatisch op tot er genoeg stabiele, loopbare toon is en seint dan dat je kunt loslaten. Alleen microfooningang.</div>
                <div class="wiz-grid2">
                    <div><label class="wiz-label">Min. stabiele toon (sec)</label>
                        <input class="wiz-input" id="wizMinStable" type="number" min="0.5" max="10" step="0.5" value="2"></div>
                    <div><label class="wiz-label">Max. duur (sec)</label>
                        <input class="wiz-input" id="wizMaxRec" type="number" min="3" max="60" value="20"></div>
                    <div><label class="wiz-label">Gevoeligheid</label>
                        <input class="wiz-input" id="wizSensitivity" type="number" min="0.3" max="3" step="0.1" value="1"></div>
                </div>
            </div>
            <label class="wiz-label" style="margin-top:14px;">Koppels</label>
            <div class="wiz-row">
                <select class="wiz-select" id="wizCouplerSrc"></select>
                <span>→</span>
                <select class="wiz-select" id="wizCouplerTgt"></select>
                <button class="wiz-mini-btn" onclick="wizAddCoupler()">+ Koppel</button>
            </div>
            <div id="wizCouplerList" style="font-size:0.85rem; color:var(--dim);"></div>
        </div>

        <div class="wiz-nav">
            <button class="wiz-btn" id="wizPrev" onclick="wizBack()">← Vorige</button>
            <button class="wiz-btn primary" id="wizNext" onclick="wizForward()">Volgende →</button>
        </div>
    </div>
</div>

<div class="header">
    <div class="logo">JM-Rec <span>v3.7</span></div>
    <div class="header-actions">
        <div class="project-info">
            <span id="projectInfo">—</span>
        </div>
        <button class="header-btn" onclick="openModal('qrModal')">QR Remote</button>
        <button class="header-btn" onclick="openModal('regModal')">Registers</button>
        <button class="header-btn" onclick="openModal('reviewModal')">Controle</button>
        <button class="header-btn" onclick="openModal('readmeModal')">? Info</button>
        <button class="header-btn" onclick="wizStartNew()">Nieuw orgel</button>
        <button class="header-btn" onclick="toggleDrawer()">Instellingen</button>
        <span id="langSel" style="margin-left:8px;"></span>
    </div>
</div>

<div class="main">
    <div class="state-badge" id="stateBadge">IDLE</div>

    <div class="error-banner" id="errorBanner"></div>

    <div class="note-display">
        <div class="note-name" id="noteName">—</div>
        <div class="note-filename" id="noteFilename">—</div>
    </div>
    
    <div class="countdown-display" id="countdownDisplay"></div>

    <div class="auto-status" id="autoStatus" style="display:none;"></div>
    <style>
    .auto-status { margin:6px auto 0; max-width:520px; text-align:center; font-weight:600;
        font-size:1.05rem; padding:12px 18px; border-radius:14px; border:1px solid var(--border);
        background:var(--surface); transition:all .15s; }
    .auto-status.waiting { color:var(--dim); }
    .auto-status.stabilizing { color:#f59e0b; border-color:#f59e0b; }
    .auto-status.hold { color:#fff; background:#22c55e; border-color:#22c55e; font-size:1.5rem;
        box-shadow:0 0 0 4px rgba(34,197,94,0.25); animation:autoPulse 1s ease-in-out infinite; }
    .auto-status.release { color:var(--dim); }
    .auto-status .as-bar { height:6px; border-radius:6px; background:rgba(245,158,11,0.25); margin-top:8px; overflow:hidden; }
    .auto-status .as-bar > div { height:100%; background:#f59e0b; width:0; transition:width .15s; }
    @keyframes autoPulse { 0%,100%{transform:scale(1);} 50%{transform:scale(1.03);} }
    </style>

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

    <style>
    .main-controls { display:flex; gap:12px; justify-content:center; margin-top:24px; flex-wrap:wrap; }
    .mc-btn { font-family:inherit; font-size:1rem; font-weight:600; cursor:pointer; border-radius:12px;
        padding:14px 28px; background:var(--surface); color:var(--text); border:1px solid var(--border); }
    .mc-btn.mc-rec { background:var(--accent); color:#fff; border-color:var(--accent); }
    .mc-btn.mc-stop { color:var(--recording); border-color:var(--recording); }
    .mc-btn:disabled { opacity:0.4; cursor:not-allowed; }
    .main-registers { margin-top:18px; max-width:680px; width:100%; }
    .mc-kbrow { display:flex; gap:8px; flex-wrap:wrap; justify-content:center; margin-bottom:10px; }
    .mc-tab { font-family:'JetBrains Mono',monospace; font-size:0.8rem; cursor:pointer; padding:6px 14px;
        border-radius:8px; background:var(--surface); color:var(--dim); border:1px solid var(--border); }
    .mc-tab.active { background:var(--accent); color:#fff; border-color:var(--accent); }
    .mc-reglist { display:flex; gap:8px; flex-wrap:wrap; justify-content:center; }
    .mc-reg { font-size:0.85rem; cursor:pointer; padding:8px 14px; border-radius:10px;
        background:var(--surface); color:var(--text); border:1px solid var(--border); }
    .mc-reg.active { border-color:var(--accent); background:rgba(125,125,255,0.10); }
    </style>
    <div class="main-controls">
        <button class="mc-btn mc-rec" id="mRecBtn" onclick="dApi('/api/record')">&#9654; Opnemen</button>
        <button class="mc-btn" onclick="dApi('/api/pause')">&#9208; Pauze</button>
        <button class="mc-btn mc-stop" onclick="dApi('/api/stop')">&#9632; Stop</button>
    </div>
    <div class="main-registers">
        <div style="font-size:0.7rem;color:var(--dim);margin-bottom:8px;display:flex;gap:12px;flex-wrap:wrap;justify-content:center;">
            <span><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#ef4444;margin-right:4px;"></span>nog op te nemen</span>
            <span><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#f59e0b;margin-right:4px;"></span>niet compleet</span>
            <span><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#a855f7;margin-right:4px;"></span>nog controleren</span>
            <span><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#22c55e;margin-right:4px;"></span>goed</span>
        </div>
        <div class="mc-kbrow" id="mKbSelector"></div>
        <div class="mc-reglist" id="mRegList"></div>
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
                <div class="d-form-group" style="flex:1;">
                    <label class="d-form-label">Opnamemodus</label>
                    <select class="d-form-input" id="dRecordMode" onchange="document.getElementById('dAutoGroup').style.display=this.value==='auto'?'':'none'">
                        <option value="fixed">Vaste duur</option>
                        <option value="auto">Intelligent (assisterend)</option>
                    </select>
                </div>
            </div>
            <div id="dAutoGroup" style="display:none;">
                <div style="font-size:0.72rem;color:var(--dim);margin:2px 0 8px;">Neemt automatisch op tot er genoeg stabiele, loopbare toon is en seint dan dat je kunt loslaten. Alleen microfooningang.</div>
                <div class="d-form-row">
                    <div class="d-form-group">
                        <label class="d-form-label">Min. stabiele toon (sec)</label>
                        <input class="d-form-input" type="number" id="dMinStable" value="2" min="0.5" max="10" step="0.5">
                    </div>
                    <div class="d-form-group">
                        <label class="d-form-label">Max. duur (sec)</label>
                        <input class="d-form-input" type="number" id="dMaxRec" value="20" min="3" max="60">
                    </div>
                </div>
                <div class="d-form-row">
                    <div class="d-form-group" style="flex:1;">
                        <label class="d-form-label">Gevoeligheid (0.3 streng – 3 los)</label>
                        <input class="d-form-input" type="number" id="dSensitivity" value="1" min="0.3" max="3" step="0.1">
                    </div>
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

        <div class="drawer-section">
            <div class="drawer-section-title">Exporteren</div>
            <div style="font-size:0.72rem;color:var(--dim);margin-bottom:8px;">Maakt een .organ-definitiebestand in de projectmap dat JM-Orgue direct kan laden.</div>
            <button class="d-btn d-btn-primary" onclick="dExportOrgan()" style="width:100%;">Exporteer .organ (JM-Orgue)</button>
            <button class="d-btn" onclick="dExportProject()" style="width:100%;margin-top:6px;">Exporteer projectgegevens (.json)</button>
            <div id="dExportResult" style="font-size:0.75rem;color:var(--dim);margin-top:6px;word-break:break-all;"></div>
        </div>

    </div>
</div>

<!-- QR Code Modal -->
<div class="modal-overlay" id="qrModal" onclick="if(event.target===this)closeModal('qrModal')">
    <div class="modal qr-modal">
        <button class="modal-close" onclick="closeModal('qrModal')">&times;</button>
        <div class="modal-title">Remote Control</div>
        <p style="color:var(--dim);font-size:0.85rem;">Scan de QR-code met je telefoon of tablet<br>om de afstandsbediening te openen</p>
        <div class="qr-img">
            <img id="qrImage" src="/api/qr.svg" alt="QR Code" style="width:100%;height:100%;">
        </div>
        <div class="qr-url" id="qrUrl">Laden...</div>
        <div class="qr-ip-row" id="qrIpRow" style="display:none;">
            <label for="qrIpSelect">Kies het netwerk waarmee je telefoon verbonden is:</label>
            <select id="qrIpSelect" onchange="selectQrIp(this.value)"></select>
        </div>
        <div class="qr-hint">Zorg dat je telefoon op hetzelfde netwerk zit als deze PC.</div>

        <div class="qr-direct">
            <div class="qr-direct-title">Directe verbinding (geen WiFi nodig)</div>
            <p class="qr-direct-intro">Geen WiFi op deze locatie? Laat deze PC zelf een netwerk uitzenden en verbind je telefoon of tablet daarmee.</p>
            <ol class="qr-direct-steps">
                <li>Klik op 'Open hotspot-instellingen' en zet de Mobiele hotspot AAN.</li>
                <li>Noteer de netwerknaam en het wachtwoord die Windows toont.</li>
                <li>Verbind je iPad/iPhone of Android met dat netwerk.</li>
                <li>Kies hierboven het hotspot-netwerk en scan de QR-code (of typ het adres).</li>
            </ol>
            <button class="qr-direct-btn" onclick="openHotspotSettings()">Open hotspot-instellingen</button>
            <p class="qr-direct-foot">Internet is niet nodig — de bediening werkt ook zonder. Houd dit venster open; schakelt de hotspot uit, zet hem opnieuw aan.</p>
        </div>
    </div>
</div>

<!-- Register manager Modal -->
<div class="modal-overlay" id="regModal" onclick="if(event.target===this)closeModal('regModal')">
    <div class="modal" style="max-width:680px;max-height:85vh;overflow-y:auto;">
        <button class="modal-close" onclick="closeModal('regModal')">&times;</button>
        <div class="modal-title">Registerbeheer</div>
        <p style="color:var(--dim);font-size:0.8rem;">Registers toevoegen/verwijderen per klavier. C-groot = MIDI 36.</p>
        <label class="d-form-label">Klavier</label>
        <select class="d-form-input" id="regKbSel" onchange="regRender()"></select>
        <div id="regList" style="margin:10px 0;"></div>
        <div style="border-top:1px solid var(--border);padding-top:10px;">
            <div class="d-form-label">Nieuw register</div>
            <div style="display:grid;grid-template-columns:1fr 60px 70px 70px auto;gap:6px;align-items:center;">
                <input class="d-form-input" id="regNewName" placeholder="naam (bijv. Prestant)">
                <input class="d-form-input" id="regNewFoot" placeholder="8">
                <input class="d-form-input" id="regNewBegin" type="number" value="36" title="beginnoot">
                <input class="d-form-input" id="regNewEnd" type="number" value="96" title="eindnoot">
                <label class="d-form-label" style="margin:0;"><input type="checkbox" id="regNewBass"> bas/disc</label>
            </div>
            <button class="d-btn d-btn-primary" style="margin-top:8px;" onclick="regAdd()">+ Register toevoegen</button>
        </div>
    </div>
</div>

<!-- Controle-prompt Modal (verschijnt automatisch na voltooien register) -->
<div class="modal-overlay" id="checkModal">
    <div class="modal" style="max-width:480px;text-align:center;">
        <div class="modal-title">Register compleet 🎉</div>
        <p id="checkMsg" style="color:var(--text);font-size:0.95rem;margin:10px 0 18px;">—</p>
        <div style="display:flex;flex-direction:column;gap:10px;">
            <button class="d-btn d-btn-primary" onclick="checkDoReview()">🔍 Nu controleren</button>
            <button class="d-btn" style="border-color:#22c55e;color:#22c55e;" onclick="checkApprove()">✓ Goedgekeurd</button>
            <button class="d-btn" onclick="checkLater()">Later</button>
        </div>
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
        <div id="readmeBody">
        <div class="modal-title">JM-Rec — Handleiding</div>

        <h2>Snelstart</h2>
        <ul>
            <li>Doorloop bij het opstarten de <strong>wizard</strong> (10 stappen) om het orgel + de registers vast te leggen — of kies <strong>Doorgaan</strong> met het laatste orgel.</li>
            <li>Op het <strong>hoofdscherm</strong>: kies een register en druk op <strong>Opnemen</strong>.</li>
            <li>Scan de <strong>QR-code</strong> (QR Remote) om met je telefoon te bedienen.</li>
            <li>Achteraf bewerken: knop <strong>Registers</strong> (toevoegen/verwijderen, gecontroleerd-markering) of <strong>Nieuw orgel</strong>.</li>
        </ul>

        <h2>Bediening</h2>
        <table>
            <tr><th>Knop</th><th>Functie</th></tr>
            <tr><td><code>Opnemen</code></td><td>Start automatische opnamecyclus van het gekozen register</td></tr>
            <tr><td><code>Pauze</code></td><td>Pauzeert na de huidige noot</td></tr>
            <tr><td><code>Stop</code></td><td>Stopt direct</td></tr>
            <tr><td><code>Vorige noot / Volgende noot</code></td><td>Spring naar een andere noot</td></tr>
            <tr><td><code>Opnieuw</code></td><td>Neem de huidige noot opnieuw op</td></tr>
        </table>

        <h2>Kleurcodes per register</h2>
        <table>
            <tr><th>Kleur</th><th>Betekenis</th></tr>
            <tr><td>🔴 rood</td><td>nog op te nemen (0 noten)</td></tr>
            <tr><td>🟠 oranje</td><td>begonnen, nog niet compleet</td></tr>
            <tr><td>🟣 paars</td><td>volledig opgenomen, nog te controleren</td></tr>
            <tr><td>🟢 groen</td><td>gecontroleerd en goedgekeurd</td></tr>
        </table>
        <p>Markeer een register als <strong>gecontroleerd</strong> (paars → groen) via de knop <strong>Registers</strong>.</p>
        <p>Zodra een register volledig is opgenomen verschijnt automatisch de vraag <strong>Nu controleren / Goedgekeurd / Later</strong> — op de PC én de afstandsbediening.</p>

        <h2>Opnamecyclus</h2>
        <p>Per noot: <strong>Aftellen</strong> (standaard 5s) &rarr; <strong>Opnemen</strong> (standaard 5s) &rarr; <strong>Volgende noot</strong>. Dit herhaalt zich automatisch tot de laatste noot.</p>

        <h2>Intelligent opnemen (assisterend)</h2>
        <p>Zet <strong>Opnamemodus</strong> in de instellingen op <em>Intelligent (assisterend)</em> (alleen microfooningang). De recorder meet eerst de ruisvloer, wacht op de toon en luistert of de klank <strong>stabiel en loopbaar</strong> is. Zodra er genoeg goede toon is verschijnt een groen sein <strong>&ldquo;Genoeg &mdash; laat los&rdquo;</strong>. Laat de toets dan los: de <strong>uitklank</strong> wordt automatisch meegenomen tot stilte en de recorder gaat door naar de volgende noot. Bij een <em>tremulant</em>-reeks wacht hij op een stabiele tremulant-modulatie i.p.v. een vlakke toon. <em>Min. stabiele toon</em> bepaalt hoeveel goede toon nodig is, <em>Max. duur</em> is een veiligheidsgrens, en <em>Gevoeligheid</em> regelt hoe gevoelig de detectie is (hoger = sneller goedkeuren, lager = strenger). Je kunt altijd handmatig <strong>Volgende</strong>/<strong>Stop</strong> gebruiken.</p>

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

        <h2>Exporteren naar JM-Orgue (.organ)</h2>
        <p>Via <strong>Instellingen &rarr; Exporteren</strong> maak je een <code>.organ</code>-definitiebestand in de projectmap. Daarin staan alle klavieren, registers (met voetmaat), zwelkasten, tremulanten en koppels, met verwijzingen naar de opgenomen samples. JM-Orgue laadt dit bestand direct; ontbrekende noten blijven stil en kun je later alsnog opnemen (daarna opnieuw exporteren). Bij multi-mic wordt de eerst ingestelde microfoonpositie gebruikt.</p>

        <h2>Instelbare parameters</h2>
        <table>
            <tr><th>Parameter</th><th>Standaard</th><th>Opties</th></tr>
            <tr><td>Samplerate</td><td>44100 Hz</td><td>44100 / 48000 / 96000</td></tr>
            <tr><td>Bitdiepte</td><td>16-bit</td><td>16 / 24</td></tr>
            <tr><td>Kanalen</td><td>Mono</td><td>Mono / Stereo</td></tr>
            <tr><td>MP3 Bitrate</td><td>192 kbps</td><td>128 / 192 / 256 / 320</td></tr>
            <tr><td>Afteltijd</td><td>5 sec</td><td>1 &ndash; 30</td></tr>
            <tr><td>Opnameduur</td><td>5 sec</td><td>1 &ndash; 60</td></tr>
            <tr><td>Opnamemodus</td><td>Vaste duur</td><td>Vaste duur / Intelligent</td></tr>
            <tr><td>Min. stabiele toon</td><td>2 sec</td><td>0.5 &ndash; 10 (auto)</td></tr>
            <tr><td>Max. duur (auto)</td><td>20 sec</td><td>3 &ndash; 60 (auto)</td></tr>
            <tr><td>Gevoeligheid</td><td>1.0</td><td>0.3 &ndash; 3 (auto)</td></tr>
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

        <p style="color:var(--dim);margin-top:20px;font-size:0.8rem;text-align:center;">JM-Rec v3.7</p>
        </div>
    </div>
</div>

<script>
// Modal functions
function openModal(id) {
    document.getElementById(id).classList.add('active');
    if (id === 'qrModal') loadQrUrl();
    if (id === 'regModal') regOpen();
}
function closeModal(id) {
    document.getElementById(id).classList.remove('active');
}
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        document.querySelectorAll('.modal-overlay.active').forEach(m => m.classList.remove('active'));
    }
});
let _qrPort = '5555';
async function loadQrUrl() {
    try {
        const res = await fetch('/api/network-info');
        const data = await res.json();
        _qrPort = data.port || '5555';
        const sel = document.getElementById('qrIpSelect');
        const row = document.getElementById('qrIpRow');
        sel.innerHTML = '';
        (data.ips || []).forEach(ip => {
            const opt = document.createElement('option');
            opt.value = ip;
            opt.textContent = (ip === data.hotspot_ip) ? (ip + ' (hotspot)') : ip;
            sel.appendChild(opt);
        });
        const chosen = data.hotspot_ip || (data.ips && data.ips[0]) || '';
        if (chosen) sel.value = chosen;
        row.style.display = (data.ips && data.ips.length > 1) ? 'block' : 'none';
        selectQrIp(chosen);
    } catch(e) {}
}
function selectQrIp(ip) {
    if (!ip) return;
    document.getElementById('qrImage').src = '/api/qr.svg?ip=' + encodeURIComponent(ip);
    document.getElementById('qrUrl').textContent = 'http://' + ip + ':' + _qrPort;
}
async function openHotspotSettings() {
    try {
        const res = await fetch('/api/open-hotspot-settings', { method: 'POST' });
        const data = await res.json();
        if (!data.success) alert('Kon hotspot-instellingen niet openen: ' + (data.error || ''));
    } catch(e) { alert('Kon hotspot-instellingen niet openen.'); }
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
        const res = await fetch(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: data ? JSON.stringify(data) : '{}'
        });
        return await res.json();
    } catch(e) { console.error(e); return null; }
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
        record_mode: document.getElementById('dRecordMode').value,
        min_stable_seconds: parseFloat(document.getElementById('dMinStable').value) || 2,
        max_record_seconds: parseFloat(document.getElementById('dMaxRec').value) || 20,
        auto_sensitivity: parseFloat(document.getElementById('dSensitivity').value) || 1,
        start_note: parseInt(document.getElementById('dStartNote').value),
        end_note: parseInt(document.getElementById('dEndNote').value),
        bass_treble_split: document.getElementById('dBasDiscant').checked,
        split_note: parseInt(document.getElementById('dSplitNote').value),
        split_record_bas: document.getElementById('dSplitBas').checked,
        split_record_disc: document.getElementById('dSplitDisc').checked
    };
    await dApi('/api/settings', data);
}

// ── Exporteren (.organ voor JM-Orgue + project-JSON) ──
async function dExportOrgan() {
    const el = document.getElementById('dExportResult');
    el.textContent = '…';
    const res = await dApi('/api/export-organ');
    if (res && res.success) {
        let msg = tr('.organ opgeslagen:') + ' ' + res.path +
            ' — ' + res.stops + ' ' + tr('registers') + ', ' + res.pipes_found + ' ' + tr('samples');
        if (res.pipes_missing > 0) msg += ', ' + res.pipes_missing + ' ' + tr('ontbrekend');
        if (res.flac_warning) msg += ' — ' + tr('Let op: JM-Orgue ondersteunt geen FLAC; kies WAV of MP3 als formaat');
        el.textContent = msg;
        el.style.color = (res.pipes_missing > 0 || res.flac_warning) ? '#f59e0b' : 'var(--accent)';
    } else {
        el.textContent = tr('Export mislukt:') + ' ' + ((res && res.error) || '?');
        el.style.color = 'var(--recording)';
    }
}
async function dExportProject() {
    const el = document.getElementById('dExportResult');
    el.textContent = '…';
    const res = await dApi('/api/export-project');
    if (res && res.success) {
        el.textContent = tr('Project-JSON opgeslagen:') + ' ' + res.path;
        el.style.color = 'var(--accent)';
    } else {
        el.textContent = tr('Export mislukt:') + ' ' + ((res && res.error) || '?');
        el.style.color = 'var(--recording)';
    }
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
        document.getElementById('dRecordMode').value = s.record_mode || 'fixed';
        document.getElementById('dAutoGroup').style.display = (s.record_mode === 'auto') ? '' : 'none';
        document.getElementById('dMinStable').value = s.min_stable_seconds != null ? s.min_stable_seconds : 2;
        document.getElementById('dMaxRec').value = s.max_record_seconds != null ? s.max_record_seconds : 20;
        document.getElementById('dSensitivity').value = s.auto_sensitivity != null ? s.auto_sensitivity : 1;
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

let dViewKb = null;
let _dRegSig = '';
const D_STATUS_COLORS = { todo:'#ef4444', partial:'#f59e0b', review:'#a855f7', done:'#22c55e' };
function dSelectKbView(n){ dViewKb = n; _dRegSig=''; if (window._lastState) dRenderPlan(window._lastState); }
async function dSelectReg(kb, reg, variant){ await dApi('/api/select-register', { keyboard:kb, register:reg, variant:variant }); }
function dRenderPlan(state){
    const plan = state.plan || [];
    const ksel = document.getElementById('mKbSelector');
    if (!ksel) return;
    if (!dViewKb || !plan.find(k=>k.name===dViewKb))
        dViewKb = (state.active && state.active.keyboard) || (plan[0] && plan[0].name) || null;
    const sig = JSON.stringify({ v:dViewKb, a:state.active,
        p: plan.map(k=>[k.name, k.registers.map(r=>r.series.map(s=>s.recorded+'/'+s.expected+'/'+s.status))]) });
    if (sig === _dRegSig) return;
    _dRegSig = sig;
    ksel.innerHTML = plan.map(k=>'<button class="mc-tab'+(k.name===dViewKb?' active':'')+'" onclick="dSelectKbView(\''+k.name+'\')">'+k.name+'</button>').join('');
    const kb = plan.find(k=>k.name===dViewKb);
    const list = document.getElementById('mRegList');
    if (!list) return;
    if (!kb || !kb.registers.length){ list.innerHTML = '<span style="color:var(--dim);font-size:0.85rem;">'+tr('Geen registers')+'</span>'; return; }
    const act = state.active || {};
    list.innerHTML = kb.registers.map(r=>r.series.map(s=>{
        const isAct = act.keyboard===kb.name && act.register===r.name && act.variant===s.variant;
        const col = D_STATUS_COLORS[s.status] || 'var(--dim)';
        return '<span class="mc-reg'+(isAct?' active':'')+'" style="border-color:'+col+';" onclick="dSelectReg(\''+kb.name+'\',\''+r.name+'\',\''+s.variant+'\')">'+
            '<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:'+col+';margin-right:6px;"></span>'+
            r.display+(s.variant==='trem'?' (trem)':'')+' <b style="color:'+col+';">'+s.recorded+'/'+s.expected+'</b></span>';
    }).join('')).join('');
}

function updateUI(state) {
    // State badge
    const badge = document.getElementById('stateBadge');
    const _stMap = {idle:'GEREED', countdown:'AFTELLEN', recording:'OPNAME', paused:'GEPAUZEERD'};
    badge.textContent = tr(_stMap[state.state] || state.state.toUpperCase());
    badge.className = 'state-badge ' + (state.state === 'recording' ? 'recording' : state.state === 'countdown' ? 'countdown' : '');

    // Main register selector + gate record button
    dRenderPlan(state);
    handleCheckPrompt(state);
    const mRec = document.getElementById('mRecBtn');
    if (mRec) {
        const hasActive = !!(state.active && state.active.register);
        mRec.disabled = !hasActive;
    }

    // Error banner
    const errBanner = document.getElementById('errorBanner');
    if (errBanner) {
        if (state.last_error) {
            errBanner.textContent = '⚠ ' + state.last_error;
            errBanner.classList.add('show');
        } else {
            errBanner.classList.remove('show');
        }
    }

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
    
    // Intelligent (assistive) auto-mode status + "laat los" cue
    const as = document.getElementById('autoStatus');
    if (as) {
        const ph = state.auto_phase;
        if (state.record_mode === 'auto' && state.state === 'recording' && ph && ph !== 'idle') {
            as.style.display = '';
            as.className = 'auto-status ' + ph;
            if (ph === 'waiting') {
                as.innerHTML = '◉ ' + tr('Wachten op toon…');
            } else if (ph === 'stabilizing') {
                const pct = Math.round((state.stable_progress || 0) * 100);
                as.innerHTML = '◉ ' + tr('Stabiliseren…') + ' ' + pct + '%' +
                    '<div class="as-bar"><div style="width:' + pct + '%"></div></div>';
            } else if (ph === 'hold') {
                as.innerHTML = '✓ ' + tr('Genoeg — laat los');
            } else if (ph === 'release') {
                as.innerHTML = tr('Uitklank opnemen…');
            }
        } else {
            as.style.display = 'none';
        }
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
        window._lastState = state;
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

// ============ Startup + Setup Wizard ============
const WIZ_TOTAL = 10;
let wizStepN = 1;
let wizData = {};
let wizDevices = [];
function wizVal(id){ const e=document.getElementById(id); return e?e.value:''; }
function wizEsc(s){ return (s||'').toString().replace(/"/g,'&quot;'); }

function wizResetData(){
    wizData = {
        output_dir:'', plaats:'', kerk:'', bouwer:'', folder_edited:false,
        n_keyboards:2, has_pedal:true, keyboards:[], pedal_registers:[],
        tremulant_scope:'none', split_note:60, couplers:[],
        settings:{ sample_rate:48000, bit_depth:16, channels:1, output_format:'mp3',
            mp3_bitrate:192, countdown_seconds:5, record_seconds:5, record_gain:1.0,
            record_mode:'fixed', min_stable_seconds:2, max_record_seconds:20, auto_sensitivity:1,
            device_indices:[], device_names:{}, input_mode:'mic', loopback_device_id:null }
    };
}

async function wizBootstrap(){
    try {
        const s = await (await fetch('/api/state')).json();
        if (s.has_manifest) return;  // already set up this session
    } catch(e){}
    try {
        const lp = await (await fetch('/api/last-project')).json();
        if (lp && lp.exists){
            document.getElementById('startupText').textContent =
                'Laatste orgel: ' + (lp.plaats || lp.organ || '') + (lp.kerk ? ' – ' + lp.kerk : '');
            document.getElementById('startupContinueBtn').style.display = '';
            document.getElementById('startupOverlay').style.display = 'flex';
            return;
        }
    } catch(e){}
    wizStartNew();
}

async function startupContinue(){
    try {
        const r = await (await fetch('/api/load-project', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'})).json();
        if (!r.success){ alert('Kon orgel niet laden: ' + (r.error || 'onbekende fout')); return; }
    } catch(e){ alert('Kon orgel niet laden: ' + e); return; }
    document.getElementById('startupOverlay').style.display = 'none';
}

async function wizStartNew(){
    wizResetData();
    try { const s = await (await fetch('/api/state')).json(); wizData.output_dir = s.output_dir || ''; } catch(e){}
    document.getElementById('startupOverlay').style.display = 'none';
    document.getElementById('wizOverlay').style.display = 'flex';
    wizStepN = 1; wizShow();
}

function wizShow(){
    document.getElementById('wizStepLabel').textContent = tr('Stap') + ' ' + wizStepN + ' / ' + WIZ_TOTAL;
    document.getElementById('wizProgress').style.width = (wizStepN/WIZ_TOTAL*100) + '%';
    for (let i=1;i<=WIZ_TOTAL;i++){ const el=document.getElementById('wizStep'+i); if(el) el.style.display='none'; }
    const cur = document.getElementById('wizStep'+wizStepN); if (cur) cur.style.display='block';
    if (wizStepN===1) document.getElementById('wizOutputDir').value = wizData.output_dir;
    if (wizStepN===2) wizBuildMics();
    if (wizStepN===3){ document.getElementById('wizPlaats').value = wizData.plaats; wizUpdateFolder(); }
    if (wizStepN===4) document.getElementById('wizKerk').value = wizData.kerk;
    if (wizStepN===5){ document.getElementById('wizBouwer').value = wizData.bouwer; wizUpdateFolder(); }
    if (wizStepN===6){ document.getElementById('wizNkb').value = wizData.n_keyboards; document.getElementById('wizHasPedal').checked = wizData.has_pedal; }
    if (wizStepN===7) wizBuildKbNames();
    if (wizStepN===8){ document.getElementById('wizTremScope').value = wizData.tremulant_scope; wizBuildTrem(); }
    if (wizStepN===9) wizBuildRegisters();
    if (wizStepN===10) wizBuildSettings();
    document.getElementById('wizPrev').style.visibility = wizStepN>1 ? 'visible' : 'hidden';
    document.getElementById('wizNext').textContent = tr(wizStepN===WIZ_TOTAL ? 'Opslaan & starten' : 'Volgende →');
}

async function wizPickFolder(){
    try {
        const r = await (await fetch('/api/pick-folder',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
        if (r.success && r.path){ document.getElementById('wizOutputDir').value = r.path; wizData.output_dir = r.path; }
        else if (r.error){ alert('Mapkiezer fout: '+r.error); }
    } catch(e){ alert('Kon mapkiezer niet openen: '+e); }
}
function wizForward(){ wizCollectStep(wizStepN); if (wizStepN===WIZ_TOTAL){ wizCommit(); return; } wizStepN++; wizShow(); }
function wizBack(){ wizCollectStep(wizStepN); if (wizStepN>1){ wizStepN--; wizShow(); } }

function wizCollectStep(n){
    if (n===1) wizData.output_dir = wizVal('wizOutputDir');
    else if (n===2) wizCollectMics();
    else if (n===3){ wizData.plaats = wizVal('wizPlaats'); wizUpdateFolder(); }
    else if (n===4) wizData.kerk = wizVal('wizKerk');
    else if (n===5){ wizData.bouwer = wizVal('wizBouwer'); wizUpdateFolder(); }
    else if (n===6){ wizData.n_keyboards = Math.max(1, parseInt(wizVal('wizNkb'))||1);
        wizData.has_pedal = document.getElementById('wizHasPedal').checked; wizEnsureKeyboards(); }
    else if (n===10) wizCollectSettings();
}

function wizUpdateFolder(){
    const p = (document.getElementById('wizPlaats') ? document.getElementById('wizPlaats').value : wizData.plaats) || '';
    const b = (document.getElementById('wizBouwer') ? document.getElementById('wizBouwer').value : wizData.bouwer) || '';
    const code = (p.replace(/\s+/g,'').slice(0,4) + b.replace(/\s+/g,'').slice(0,4)) || 'Orgel';
    const p1 = document.getElementById('wizFolderPrev1'); if (p1) p1.textContent = code;
    if (!wizData.folder_edited){ const f = document.getElementById('wizFolder'); if (f) f.value = code; }
}

// ---- Step 2: microphones ----
function wizToggleMicMode(){
    const mode = document.getElementById('wizMicMode').value;
    document.getElementById('wizMicList').style.display = mode==='mic' ? '' : 'none';
    document.getElementById('wizLoopbackList').style.display = mode==='loopback' ? '' : 'none';
}
async function wizBuildMics(){
    document.getElementById('wizMicMode').value = wizData.settings.input_mode || 'mic';
    wizToggleMicMode();
    const cont = document.getElementById('wizMicList');
    try {
        const devs = await (await fetch('/api/devices')).json();
        wizDevices = devs;
        if (!devs.length){ cont.innerHTML = '<p class="hint">Geen microfoons gevonden.</p>'; }
        else cont.innerHTML = devs.map(d => {
            const checked = wizData.settings.device_indices.indexOf(d.index)>=0 ? 'checked' : '';
            const pos = (wizData.settings.device_names && wizData.settings.device_names[d.index]) || '';
            return '<div class="wiz-row"><label class="wiz-check" style="flex:1;"><input type="checkbox" class="wizMicChk" data-idx="'+d.index+'" '+checked+'> '+d.name+'</label>'+
                   '<input type="text" class="wizMicPos" data-idx="'+d.index+'" placeholder="positie" value="'+wizEsc(pos)+'" style="max-width:150px;"></div>';
        }).join('');
    } catch(e){ cont.innerHTML = '<p class="hint">Kon apparaten niet laden.</p>'; }
    const lb = document.getElementById('wizLoopbackList');
    try {
        const sp = await (await fetch('/api/loopback-devices')).json();
        if (sp && sp.length){
            lb.innerHTML = '<label class="wiz-label">Speaker (loopback)</label><select class="wiz-select" id="wizLoopbackSel">' +
                sp.map(s=>'<option value="'+wizEsc(s.id)+'" '+(s.id===wizData.settings.loopback_device_id?'selected':'')+'>'+s.name+'</option>').join('') + '</select>';
        } else lb.innerHTML = '<p class="hint">Geen loopback-apparaten.</p>';
    } catch(e){ lb.innerHTML=''; }
}
function wizCollectMics(){
    const mode = document.getElementById('wizMicMode').value;
    wizData.settings.input_mode = mode;
    const idxs=[], names={};
    document.querySelectorAll('.wizMicChk').forEach(c=>{ if (c.checked) idxs.push(parseInt(c.dataset.idx)); });
    document.querySelectorAll('.wizMicPos').forEach(p=>{ if (p.value.trim()) names[p.dataset.idx]=p.value.trim(); });
    wizData.settings.device_indices = idxs;
    wizData.settings.device_names = names;
    const sel = document.getElementById('wizLoopbackSel');
    if (mode==='loopback' && sel) wizData.settings.loopback_device_id = sel.value;
}

// ---- Step 6/7: keyboards ----
function wizEnsureKeyboards(){
    const n = wizData.n_keyboards;
    const defaults = ['Hoofdwerk','Zwelwerk','Borstwerk','Rugwerk','Bovenwerk','Solo'];
    while (wizData.keyboards.length < n)
        wizData.keyboards.push({ name: defaults[wizData.keyboards.length] || ('Klavier'+(wizData.keyboards.length+1)),
                                 zwelwerk:false, tremulant:false, registers:[] });
    wizData.keyboards.length = n;
}
function wizBuildKbNames(){
    wizEnsureKeyboards();
    document.getElementById('wizKbNames').innerHTML = wizData.keyboards.map((kb,i)=>
        '<label class="wiz-label">'+tr('Klavier')+' '+(i+1)+'</label>'+
        '<input class="wiz-input" value="'+wizEsc(kb.name)+'" oninput="wizData.keyboards['+i+'].name=this.value">').join('');
}

// ---- Step 8: tremulant + swell ----
function wizBuildTrem(){
    const scope = document.getElementById('wizTremScope').value;
    wizData.tremulant_scope = scope;
    const per = document.getElementById('wizTremPerKb');
    if (scope==='keyboard'){
        per.innerHTML = wizData.keyboards.map((kb,i)=>
            '<label class="wiz-check"><input type="checkbox" '+(kb.tremulant?'checked':'')+
            ' onchange="wizData.keyboards['+i+'].tremulant=this.checked"> '+tr('Tremulant op')+' '+wizEsc(kb.name)+'</label>').join('');
    } else {
        per.innerHTML = '';
        const on = (scope==='organ');
        wizData.keyboards.forEach(kb=>kb.tremulant=on);
    }
    document.getElementById('wizZwelkast').innerHTML = wizData.keyboards.map((kb,i)=>
        '<label class="wiz-check"><input type="checkbox" '+(kb.zwelwerk?'checked':'')+
        ' onchange="wizData.keyboards['+i+'].zwelwerk=this.checked"> '+tr('Zwelkast op')+' '+wizEsc(kb.name)+'</label>').join('');
}

// ---- Step 9: registers ----
function wizRegArray(kbKey){ return kbKey==='P' ? wizData.pedal_registers : wizData.keyboards[parseInt(kbKey)].registers; }
function wizSetReg(kbKey,i,field,val){
    const arr = wizRegArray(kbKey);
    if (field==='begin_note' || field==='end_note'){
        val = parseInt(val)||0; arr[i][field]=val;
        const lbl = document.getElementById('wiz'+(field==='begin_note'?'Begin':'End')+'Lbl_'+kbKey+'_'+i);
        if (lbl) lbl.textContent = dMidiToName(val);
    } else arr[i][field] = val;
}
function wizRegRow(kbKey,i,r){
    return '<tr>'+
      '<td><input type="text" value="'+wizEsc(r.display)+'" oninput="wizSetReg(\''+kbKey+'\','+i+',\'display\',this.value)" placeholder="Prestant"></td>'+
      '<td style="width:55px;"><input type="text" value="'+wizEsc(r.foot)+'" oninput="wizSetReg(\''+kbKey+'\','+i+',\'foot\',this.value)" placeholder="8"></td>'+
      '<td style="width:64px;"><input type="number" value="'+r.begin_note+'" oninput="wizSetReg(\''+kbKey+'\','+i+',\'begin_note\',this.value)"></td>'+
      '<td><span class="wiz-notelbl" id="wizBeginLbl_'+kbKey+'_'+i+'">'+dMidiToName(r.begin_note)+'</span></td>'+
      '<td style="width:64px;"><input type="number" value="'+r.end_note+'" oninput="wizSetReg(\''+kbKey+'\','+i+',\'end_note\',this.value)"></td>'+
      '<td><span class="wiz-notelbl" id="wizEndLbl_'+kbKey+'_'+i+'">'+dMidiToName(r.end_note)+'</span></td>'+
      '<td style="text-align:center;"><input type="checkbox" '+(r.bass_treble?'checked':'')+' onchange="wizSetReg(\''+kbKey+'\','+i+',\'bass_treble\',this.checked)"></td>'+
      '<td><button class="wiz-del" onclick="wizDelRegister(\''+kbKey+'\','+i+')">×</button></td></tr>';
}
function wizRegBlock(kbKey,name,regs){
    const rows = regs.map((r,i)=>wizRegRow(kbKey,i,r)).join('');
    return '<div class="wiz-kb-block"><h3>'+wizEsc(name)+'</h3>'+
      '<table class="wiz-reg-table"><tr><th>'+tr('Naam')+'</th><th>'+tr('Voet')+'</th><th>'+tr('Begin')+'</th><th></th><th>'+tr('Eind')+'</th><th></th><th>'+tr('Bas/disc')+'</th><th></th></tr>'+rows+'</table>'+
      '<button class="wiz-mini-btn" style="margin-top:8px;" onclick="wizAddRegister(\''+kbKey+'\')">'+tr('+ Register')+'</button></div>';
}
function wizBuildRegisters(){
    let html = '';
    wizData.keyboards.forEach((kb,i)=>{ html += wizRegBlock(String(i), kb.name, kb.registers); });
    if (wizData.has_pedal) html += wizRegBlock('P','Pedaal', wizData.pedal_registers);
    document.getElementById('wizRegisters').innerHTML = html;
}
function wizAddRegister(kbKey){
    wizRegArray(kbKey).push({ display:'', foot:'', begin_note:36, end_note:96, bass_treble:false });
    wizBuildRegisters();
}
function wizDelRegister(kbKey,i){ wizRegArray(kbKey).splice(i,1); wizBuildRegisters(); }

// ---- Step 10: settings + couplers ----
function wizBuildSettings(){
    const s = wizData.settings;
    document.getElementById('wizSampleRate').value = s.sample_rate;
    document.getElementById('wizBitDepth').value = s.bit_depth;
    document.getElementById('wizChannels').value = s.channels;
    document.getElementById('wizFormat').value = s.output_format;
    document.getElementById('wizCountdown').value = s.countdown_seconds;
    document.getElementById('wizRecDur').value = s.record_seconds;
    document.getElementById('wizRecordMode').value = s.record_mode || 'fixed';
    document.getElementById('wizAutoGroup').style.display = (s.record_mode === 'auto') ? '' : 'none';
    document.getElementById('wizMinStable').value = s.min_stable_seconds != null ? s.min_stable_seconds : 2;
    document.getElementById('wizMaxRec').value = s.max_record_seconds != null ? s.max_record_seconds : 20;
    document.getElementById('wizSensitivity').value = s.auto_sensitivity != null ? s.auto_sensitivity : 1;
    document.getElementById('wizSplitNote').value = wizData.split_note;
    document.getElementById('wizSplitLbl').textContent = dMidiToName(wizData.split_note);
    const opts = wizData.keyboards.map(k=>'<option>'+wizEsc(k.name)+'</option>').join('') + (wizData.has_pedal?'<option>Pedaal</option>':'');
    document.getElementById('wizCouplerSrc').innerHTML = opts;
    document.getElementById('wizCouplerTgt').innerHTML = opts;
    wizRenderCouplers();
}
function wizCollectSettings(){
    const s = wizData.settings;
    s.sample_rate = parseInt(wizVal('wizSampleRate'));
    s.bit_depth = parseInt(wizVal('wizBitDepth'));
    s.channels = parseInt(wizVal('wizChannels'));
    s.output_format = wizVal('wizFormat');
    s.countdown_seconds = parseInt(wizVal('wizCountdown'))||5;
    s.record_seconds = parseInt(wizVal('wizRecDur'))||5;
    s.record_mode = wizVal('wizRecordMode') || 'fixed';
    s.min_stable_seconds = parseFloat(wizVal('wizMinStable'))||2;
    s.max_record_seconds = parseFloat(wizVal('wizMaxRec'))||20;
    s.auto_sensitivity = parseFloat(wizVal('wizSensitivity'))||1;
    wizData.split_note = parseInt(wizVal('wizSplitNote'))||60;
}
function wizAddCoupler(){
    const src = wizVal('wizCouplerSrc'), tgt = wizVal('wizCouplerTgt');
    if (src && tgt && src!==tgt){ wizData.couplers.push({source:src, target:tgt}); wizRenderCouplers(); }
}
function wizDelCoupler(i){ wizData.couplers.splice(i,1); wizRenderCouplers(); }
function wizRenderCouplers(){
    document.getElementById('wizCouplerList').innerHTML = wizData.couplers.map((c,i)=>
        '<div class="wiz-row">'+wizEsc(c.source)+' → '+wizEsc(c.target)+' <button class="wiz-del" onclick="wizDelCoupler('+i+')">×</button></div>').join('');
}

async function wizCommit(){
    wizCollectSettings();
    const payload = {
        output_dir: wizData.output_dir,
        folder_name: (document.getElementById('wizFolder').value || '').trim(),
        plaats: wizData.plaats, kerk: wizData.kerk, bouwer: wizData.bouwer,
        tremulant_scope: wizData.tremulant_scope, has_pedal: wizData.has_pedal,
        split_note: wizData.split_note, split_record_bas:true, split_record_disc:true,
        keyboards: wizData.keyboards.map(k=>({ name:k.name, zwelwerk:k.zwelwerk, tremulant:k.tremulant,
            registers:k.registers.filter(r=>(r.display||'').trim()).map(r=>({
                display:r.display, foot:r.foot, begin_note:r.begin_note, end_note:r.end_note, bass_treble:r.bass_treble })) })),
        pedal_registers: wizData.has_pedal ? wizData.pedal_registers.filter(r=>(r.display||'').trim()).map(r=>({
            display:r.display, foot:r.foot, begin_note:r.begin_note, end_note:r.end_note, bass_treble:r.bass_treble })) : [],
        couplers: wizData.couplers, settings: wizData.settings
    };
    try {
        const r = await (await fetch('/api/commit-organ',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})).json();
        if (r.success){ document.getElementById('wizOverlay').style.display='none'; }
        else alert('Kon orgel niet opslaan: '+(r.error||''));
    } catch(e){ alert('Fout bij opslaan: '+e); }
}

// ============ Register manager modal ============
function regOpen(){
    const st = window._lastState;
    const sel = document.getElementById('regKbSel');
    const plan = (st && st.plan) || [];
    sel.innerHTML = plan.map(k=>'<option>'+k.name+'</option>').join('');
    if (st && st.active && st.active.keyboard) sel.value = st.active.keyboard;
    regRender();
}
const REG_STATUS_COLORS = { todo:'#ef4444', partial:'#f59e0b', review:'#a855f7', done:'#22c55e' };
const REG_STATUS_LBL = { todo:'nog op te nemen', partial:'niet compleet', review:'nog controleren', done:'goed' };
function regRender(){
    const st = window._lastState;
    const kbName = document.getElementById('regKbSel').value;
    const plan = (st && st.plan) || [];
    const kb = plan.find(k=>k.name===kbName);
    const list = document.getElementById('regList');
    const legend = '<div style="font-size:0.7rem;color:var(--dim);margin-bottom:8px;display:flex;gap:10px;flex-wrap:wrap;">'+
        Object.keys(REG_STATUS_LBL).map(k=>'<span><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:'+REG_STATUS_COLORS[k]+';margin-right:4px;"></span>'+tr(REG_STATUS_LBL[k])+'</span>').join('')+'</div>';
    if (!kb || !kb.registers.length){ list.innerHTML = legend + '<p style="color:var(--dim);font-size:0.85rem;">'+tr('Nog geen registers.')+'</p>'; return; }
    list.innerHTML = legend + kb.registers.map(r=>{
        const series = r.series.map(s=>{
            const col = REG_STATUS_COLORS[s.status]||'var(--dim)';
            const full = s.recorded>=s.expected && s.expected>0;
            const chk = '<label style="font-size:0.7rem;color:var(--dim);'+(full?'':'opacity:0.4;')+'"><input type="checkbox" '+(s.checked?'checked':'')+' '+(full?'':'disabled')+' onchange="regMark(\''+kbName+'\',\''+r.name+'\',\''+s.variant+'\',this.checked)"> '+tr('gecontroleerd')+'</label>';
            return '<div style="display:flex;align-items:center;gap:8px;margin-top:3px;">'+
                '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:'+col+';"></span>'+
                '<span style="font-size:0.75rem;">'+tr(s.variant==='trem'?'tremulant':'normaal')+': <b style="color:'+col+';">'+s.recorded+'/'+s.expected+'</b></span>'+chk+'</div>';
        }).join('');
        return '<div class="d-checkbox-row" style="flex-direction:column;align-items:stretch;border-bottom:1px solid var(--border);padding:6px 0;">'+
            '<div style="display:flex;justify-content:space-between;"><span><b>'+r.display+'</b> '+(r.foot||'')+' ['+r.begin_note+'–'+r.end_note+'] '+(r.bass_treble?'(bas/disc)':'')+'</span>'+
            '<button class="wiz-del" onclick="regDel(\''+kbName+'\',\''+r.name+'\')">×</button></div>'+series+'</div>';
    }).join('');
}
async function regMark(kbName, regName, variant, checked){
    await dApi('/api/mark-register', { keyboard:kbName, register:regName, variant:variant, checked:checked });
    setTimeout(regOpen, 150);
}

// ============ Controle-prompt (na voltooien register) ============
let _checkKey = null;
function handleCheckPrompt(state){
    const cp = state.check_prompt;
    const modal = document.getElementById('checkModal');
    if (!modal) return;
    if (cp){
        const key = cp.keyboard+'|'+cp.register+'|'+cp.variant;
        if (key !== _checkKey){
            _checkKey = key;
            window._checkPrompt = cp;
            document.getElementById('checkMsg').innerHTML =
                tr('Register')+' <b>'+cp.display+'</b>'+(cp.variant==='trem'?' (trem)':'')+' '+tr('op')+' <b>'+cp.keyboard+'</b> '+tr('is volledig opgenomen')+' ('+cp.recorded+'/'+cp.expected+').<br>'+tr('Wil je het nu controleren?');
            modal.classList.add('active');
        }
    } else {
        _checkKey = null;
    }
}
async function checkLater(){ document.getElementById('checkModal').classList.remove('active'); await dApi('/api/dismiss-check'); }
async function checkApprove(){
    const cp = window._checkPrompt;
    document.getElementById('checkModal').classList.remove('active');
    if (cp) await dApi('/api/mark-register', { keyboard:cp.keyboard, register:cp.register, variant:cp.variant, checked:true });
    await dApi('/api/dismiss-check');
}
async function checkDoReview(){
    document.getElementById('checkModal').classList.remove('active');
    await dApi('/api/dismiss-check');
    _dRevScope = 'register';
    openModal('reviewModal');
    await dStartReview();
}
async function regAdd(){
    const kbName = document.getElementById('regKbSel').value;
    const name = document.getElementById('regNewName').value.trim();
    if (!name){ alert('Geef een registernaam'); return; }
    await dApi('/api/add-register', { keyboard:kbName, name:name,
        foot:document.getElementById('regNewFoot').value,
        begin_note:parseInt(document.getElementById('regNewBegin').value)||36,
        end_note:parseInt(document.getElementById('regNewEnd').value)||96,
        bass_treble:document.getElementById('regNewBass').checked });
    document.getElementById('regNewName').value='';
    setTimeout(regOpen, 150);
}
async function regDel(kbName, regName){
    if (!confirm('Register "'+regName+'" uit het plan verwijderen? (opnames blijven op schijf)')) return;
    await dApi('/api/remove-register', { keyboard:kbName, register:regName });
    setTimeout(regOpen, 150);
}

// ---- i18n init ----
jmInitLang();
(function(){ const ql=new URLSearchParams(location.search).get('lang'); if(ql && window.jmLangs.indexOf(ql)>=0){ try{localStorage.setItem('jmLang',ql);}catch(e){} jmInitLang(); } })();
try { document.getElementById('langSel').innerHTML = jmLangSelectorHtml(); } catch(e){}
translateTree(document.body);
try { applyHandleiding(); } catch(e){}

wizBootstrap();
</script>
</body>
</html>"""


REMOTE_HTML = r"""
<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>JM-Rec Remote</title>
<script src="/i18n.js"></script>
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

/* Error banner */
.error-banner {
    display: none;
    text-align: center;
    font-size: 0.9rem;
    margin: 10px 0 4px;
    padding: 9px 14px;
    border-radius: 10px;
    background: rgba(255, 59, 92, 0.12);
    border: 1px solid var(--recording);
    color: var(--recording);
    overflow-wrap: anywhere;
    word-break: break-word;
}
.error-banner.show { display: block; }

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
    <div style="display:flex;align-items:center;gap:10px;">
        <span id="langSel"></span>
        <div class="connection-dot" id="connectionDot"></div>
    </div>
</div>

<div class="tabs">
    <div class="tab active" onclick="switchTab('control')">Bediening</div>
    <div class="tab" onclick="switchTab('review')">Controle</div>
</div>

<!-- CONTROL TAB -->
<div class="tab-content active" id="tab-control">
    <div class="section">
        <div class="status-card">
            <div class="state-label" id="rStateLabel">IDLE</div>
            <div class="error-banner" id="rErrorBanner"></div>
            <div class="current-note" id="rNoteName">—</div>
            <div class="filename-label" id="rFilename">—</div>
            <div class="countdown-big" id="rCountdown"></div>

            <div id="rAutoStatus" style="display:none;text-align:center;font-weight:600;margin:6px 0;padding:12px;border-radius:12px;border:1px solid var(--border);"></div>
            <style>
            #rAutoStatus.waiting { color:var(--dim); }
            #rAutoStatus.stabilizing { color:#f59e0b; border-color:#f59e0b; }
            #rAutoStatus.hold { color:#fff; background:#22c55e; border-color:#22c55e; font-size:1.35rem;
                box-shadow:0 0 0 4px rgba(34,197,94,0.25); }
            #rAutoStatus.release { color:var(--dim); }
            #rAutoStatus .as-bar { height:6px; border-radius:6px; background:rgba(245,158,11,0.25); margin-top:8px; overflow:hidden; }
            #rAutoStatus .as-bar > div { height:100%; background:#f59e0b; transition:width .15s; }
            </style>

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

    <div class="section" id="rCheckBanner" style="display:none;">
        <div style="background:rgba(34,197,94,0.12);border:1px solid #22c55e;border-radius:10px;padding:12px;text-align:center;">
            <div id="rCheckMsg" style="font-size:0.9rem;margin-bottom:10px;">—</div>
            <div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap;">
                <button class="btn" onclick="rCheckReview()">🔍 Controleren</button>
                <button class="btn" style="border-color:#22c55e;color:#22c55e;" onclick="rCheckApprove()">✓ Goedgekeurd</button>
                <button class="btn" onclick="rCheckLater()">Later</button>
            </div>
        </div>
    </div>

    <div class="section">
        <div class="section-title">Opname</div>
        <div class="controls">
            <div class="controls-main">
                <button class="btn btn-primary" id="rRecBtn" onclick="apiCall('/api/record')">▶ Opnemen</button>
                <button class="btn" onclick="apiCall('/api/pause')">⏸ Pauze</button>
                <button class="btn btn-danger" onclick="apiCall('/api/stop')">■ Stop</button>
            </div>
            <div class="controls-row">
                <button class="btn" onclick="apiCall('/api/prev')">◀ Vorige noot</button>
                <button class="btn" onclick="apiCall('/api/redo')">↻ Opnieuw</button>
                <button class="btn" onclick="apiCall('/api/next')">Volgende noot ▶</button>
            </div>
        </div>
    </div>

    <div class="section">
        <div class="section-title">Register kiezen</div>
        <div style="font-size:0.68rem;color:var(--dim);margin-bottom:8px;display:flex;gap:10px;flex-wrap:wrap;">
            <span><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#ef4444;margin-right:4px;"></span>nog op te nemen</span>
            <span><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#f59e0b;margin-right:4px;"></span>niet compleet</span>
            <span><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#a855f7;margin-right:4px;"></span>controleren</span>
            <span><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#22c55e;margin-right:4px;"></span>goed</span>
        </div>
        <div id="rKbSelector" style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px;"></div>
        <div id="rRegList"></div>
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
    label.textContent = tr({idle:'GEREED', countdown:'AFTELLEN', recording:'OPNAME', paused:'GEPAUZEERD'}[state.state] || state.state.toUpperCase());
    label.className = 'state-label ' + (state.state === 'recording' ? 'recording' : state.state === 'countdown' ? 'countdown' : '');

    // Error banner
    const rErr = document.getElementById('rErrorBanner');
    if (rErr) {
        if (state.last_error) {
            rErr.textContent = '⚠ ' + state.last_error;
            rErr.classList.add('show');
        } else {
            rErr.classList.remove('show');
        }
    }

    // Note
    const note = document.getElementById('rNoteName');
    note.textContent = state.note.current_name;
    note.className = 'current-note ' + (state.state === 'recording' ? 'recording' : state.state === 'countdown' ? 'countdown' : '');
    
    // Filename
    document.getElementById('rFilename').textContent = state.note.current_filename;
    
    // Countdown
    const cd = document.getElementById('rCountdown');
    cd.textContent = state.state === 'countdown' && state.countdown > 0 ? state.countdown : '';

    // Intelligent (assistive) auto-mode status + "laat los" cue
    const ras = document.getElementById('rAutoStatus');
    if (ras) {
        const ph = state.auto_phase;
        if (state.record_mode === 'auto' && state.state === 'recording' && ph && ph !== 'idle') {
            ras.style.display = '';
            ras.className = ph;
            if (ph === 'waiting') {
                ras.innerHTML = '◉ ' + tr('Wachten op toon…');
            } else if (ph === 'stabilizing') {
                const pct = Math.round((state.stable_progress || 0) * 100);
                ras.innerHTML = '◉ ' + tr('Stabiliseren…') + ' ' + pct + '%' +
                    '<div class="as-bar"><div style="width:' + pct + '%"></div></div>';
            } else if (ph === 'hold') {
                ras.innerHTML = '✓ ' + tr('Genoeg — laat los');
            } else if (ph === 'release') {
                ras.innerHTML = tr('Uitklank opnemen…');
            }
        } else {
            ras.style.display = 'none';
        }
    }

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

    // Controle-prompt banner (na voltooien register)
    const cp = state.check_prompt;
    const cb = document.getElementById('rCheckBanner');
    if (cb) {
        if (cp) {
            window._rCheckPrompt = cp;
            document.getElementById('rCheckMsg').innerHTML = tr('Register')+' <b>'+cp.display+'</b>'+(cp.variant==='trem'?' (trem)':'')+' '+tr('is compleet')+' ('+cp.recorded+'/'+cp.expected+'). '+tr('Controleren?');
            cb.style.display = '';
        } else cb.style.display = 'none';
    }

    // Register-series selector + progress (restricted remote)
    rRenderRegisters(state);

    // Gate record button: a register series must be selected first
    const recBtn = document.getElementById('rRecBtn');
    if (recBtn) {
        const hasActive = !!(state.active && state.active.register);
        recBtn.disabled = !hasActive;
        recBtn.style.opacity = hasActive ? '1' : '0.5';
    }
}

// ---- Restricted remote: register-series selection ----
let rViewKb = null;
let _rLastState = null;
let _rRegSig = '';
const STATUS_COLORS = { todo:'#ef4444', partial:'#f59e0b', review:'#a855f7', done:'#22c55e' };
function rSelectKb(name){ rViewKb = name; _rRegSig=''; if (_rLastState) rRenderRegisters(_rLastState); }
async function rSelectReg(kb, reg, variant){
    await apiCall('/api/select-register', { keyboard: kb, register: reg, variant: variant });
}
async function rCheckApprove(){
    const cp = window._rCheckPrompt;
    if (cp) await apiCall('/api/mark-register', { keyboard:cp.keyboard, register:cp.register, variant:cp.variant, checked:true });
    await apiCall('/api/dismiss-check');
}
async function rCheckLater(){ await apiCall('/api/dismiss-check'); }
function rCheckReview(){ switchTab('review'); }
function rRenderRegisters(state){
    _rLastState = state;
    const plan = state.plan || [];
    if (!rViewKb || !plan.find(k => k.name === rViewKb))
        rViewKb = (state.active && state.active.keyboard) || (plan[0] && plan[0].name) || null;
    // Only rebuild the DOM when something actually changed, so taps aren't
    // eaten by a constant innerHTML rebuild on every poll.
    const sig = JSON.stringify({ v:rViewKb, a:state.active,
        p: plan.map(k=>[k.name, k.registers.map(r=>r.series.map(s=>s.recorded+'/'+s.expected+'/'+s.status))]) });
    if (sig === _rRegSig) return;
    _rRegSig = sig;
    const ksel = document.getElementById('rKbSelector');
    if (ksel) ksel.innerHTML = plan.map(k =>
        '<button class="btn ' + (k.name === rViewKb ? 'btn-primary' : '') + '" style="padding:6px 12px;" onclick="rSelectKb(\'' + k.name + '\')">' + k.name + '</button>').join('');
    const list = document.getElementById('rRegList');
    if (!list) return;
    const kb = plan.find(k => k.name === rViewKb);
    if (!kb || !kb.registers.length){
        list.innerHTML = '<p style="color:var(--dim);font-size:0.85rem;">'+tr('Geen registers gedefinieerd. Stel het orgel in op de master-PC.')+'</p>';
        return;
    }
    const act = state.active || {};
    list.innerHTML = kb.registers.map(r => r.series.map(s => {
        const isAct = act.keyboard === kb.name && act.register === r.name && act.variant === s.variant;
        const col = STATUS_COLORS[s.status] || 'var(--dim)';
        return '<div onclick="rSelectReg(\'' + kb.name + '\',\'' + r.name + '\',\'' + s.variant + '\')" ' +
            'style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:10px 12px;margin-bottom:6px;' +
            'border:2px solid ' + col + ';border-radius:10px;cursor:pointer;' + (isAct ? 'background:rgba(125,125,255,0.14);' : '') + '">' +
            '<span><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:' + col + ';margin-right:8px;"></span>' +
            r.display + (s.variant === 'trem' ? ' <span style="color:var(--dim)">(trem)</span>' : '') + (r.foot ? ' ' + r.foot : '') + '</span>' +
            '<span style="font-family:monospace;font-size:0.8rem;color:' + col + ';">' + s.recorded + '/' + s.expected + '</span></div>';
    }).join('')).join('');
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

// ---- i18n init ----
jmInitLang();
try { document.getElementById('langSel').innerHTML = jmLangSelectorHtml(); } catch(e){}
translateTree(document.body);

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
