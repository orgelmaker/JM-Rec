# JM-Rec v3.0 — Build-instructies

## Wat is nieuw in v3.0
- **"Wat je hoort" opnamemodus** — neem systeemaudio op via WASAPI loopback
  in plaats van (of naast) een microfoon. Ideaal voor het samplen vanuit
  Hauptwerk, GrandOrgue of andere software direct op dezelfde PC.

## Vereisten

| Software | Versie | Download |
|----------|--------|----------|
| Python | 3.10+ | https://python.org (vink **Add to PATH** aan!) |
| Inno Setup | 6 | https://jrsoftware.org/isdl.php |
| LAME of FFmpeg | — | voor MP3-conversie (optioneel, pydub fallback) |

## Stap voor stap

### 1. Repository klonen of kopiëren
```
git clone https://github.com/orgelmaker/JM-Rec.git
cd JM-Rec
```

### 2. Python-omgeving opzetten
```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
pip install pyinstaller
```

### 3. Bouwen
Gebruik het bestaande build-script:
```
setup\build.bat
```

Dit doet automatisch:
1. **PyInstaller** — bouwt `dist\JM-Rec.exe`
2. **Kopieert** README, icon en ISS-script naar `dist\`
3. **Inno Setup** — bouwt `output\JM-Rec-Setup.exe`
4. **Opruimen** — verwijdert `build\` en `dist\`

Het resultaat staat in:
```
output\JM-Rec-Setup.exe
```

### Handmatig bouwen (zonder Inno Setup)
Als je alleen de .exe nodig hebt zonder installer:
```
python -m PyInstaller JM-Rec.spec --noconfirm --clean
```
De executable staat dan in `dist\JM-Rec.exe`.

## Nieuwe dependency: soundcard

De `soundcard` library (voor WASAPI loopback) wordt automatisch meegenomen
door PyInstaller via de hidden imports in `JM-Rec.spec`. Geen extra stappen
nodig. Als `soundcard` niet geïnstalleerd is, werkt JM-Rec nog steeds —
alleen de "Wat je hoort"-modus is dan niet beschikbaar.

## Problemen oplossen

| Probleem | Oplossing |
|----------|-----------|
| `python` niet gevonden | Herinstalleer Python met **Add to PATH** aangevinkt |
| `pip install` faalt voor soundcard | `pip install comtypes` eerst, dan opnieuw |
| Inno Setup niet gevonden | Installeer in standaardlocatie of voeg `iscc` toe aan PATH |
| Build mislukt op soundcard imports | Controleer dat `comtypes` in de venv zit |
