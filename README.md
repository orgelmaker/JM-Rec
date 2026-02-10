<p align="center">
  <img src="https://img.shields.io/badge/platform-Windows-blue?style=flat-square" alt="Platform">
  <img src="https://img.shields.io/badge/remote-Android%20%7C%20iOS%20%7C%20Windows-green?style=flat-square" alt="Remote">
  <img src="https://img.shields.io/badge/output-GrandOrgue%20%7C%20Hauptwerk-orange?style=flat-square" alt="Output">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey?style=flat-square" alt="License">
</p>

# 🎵 JM-Rec — Organ Sample Recorder

**Neem pijporgels op, noot voor noot, met automatische doorloop en draadloze bediening.**

JM-Rec is een opnametool speciaal ontworpen voor het samplen van pijporgels. Het genereert MP3-bestanden met GrandOrgue/Hauptwerk-compatibele naamgeving en biedt een draadloze afstandsbediening via elke browser — Android, iOS of Windows.

---

## ✨ Features

- 🎹 **Automatische noot-doorloop** — telt af, neemt op, gaat door naar de volgende noot
- 📱 **Draadloze afstandsbediening** — bedien de opname vanaf je telefoon, tablet of tweede PC
- 📺 **Display-modus** — groot leesbaar scherm bij het orgel met noot, VU-meter en voortgang
- 🔲 **QR-code** — scan om direct de remote te openen, geen URL overtypen
- ⚙️ **PC-instellingen** — alle parameters ook instelbaar via het display-scherm
- 🗂️ **GrandOrgue/Hauptwerk-naamgeving** — `036-c.mp3`, `037-c#.mp3`, etc.
- 🔧 **Repareer & verwijder** — professionele installer met repair en uninstall
- 🚫 **Geen terminal** — draait onzichtbaar op de achtergrond, browser sluiten = afsluiten

---

## 📦 Installatie

### Standalone (aanbevolen)

Download **`JM-Rec-Setup.exe`** van deze link (https://github.com/orgelmaker/JM-Rec/blob/main/output/JM-Rec-Setup.exe) en voer de installer uit. Geen Python of andere software vereist.

> Bij opnieuw uitvoeren van de setup kun je kiezen tussen **Repareren** of **Verwijderen**.

### Vanuit broncode

```bash
# Vereisten: Python 3.10+, LAME of FFmpeg voor MP3-conversie
pip install -r requirements.txt
python jm_rec.py --port 5555
```

---

## 🚀 Snel starten

1. **Start JM-Rec** via de snelkoppeling op het bureaublad
2. De browser opent automatisch het **display-scherm**
3. **Scan de QR-code** met je telefoon om de afstandsbediening te openen
4. Stel **project** en **register** in via het Project-tabblad
5. Druk op **▶ Opnemen** — de rest gaat automatisch

---

## 🖥️ Display (PC-scherm bij het orgel)

Na het starten opent de browser automatisch de display-pagina (`http://localhost:5555/display`).

Toont:
- Huidige noot en bestandsnaam
- Aftelling en opname-indicator
- VU-meter en voortgangsbalk
- QR-code voor de afstandsbediening
- Instellingen-paneel (via ⚙️)

---

## 📱 Afstandsbediening (Android / iOS / Windows)

Scan de QR-code op het display of open `http://<PC-IP>:5555` op een ander apparaat.

Werkt op elk apparaat met een browser — telefoon, tablet of tweede PC.

| Tabblad | Functie |
|---------|---------|
| **Bediening** | Opnemen, Stop, Vorige, Opnieuw, Volgende |
| **Project** | Projectnaam, registernaam, opslaglocatie |
| **Instellingen** | Microfoon, samplerate, bitdiepte, opnameduur, nootbereik |

---

## 🗂️ Bestandsstructuur

```
Opslaglocatie/
├── ProjectNaam/
│   ├── Register_A/
│   │   ├── 036-c.mp3
│   │   ├── 037-c#.mp3
│   │   ├── 038-d.mp3
│   │   └── ...
│   ├── Register_B/
│   │   ├── 036-c.mp3
│   │   └── ...
```

Naamgeving volgt de **GrandOrgue/Hauptwerk**-conventie: `{MIDI-nummer}-{nootnaam}.mp3`

---

## ⚙️ Parameters

| Parameter | Standaard | Opties |
|-----------|-----------|--------|
| Samplerate | 44100 Hz | 44100 / 48000 / 96000 |
| Bitdiepte | 16-bit | 16 / 24 |
| Kanalen | Mono | Mono / Stereo |
| MP3 Bitrate | 192 kbps | 128 / 192 / 256 / 320 |
| Afteltijd | 5 sec | 1–30 |
| Opnameduur | 5 sec | 1–60 |
| Startnoot | MIDI 36 (C2) | 0–127 |
| Eindnoot | MIDI 96 (C7) | 0–127 |

---

## 💡 Tips

- Gebruik een **condensatormicrofoon** voor de beste kwaliteit
- Neem op in **24-bit** voor maximale dynamiek
- Gebruik **Stereo** bij een AB- of ORTF-opstelling
- Zet de opnameduur lang genoeg voor langzaam sprekende pijpen (10+ sec voor 16')
- Zorg dat PC en telefoon op **hetzelfde netwerk** zitten (WiFi of hotspot)
- Converteer MP3 naar WAV voor GrandOrgue:
  ```bash
  for %f in (*.mp3) do ffmpeg -i "%f" "%~nf.wav"
  ```

---

## 🖱️ Commandoregel

```
JM-Rec.exe [opties]

  --port PORT       Webserver poort (standaard: 5555)
  --host HOST       Host om op te binden (standaard: 0.0.0.0)
  --project NAAM    Projectnaam
  --register NAAM   Registernaam
  --output PAD      Opslaglocatie
```

---

## 📋 Vereisten

| | Standalone | Broncode |
|---|---|---|
| **Windows** | 10/11 (64-bit) | 10/11 (64-bit) |
| **Python** | Niet nodig | 3.10+ |
| **MP3-encoder** | Ingebouwd | LAME of FFmpeg |
| **Netwerk** | WiFi voor remote | WiFi voor remote |

---

## 🛠️ Zelf bouwen

```bash
# Installeer dependencies
pip install -r requirements.txt
pip install pyinstaller

# Bouw standalone exe
pyinstaller JM-Rec.spec --noconfirm --clean

# Bouw installer (Inno Setup vereist)
iscc dist/jm_rec_setup.iss
```
