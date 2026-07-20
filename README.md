<p align="center">
  <img src="https://img.shields.io/badge/version-3.8-blue?style=flat-square" alt="Version">
  <img src="https://img.shields.io/badge/platform-Windows-blue?style=flat-square" alt="Platform">
  <img src="https://img.shields.io/badge/remote-Android%20%7C%20iOS%20%7C%20Windows-green?style=flat-square" alt="Remote">
  <img src="https://img.shields.io/badge/output-JM--Orgue-orange?style=flat-square" alt="Output">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey?style=flat-square" alt="License">
</p>

# JM-Rec v3.8 — Organ Sample Recorder

**Neem pijporgels op, noot voor noot, met automatische doorloop en draadloze bediening.**

JM-Rec is een opnametool speciaal ontworpen voor het samplen van pijporgels. Het genereert MP3-bestanden met JM-Orgue-compatibele naamgeving en biedt een draadloze afstandsbediening via elke browser — Android, iOS of Windows.

### Nieuw in v3.8
- **Update-melding** — bij het opstarten controleert JM-Rec (op de achtergrond, faalt stil zonder internet) of er een nieuwere release op GitHub staat. Is die er, dan verschijnt een groene **⬆ Update**-knop in de header die direct naar de download linkt.
- **Automatische release-builds** — een GitHub Actions-workflow bouwt bij elke versietag (of handmatig via *Actions → Release → Run workflow*) de Windows-installer + portable zip, plus experimentele Linux- en macOS-archieven, en publiceert alles als GitHub-release.

### Nieuw in v3.7
- **Exporteer .organ (JM-Orgue)** — via **Instellingen → Exporteren** maakt JM-Rec een compleet `.organ`-definitiebestand in de projectmap: klavieren, registers (met voetmaat), zwelkasten, tremulanten en koppels, met verwijzingen naar de opgenomen samples. JM-Orgue laadt dit bestand direct. Ontbrekende noten blijven stil en kun je later alsnog opnemen (daarna opnieuw exporteren). Daarnaast blijft de export van projectgegevens (`.jm-rec.json`) beschikbaar.

### Nieuw in v3.6
- **Intelligent opnemen (assisterend)** — nieuwe **Opnamemodus** naast de vaste duur (alleen microfooningang). De recorder meet de ruisvloer, wacht op de toon en luistert of de klank **stabiel en loopbaar** is. Zodra dat zo is verschijnt een groen sein **"Genoeg — laat los"**; je laat de toets los, de **uitklank** wordt automatisch meegenomen tot stilte en hij gaat door naar de volgende noot. Bij een tremulant-reeks wacht hij op een stabiele tremulant-modulatie. Instelbaar: min. stabiele toon, max. duur (veiligheidsgrens) en gevoeligheid. Zichtbaar op zowel het display als de afstandsbediening.

### Nieuw in v3.5
- **Opstart-wizard** — je definieert het hele orgel vooraf in 10 stappen (locatie → microfoon → plaats/kerk/orgelbouwer → klavieren/pedaal → namen → tremulant/zwelkast → **registers met begin/eind-noot + voetmaat + bas/disc** → opname-instellingen + koppels). Bij opstart kies je: doorgaan met het laatste orgel óf een nieuw orgel.
- **Mapkiezer** — bij stap 1 opent de **Bladeren…**-knop de Windows-verkenner om de opslagmap te kiezen.
- **Registers vooraf gedefinieerd** — elk register heeft een eigen begin/eind-noot, voetmaat en bas/disc-instelling, bewaard in een projectbestand. Op een trem-klavier wordt elk register 2× opgenomen (normaal + `_trem`).
- **Vergrendelde afstandsbediening** — op de telefoon/tablet kies je alleen een register en bedien je opnemen/pauze/stop + noot vooruit/achteruit. Instellen gebeurt alleen op de master-PC.
- **Kleurcodes per register** — 🔴 nog op te nemen · 🟠 niet compleet · 🟣 nog te controleren · 🟢 goed (gecontroleerd). Zichtbaar op de PC én de afstandsbediening.
- **Directe verbinding (hotspot)** — geen WiFi nodig: laat de PC zelf een netwerk uitzenden en verbind telefoon/tablet rechtstreeks (via de QR-modal).

### Eerder toegevoegd (v3.x)
- **"Wat je hoort" opnamemodus** — neem systeemaudio op via WASAPI loopback in plaats van (of naast) een microfoon.
- **Automatische samplerate** — als een microfoon 44100 Hz weigert, schakelt JM-Rec automatisch naar de native rate (bv. 48000) zodat opnemen niet stil faalt.

---

## Features

