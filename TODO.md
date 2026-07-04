# JM-Rec TODO — Integratie met JM-Orgue

> Status: 2026-07-04 — Alle integratie-features zijn af (v3.7). Workflow opnemen → exporteren → spelen in JM-Orgue is compleet.

## Afgerond

### 1. Zwelwerk per klavier ✅ KLAAR (v3.5)
- [x] Per klavier aangeven of het een **zwelwerk** is (wizard + master-edit)
- [x] Opslaan in de projectconfiguratie (manifest)
- [x] Bij export naar .organ: zwelwerk-klavieren krijgen een `[Enclosure]` sectie

### 2. Koppels definiëren ✅ KLAAR (v3.5)
- [x] Koppels toevoegen in wizard stap 10 + drawer
- [x] Per koppel: bron-klavier, doel-klavier
- [x] Opslaan in projectconfiguratie
- [x] Bij export naar .organ: koppels worden `[Coupler]` secties

### 3. .organ export vanuit JM-Rec ✅ KLAAR (v3.7)
- [x] Knop **Exporteer .organ** (Instellingen → Exporteren op de master-PC)
- [x] Genereert een volledig .organ definitiebestand met:
  - `[Organ]` — orgelnaam, bouwer, locatie, opname-details
  - `[WindchestGroup]` — per klavier (Name = klaviernaam voor zwelkast-detectie)
  - `[Enclosure]` — voor zwelwerken
  - `[Tremulant]` — voor klavieren met tremulant
  - `[Manual]` — per klavier (pedaal = Manual000)
  - `[Stop]` — per register met pipe-verwijzingen; voetmaat → HarmonicNumber; deelbereiken via FirstAccessiblePipeLogicalKeyNumber
  - `[Coupler]` — gedefinieerde koppels
- [x] Pipe-paden relatief t.o.v. het .organ bestand
- [x] Ondersteunt WAV, FLAC en MP3 (voorkeursvolgorde wav > flac > mp3)
- [x] Ontbrekende samples: pad wordt toch geschreven (stil in JM-Orgue, later opneembaar), teller in resultaat
- [x] Bas/disc-splitmappen en multi-mic-posities worden gevonden (eerste positie gebruikt)

### 4. Naamgeving aanpassen ✅ KLAAR (v3.7)
- [x] README: verwijzingen naar externe software vervangen door JM-Orgue/neutraal
- [x] Badge in README: "output: JM-Orgue"

### 5. Directe JM-Orgue compatibiliteit ✅ KLAAR
- [x] Mapstructuur blijft: `Orgelnaam/Klavier/Register/036-c.wav`
- [x] Tremulant: `Register_trem/` (al ondersteund door JM-Orgue)
- [x] Multi-mic: `Register/Front/`, `Register/Midden/` (al ondersteund door JM-Orgue)
- [x] JM-Orgue kan deze mappen direct scannen via "Scan map" knop

## Workflow (compleet)

1. Gebruiker neemt orgel op met JM-Rec (vaste duur of intelligent/assisterend, v3.6)
2. JM-Rec exporteert mapstructuur + `.organ` bestand (Instellingen → Exporteren)
3. JM-Orgue laadt het `.organ` bestand of scant de map
4. Gebruiker speelt het orgel

## Mogelijk later

- [ ] `_trem`-samples per pijp koppelen in de .organ (vergt parser-uitbreiding in JM-Orgue; nu: LFO-tremulant uit `[Tremulant]` + mapscan vindt `_trem`-mappen)
- [ ] Multi-mic: meerdere posities als aparte ranks in de .organ (nu: eerste positie)
- [ ] Detectiedrempels intelligent opnemen tunen met echte orgelopnames (v3.6)