- **Orgelstructuur** — stel klavieren, pedaal en registers in per orgel
- **Registernaam-automatisering** — "Holpijp 8 voet" wordt automatisch `Holpijp_8`, "Mixtuur 4 sterk" wordt `Mixtuur_4st`
- **Tremulant** — registermappen krijgen automatisch `_trem` suffix
- **"Wat je hoort"** — neem systeemaudio op via WASAPI loopback, direct vanuit een virtueel orgel op dezelfde PC
- **Multi-microfoon** — neem gelijktijdig op met meerdere microfoons (front, midden, rear) in aparte submappen
- **Automatische noot-doorloop** — telt af, neemt op, gaat door naar de volgende noot
- **Draadloze afstandsbediening** — bedien de opname vanaf je telefoon, tablet of tweede PC
- **Display-modus** — groot leesbaar scherm bij het orgel met noot, VU-meter en voortgang
- **QR-code** — scan om direct de remote te openen, geen URL overtypen
- **PC-instellingen** — alle parameters ook instelbaar via het display-scherm
- **JM-Orgue-compatibele naamgeving** — `036-c.mp3`, `037-c#.mp3`, etc.
- **.organ-export** — genereer een compleet orgeldefinitiebestand voor JM-Orgue, inclusief zwelkasten, tremulanten en koppels
- **Repareer & verwijder** — professionele installer met repair en uninstall
- **Geen terminal** — draait onzichtbaar op de achtergrond, browser sluiten = afsluiten

---

## Installatie

### Standalone (aanbevolen)

Download de nieuwste **`JM-Rec-Setup-x.x.exe`** van de [releases-pagina](https://github.com/orgelmaker/JM-Rec/releases/latest) en voer de installer uit. Geen Python of andere software vereist. (De laatste installer staat ook als `JM-Rec-Setup.exe` in de hoofdmap van deze repository.)

> Bij opnieuw uitvoeren van de setup kun je kiezen tussen **Repareren** of **Verwijderen**.
> Draai je JM-Rec al? Bij het opstarten verschijnt automatisch een **⬆ Update**-knop zodra er een nieuwere versie is.

### Vanuit broncode

```bash
# Vereisten: Python 3.10+, LAME of FFmpeg voor MP3-conversie
pip install -r requirements.txt
python jm_rec.py --port 5555
```

---

## Snel starten

1. **Start JM-Rec** via de snelkoppeling op het bureaublad
2. De browser opent het **display-scherm**. Bij een nieuw orgel start de **wizard**; als je eerder een orgel hebt opgenomen kies je **Doorgaan** of **Nieuw orgel**.
3. **Doorloop de wizard** (10 stappen). Gebruik bij stap 1 eventueel **Bladeren…** om de opslagmap te kiezen, en voer bij stap 9 per klavier de registers in (naam, voetmaat, begin/eind-noot, bas/disc).
4. Klik op **Opslaan & starten** — het orgel staat klaar.
5. Op het **hoofdscherm** kies je een register en druk je op **Opnemen** — de rest gaat automatisch.
6. **Scan de QR-code** ("QR Remote") met je telefoon om op afstand te bedienen.

> Achteraf bewerken doe je op de master-PC: knop **Registers** (toevoegen/verwijderen + gecontroleerd-markering) of **Nieuw orgel** (wizard opnieuw).

---

## Display (PC-scherm bij het orgel)

Na het starten opent de browser automatisch de display-pagina (`http://localhost:5555/display`).

Toont:
- Huidige noot en bestandsnaam
- Orgel / klavier / register in de header
- Aftelling en opname-indicator
- VU-meter en voortgangsbalk
- QR-code voor de afstandsbediening
- Instellingen-paneel (via Instellingen-knop)

---

## Afstandsbediening (Android / iOS / Windows)

Scan de QR-code op het display of open `http://<PC-IP>:5555` op een ander apparaat. Werkt op elk apparaat met een browser — telefoon, tablet of tweede PC.

De afstandsbediening is bewust **vergrendeld**: je kunt alleen opnemen en registers kiezen, niet het orgel of de instellingen wijzigen (dat gebeurt op de master-PC).

| Tabblad | Functie |
|---------|---------|
| **Bediening** | **Opnemen / Pauze / Stop**, **Vorige noot / Opnieuw / Volgende noot**, en de **registerkiezer** (kies klavier → register-reeks om op te nemen) |
| **Controle** | Samples terugluisteren / controleren |

### Register kiezen op afstand
Onder **Register kiezen** tik je eerst op een klavier en daarna op het register(reeks) dat je wilt opnemen. De gekozen reeks licht op en pas dan is **Opnemen** actief. Elke reeks toont de voortgang (`opgenomen/totaal`) en een **kleurcode**.

### Kleurcodes
| Kleur | Betekenis |
|-------|-----------|
| 🔴 rood | nog op te nemen (0 noten) |
| 🟠 oranje | begonnen, nog niet compleet |
| 🟣 paars | volledig opgenomen, nog te controleren |
| 🟢 groen | gecontroleerd en goedgekeurd |

Je markeert een reeks als **gecontroleerd** (paars → groen) op de master-PC via de knop **Registers**.

> **Automatische controle-vraag:** zodra een register volledig is opgenomen, verschijnt automatisch een venster: **Nu controleren** (opent de analyse), **Goedgekeurd** (zet direct op groen) of **Later**. Dit werkt op de PC én op de afstandsbediening.

### Directe verbinding zonder WiFi (hotspot)
Geen netwerk op locatie? Open op de PC **QR Remote** → sectie **Directe verbinding** → **Open hotspot-instellingen**, zet de Windows mobiele hotspot aan en verbind je telefoon/tablet met dat netwerk. Kies in de QR-modal het hotspot-netwerk en scan de code. Internet is niet nodig.

---

## Bestandsstructuur

```
Opslaglocatie/
├── Orgelnaam/
│   ├── Hoofdwerk/
│   │   ├── Prestant_8/
│   │   │   ├── 036-c.mp3
│   │   │   ├── 037-c#.mp3
│   │   │   └── ...
│   │   ├── Holpijp_8_trem/
│   │   │   ├── 036-c.mp3
│   │   │   └── ...
│   ├── Zwelwerk/
│   │   └── ...
│   ├── Pedaal/
│   │   └── ...
```

Bij **multi-microfoon** opnames worden submappen per positie aangemaakt:

```
├── Prestant_8/
│   ├── Front/
│   │   ├── 036-c.mp3
│   │   └── ...
│   ├── Midden/
│   │   ├── 036-c.mp3
│   │   └── ...
│   ├── Rear/
│   │   ├── 036-c.mp3
│   │   └── ...
```

Naamgeving volgt de **JM-Orgue**-conventie: `{MIDI-nummer}-{nootnaam}.mp3`

---

## Registernaam-formattering

| Invoer | Mapnaam |
|--------|---------|
| Holpijp 8 voet | `Holpijp_8` |
| Prestant 8' | `Prestant_8` |
| Mixtuur 4 sterk | `Mixtuur_4st` |
| Trompet 8 | `Trompet_8` |
| Holpijp 8 voet + tremulant | `Holpijp_8_trem` |

---

## Parameters

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

## Tips

- Gebruik een **condensatormicrofoon** voor de beste kwaliteit
- Neem op in **24-bit** voor maximale dynamiek
- Gebruik **Stereo** bij een AB- of ORTF-opstelling
- Zet de opnameduur lang genoeg voor langzaam sprekende pijpen (10+ sec voor 16')
- Zorg dat PC en telefoon op **hetzelfde netwerk** zitten (WiFi of hotspot)
- Bij multi-mic: geef elke microfoon een duidelijke **positienaam** (Front, Midden, Rear)
- Converteer MP3 naar WAV indien gewenst:
  ```bash
  for %f in (*.mp3) do ffmpeg -i "%f" "%~nf.wav"
  ```

---

## Commandoregel

```
JM-Rec.exe [opties]

  --port PORT       Webserver poort (standaard: 5555)
  --host HOST       Host om op te binden (standaard: 0.0.0.0)
  --project NAAM    Projectnaam
  --register NAAM   Registernaam
  --output PAD      Opslaglocatie
```

---

## Vereisten

| | Standalone | Broncode |
|---|---|---|
| **Windows** | 10/11 (64-bit) | 10/11 (64-bit) |
| **Python** | Niet nodig | 3.10+ |
| **MP3-encoder** | Ingebouwd | LAME of FFmpeg |
| **soundcard** | Ingebouwd | `pip install soundcard` (optioneel, voor "Wat je hoort") |
| **Netwerk** | WiFi voor remote | WiFi voor remote |

---

## Zelf bouwen

Zie **[BUILD.md](BUILD.md)** voor volledige build-instructies (venv, PyInstaller, Inno Setup).

Snelstart:
```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
pip install pyinstaller
setup\build.bat
```

## Release maken (beheerder)

Een nieuwe release bouwen en publiceren gaat automatisch via GitHub Actions:

1. Zet het versienummer in `jm_rec.py` (`JM_REC_VERSION`), de HTML-literals en `setup/jm_rec_setup.iss`
2. Commit en push naar `main`
3. **Trigger de build** op één van twee manieren:
   - Push een versietag: `git tag v3.8` gevolgd door `git push origin v3.8`, of
   - Handmatig: GitHub → **Actions** → **Release** → **Run workflow** (maakt zelf de tag)
4. De workflow bouwt de **Windows-installer** (Inno Setup) + **portable zip**, plus experimentele **Linux**- (tar.gz) en **macOS**-archieven (zip), en publiceert alles als GitHub-release met automatische release notes

> De tag moet overeenkomen met `JM_REC_VERSION`, anders stopt de workflow met een foutmelding. Linux/macOS zijn experimenteel: de app draait er, maar Windows-specifieke functies ("Wat je hoort"-loopback, mapkiezer, hotspot-koppeling) werken er niet. Een falende Linux/macOS-build blokkeert de Windows-release niet.
