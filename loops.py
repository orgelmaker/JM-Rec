#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Looppunten zoeken in orgelsamples (rekenkern van JM-Rec).

Een orgeltoon moet onbeperkt aangehouden kunnen worden, en daarvoor heeft de
sampler een stuk uit het midden nodig dat naadloos op zichzelf aansluit. Deze
module zoekt dat stuk en schrijft het als `smpl`-chunk in de WAV, waar JM-Orgue
het leest (loader.rs, read_wav_markers).

Wat het zoeken kansrijk maakt, is dat JM-Rec twee dingen precies weet:

  - **Waar het stabiele deel ligt.** JM-Rec speelde de noot zelf. De aanspraak
    is er bij het opnemen al afgeknipt en de uitklank begint bij de note-off,
    dus met `midi_hold_seconds` ligt het bruikbare midden vast. Het uit de
    omhullende raden gaat bij hoge pijpen mis: daar is de aanspraak luider dan
    de toon zelf.
  - **Welke toon het is.** Uit de bestandsnaam komt het MIDI-nummer en uit het
    project de voetmaat, dus de verwachte grondtoon ligt vast; die wordt in het
    signaal nagemeten, want een orgel staat zelden op precies 440 Hz.

Beoordeeld wordt op twee dingen tegelijk. De **golfvorm** vlak na het einde moet
aansluiten op die vlak na het begin (genormaliseerde fout; 0 is perfect). En de
**omhullende** moet over anderhalve seconde doorlopen - dat vangt de trage
beweging die een korte golfvormvergelijking niet ziet, zoals de zweving van een
mixtuur met zes koren. Zonder dat tweede criterium knipt de loop middenin een
zwevingscyclus en klinkt het register onrustig.

Sluit de golfvorm niet schoon aan, dan wordt de audio zelf naadloos gemaakt:
over de laatste honderdtwintig milliseconde vóór het einde gaat hij lineair over
in wat er vóór het begin staat. Blijft ook dat te ver uit elkaar liggen, dan
komt er GEEN loop - een hoorbare tik elke seconde is erger dan een noot die
netjes uitklinkt.

Copyright (c) 2026 Martijn van der Kolk (orgelmaker) - alle rechten
voorbehouden. Zie LICENSE.
"""
import io
import json
import os
import re
import shutil
import struct
import sys

import numpy as np

try:
    import soundfile as sf
    HAS_SF = True
except ImportError:          # pragma: no cover - alleen zonder soundfile
    HAS_SF = False


# ── toonhoogte ──

def voet_factor(voet):
    """Hoeveel maal de geschreven toon klinkt dit register?

    8 voet = zoals geschreven, 16 voet een octaaf lager, 4 voet een octaaf
    hoger, "2 2/3" een twaalfde hoger. Mixturen en cornetten ("4st", "III St.")
    hebben geen enkele grondtoon; die geven None.
    """
    v = str(voet or "").strip().lower().replace("'", "").replace("’", "")
    if not v or re.search(r"\d\s*st\b|\bst\b|sterk|rangs|rang|[ivx]+\s*st", v):
        return None
    try:
        delen = v.split()
        waarde = float(delen[0].replace(",", "."))
        if len(delen) > 1 and "/" in delen[1]:
            teller, noemer = delen[1].split("/")
            waarde += float(teller) / float(noemer)
        return 8.0 / waarde if waarde > 0 else None
    except (ValueError, IndexError, ZeroDivisionError):
        return None


def grondtoon(midi_noot, factor, stemtoon=440.0):
    return stemtoon * 2.0 ** ((midi_noot - 69) / 12.0) * factor


def gemeten_grondtoon(x, sr, ruw):
    """Grondtoon uit het signaal halen, met `ruw` als richtwaarde.

    Nodig omdat een orgel zelden exact op 440 Hz staat (Westzaan 443, Dordrecht
    441) en omdat de laagste pijpen een zwakke grondtoon hebben. We zoeken de
    piek in het spectrum die het dichtst bij de verwachte waarde ligt en
    verfijnen die met de buren (parabolisch).
    """
    k = x[: min(len(x), int(sr * 1.5))]
    if k.size < 2048:
        return ruw
    sp = np.abs(np.fft.rfft(k * np.hanning(k.size)))
    fr = np.fft.rfftfreq(k.size, 1.0 / sr)
    band = (fr > ruw * 0.8) & (fr < ruw * 1.25)
    if not band.any() or sp[band].max() <= 0:
        return ruw
    idx = np.where(band)[0][int(np.argmax(sp[band]))]
    if 0 < idx < sp.size - 1 and sp[idx] > 0:
        a, b, c = sp[idx - 1], sp[idx], sp[idx + 1]
        noemer = a - 2 * b + c
        delta = 0.5 * (a - c) / noemer if noemer else 0.0
        return float((idx + np.clip(delta, -0.5, 0.5)) * sr / k.size)
    return float(fr[idx])


# ── stabiel deel ──

def omhullende(x, sr, blok_ms=10):
    blok = max(1, int(sr * blok_ms / 1000.0))
    n = (len(x) // blok) * blok
    if n == 0:
        return np.array([]), blok
    return np.abs(x[:n]).reshape(-1, blok).max(axis=1), blok


def stabiel_deel(x, sr, hold=None):
    """(begin, einde) van het aangehouden middenstuk, in samples.

    Weet je hoe lang de toets ingedrukt was (JM-Rec weet dat: `midi_hold_seconds`),
    geef die dan mee - dat is veel betrouwbaarder dan het uit de omhullende
    afleiden. Bij hoge pijpen is de aanspraak namelijk luider dan de toon zelf
    en schommelt de omhullende in de zaal flink, waardoor een drempel op de piek
    de note-off veel te vroeg ziet.

    Zonder die hint wordt het aanhoudniveau geschat als de mediaan van het
    middenstuk, en geldt als note-off de eerste blijvende daling daaronder.
    """
    env, blok = omhullende(x, sr)
    if env.size < 20:
        return 0, len(x)
    piek = float(env.max())
    if piek <= 0:
        return 0, len(x)
    boven = env > piek * 0.4
    eerste = int(np.argmax(boven)) if boven.any() else 0
    begin = (eerste + int(0.15 * sr / blok)) * blok

    if hold and hold > 0.3:
        # De aanspraak is er bij het opnemen al afgeknipt, dus het ingedrukte
        # deel loopt vanaf (vrijwel) het begin van het bestand. Een marge van
        # 150 ms houdt de note-off zelf buiten de loop.
        einde = int(min(len(x), (hold - 0.15) * sr))
        return int(min(begin, max(0, einde - int(0.3 * sr)))), einde

    midden = env[int(env.size * 0.15):int(env.size * 0.6)]
    houdniveau = float(np.median(midden)) if midden.size else piek
    if houdniveau <= 0:
        houdniveau = piek
    laatste = env.size
    for i in range(eerste + 20, env.size):
        if env[i] < houdniveau * 0.5 and (env[i:i + 25] < houdniveau * 0.6).all():
            laatste = i
            break
    einde = max(begin, laatste * blok)
    return int(begin), int(min(einde, len(x)))


# ── loop zoeken ──

def nsd(a, b):
    """Genormaliseerd verschil tussen twee even lange stukken: 0 = gelijk."""
    va = float(np.dot(a, a))
    vb = float(np.dot(b, b))
    if va <= 0 or vb <= 0:
        return 1.0
    verschil = a - b
    return float(np.dot(verschil, verschil) / np.sqrt(va * vb))


def nuldoorgang(x, i, zoek=400):
    """Dichtstbijzijnde stijgende nuldoorgang bij i."""
    lo = max(1, i - zoek)
    hi = min(len(x) - 1, i + zoek)
    kandidaten = [j for j in range(lo, hi) if x[j - 1] <= 0 < x[j]]
    if not kandidaten:
        return i
    return min(kandidaten, key=lambda j: abs(j - i))


def sprong_db(x, start, eind):
    """Hoe groot is het niveauverschil op de naad, in dB onder het signaal?

    Dit is wat je hoort als tik: het verschil tussen de laatste sample vóór het
    loop-einde en de eerste sample van het loop-begin, afgezet tegen het
    plaatselijke niveau. Hoe lager (negatiever), hoe onhoorbaarder.
    """
    rms = float(np.sqrt(np.mean(x[start:eind] ** 2))) if eind > start else 0.0
    if rms <= 0:
        return 0.0
    stap = abs(float(x[eind - 1]) - float(x[start]))
    return 20.0 * np.log10(max(stap, 1e-12) / rms)


def zoek_loop(x, sr, f0, min_sec=0.4, max_sec=2.0, venster_perioden=12,
              starts=6, hold=None):
    """Zoek het beste looppunt in het stabiele deel van een mono-signaal.

    Er wordt niet één beginpunt geprobeerd maar een handvol, verspreid over het
    begin van het aangehouden deel. Dat scheelt: een toon die net op één plek
    onrustig is (een zwevende boventoon, een vleugje tremulant in de zaal) geeft
    een paar centimeter verderop vaak wél een schone loop.

    Geeft (start, einde, fout) terug, of (None, None, 1.0) als er niets
    bruikbaars in het bereik zit. `einde` is exclusief.
    """
    begin, einde_stabiel = stabiel_deel(x, sr, hold)
    bruikbaar = einde_stabiel - begin
    if f0 and f0 > 0:
        periode = sr / f0
    else:
        periode = sr / 60.0        # mixturen: reken met een lage 'zwevingstoon'
    venster = int(max(periode * venster_perioden, sr * 0.08))
    min_len = int(max(min_sec * sr, periode * 4))
    if bruikbaar < min_len + venster + periode:
        return None, None, 1.0

    # Let op: de kosten zijn niet begrensd op 1 (de golfvormfout mag erboven
    # uitkomen en de omhullende telt er nog bij op), dus hier geen 1.0 als
    # beginwaarde nemen - dan vallen juist de lastige samples buiten de boot.
    beste = (np.inf, None, None)
    speling = max(0, bruikbaar - min_len - venster - int(periode))
    for k in range(max(1, starts)):
        offset = int(periode) + int(speling * k / max(1, starts - 1)) if starts > 1 else int(periode)
        start = nuldoorgang(x, begin + offset)
        doel = x[start:start + venster]
        if doel.size < venster or not np.any(doel):
            continue
        max_len = int(min(max_sec * sr, einde_stabiel - start - venster))
        if max_len <= min_len:
            continue
        fouten = _fouten_reeks(x, start, venster, min_len, max_len)
        if fouten is None:
            continue
        # De golfvorm alleen is niet genoeg. Een mixtuur heeft meerdere pijpen
        # per toets die onderling een paar cent verschillen, en dat geeft een
        # zweving van ongeveer een hertz. Een loop kan over honderd milliseconde
        # perfect aansluiten en toch middenin zo'n zweving afknippen - dat hoor
        # je niet als tik maar als onrust, elke omwenteling opnieuw. Daarom telt
        # ook mee of de omhullende over anderhalve seconde doorloopt.
        env_fouten = _omhullende_fouten(x, sr, start, min_len, max_len)
        if env_fouten is None:
            totaal = fouten
        else:
            # Tot 0,10 telt de golfvormfout gewoon mee; daarboven lost de
            # overvloeiing de naad grotendeels op en mag de trage beweging de
            # doorslag geven. Maar niet onbeperkt: bij een fout van ruim 0,5
            # zijn de twee stukken wezenlijk verschillend en wordt de
            # overvloeiing een hoorbare overgang. Vandaar de knik in plaats van
            # een plafond. Hoe sterker de klank traag beweegt, hoe zwaarder de
            # omhullende weegt - bij een vlakke toon is die beweging zaalruis.
            if f0:
                # Enkelvoudig register: de golfvorm is maatgevend en de
                # omhullende hooguit een schifting tussen gelijkwaardige
                # kandidaten. Hier zat nooit het probleem.
                gewicht = 0.15 * min(1.0, _modulatie(x, sr, begin, einde_stabiel) / 0.12)
            else:
                # Mixtuur of cornet: meerdere pijpen per toets, dus geen enkele
                # periode en wél een duidelijke zweving. Daar is de doorlopende
                # omhullende belangrijker dan de golfvorm - die wordt toch
                # overgevloeid.
                gewicht = 0.8
            golf = np.where(fouten <= 0.10, fouten, 0.10 + 2.0 * (fouten - 0.10))
            totaal = golf + gewicht * env_fouten
        # Een langere loop herhaalt minder hoorbaar; bij vrijwel gelijke
        # kwaliteit wint hij daarom van een korte.
        lengtes = np.arange(totaal.size) + min_len
        gewogen = totaal * (1.0 - 0.06 * np.minimum(1.0, lengtes / float(sr)))
        i = int(np.argmin(gewogen))
        if gewogen[i] < beste[0]:
            beste = (gewogen[i], start, start + min_len + i)

    if beste[1] is None:
        return None, None, 1.0
    start, eind = beste[1], beste[2]
    return start, eind, nsd(x[eind:eind + venster], x[start:start + venster])


def _modulatie(x, sr, begin, einde):
    """Hoe sterk beweegt de klank traag (0,5-25 Hz), als deel van het niveau.

    Een enkele pijp ligt rond 0,03-0,05; een mixtuur met zes koren die onderling
    een paar cent verschillen komt op 0,08-0,15, en een 16-voets pedaalpijp in
    de zaal zelfs hoger.
    """
    k = x[begin:einde]
    blok = max(1, sr // 200)
    if k.size < blok * 20:
        return 0.0
    env = np.abs(k[:k.size // blok * blok]).reshape(-1, blok).mean(axis=1)
    if env.mean() <= 0:
        return 0.0
    sp = np.fft.rfft(env - env.mean())
    fr = np.fft.rfftfreq(env.size, blok / float(sr))
    sp[(fr < 0.5) | (fr > 25.0)] = 0
    return float(np.std(np.fft.irfft(sp, env.size)) / env.mean())


def _omhullende_fouten(x, sr, start, min_len, max_len, venster_s=1.5):
    """Hoe goed loopt de omhullende door bij elk eindpunt?

    Hiermee wordt de trage beweging in de klank beoordeeld - de zweving van een
    mixtuur, het ademen van de wind - die een korte golfvormvergelijking niet
    ziet. De omhullende wordt op 200 Hz bemonsterd; het antwoord staat op het
    grove raster en wordt daarna uitgesmeerd over de samples.
    """
    blok = max(1, sr // 200)
    env = np.abs(x[:len(x) // blok * blok]).reshape(-1, blok).mean(axis=1)
    w = max(4, int(venster_s * sr / blok))
    s0 = start // blok
    lo, hi = (start + min_len) // blok, (start + max_len) // blok + 1
    if s0 + w > env.size or hi + w > env.size or hi <= lo:
        return None
    doel = env[s0:s0 + w]
    doel = doel - doel.mean()
    e_doel = float(np.dot(doel, doel))
    if e_doel <= 0:
        return None
    fouten = np.empty(hi - lo)
    for i in range(hi - lo):
        v = env[lo + i:lo + i + w]
        v = v - v.mean()
        ev = float(np.dot(v, v))
        if ev <= 0:
            fouten[i] = 1.0
            continue
        d = v - doel
        fouten[i] = float(np.dot(d, d)) / np.sqrt(ev * e_doel)
    grof = np.repeat(fouten, blok)
    nodig = max_len - min_len
    if grof.size < nodig:
        grof = np.concatenate([grof, np.full(nodig - grof.size, grof[-1])])
    return grof[:nodig]


def _fouten_reeks(x, start, venster, min_len, max_len):
    """De naadfout voor élk mogelijk eindpunt tussen min_len en max_len.

    Eerst stapte deze zoeker met hele perioden, in de veronderstelling dat een
    loop van precies N golven vanzelf in fase sluit. Dat klopt alleen als de
    gemeten grondtoon exact is: zit die er een halve promille naast, dan loopt
    de fase over een seconde al een kwart golf uit en is elk stappunt mis. Op
    een Subbas van 33 Hz scheelde dat het verschil tussen fout 0,61 en 0,05.

    Daarom wordt nu élke sample bekeken. Dat kan snel: de som van de
    kwadratische verschillen is sum(a^2) - 2*sum(a*b) + sum(b^2), en de
    middelste term is een kruiscorrelatie die in één FFT over het hele bereik
    tegelijk gaat.
    """
    lo = start + min_len
    hi = start + max_len
    if hi + venster > len(x) or hi <= lo:
        return None
    doel = x[start:start + venster]
    e_doel = float(np.dot(doel, doel))
    if e_doel <= 0:
        return None

    gebied = x[lo:hi + venster]
    n = gebied.size
    fft_len = 1 << int(np.ceil(np.log2(n + venster)))
    corr = np.fft.irfft(np.fft.rfft(gebied, fft_len) *
                        np.conj(np.fft.rfft(doel, fft_len)), fft_len)[:hi - lo]

    # Energie van elk venster, via een lopende som.
    kwad = np.concatenate(([0.0], np.cumsum(gebied ** 2)))
    e_venster = kwad[venster:venster + (hi - lo)] - kwad[:hi - lo]

    noemer = np.sqrt(np.maximum(e_venster, 1e-30) * e_doel)
    return (e_venster - 2.0 * corr + e_doel) / noemer


# ── overvloeien ──

def maak_crossfade(data, start, eind, lengte):
    """Vloei het stuk vóór het loop-einde over in het stuk vóór het loop-begin.

    Niet elke orgeltoon is precies periodiek: wind, zaal en de pijp zelf laten
    de golfvorm langzaam veranderen, en dan bestaat er geen punt waar het
    einde naadloos op het begin aansluit (gemeten: de beste naad van zo'n noot
    blijft steken rond fout 0,35, ook bij een uitputtende zoektocht).

    De oplossing die sampleset-bouwers al decennia gebruiken: maak het naadloos
    door de audio zelf te mengen. Over de laatste `lengte` samples vóór het
    einde wordt geleidelijk overgegaan op wat er vóór het begin staat, zodat de
    sample op het sprongpunt exact aansluit op wat daar hoort te volgen. De
    menging is gelijke-energie (sin/cos), anders zakt het volume halverwege in.

    Werkt in-place op een (n, kanalen)-array en geeft de gebruikte lengte terug.
    """
    lengte = int(min(lengte, start, eind - start - 1))
    if lengte < 8:
        return 0
    k = np.arange(1, lengte + 1)                 # k=1 vlak vóór het sprongpunt
    t = (k - 1) / float(lengte - 1)              # 0 bij k=1, 1 bij k=lengte
    # Lineair mengen, niet gelijke-energie. Beide stukken zijn dezelfde toon op
    # een ander moment en dus sterk gecorreleerd; dan houdt een lineaire menging
    # het niveau vlak, terwijl een sin/cos-menging er middenin een bult van
    # ongeveer een dB in legt - en die bult keert elke loopomwenteling terug,
    # wat als een trilling hoorbaar wordt.
    a = t                                        # aandeel van het origineel
    b = 1.0 - t                                  # aandeel van vóór het begin
    for ch in range(data.shape[1]):
        eind_stuk = data[eind - lengte:eind, ch][::-1]      # k = 1..lengte
        begin_stuk = data[start - lengte:start, ch][::-1]
        gemengd = a * eind_stuk + b * begin_stuk
        data[eind - lengte:eind, ch] = gemengd[::-1]
    return lengte


# ── smpl-chunk ──

def smpl_chunk(sr, unity_note, loop_start, loop_eind_inclusief, cents=0.0):
    """Een `smpl`-chunk met één loop, zoals samplers hem verwachten."""
    fractie = int(max(0.0, min(99.999, cents)) / 100.0 * 0x100000000) & 0xFFFFFFFF
    kop = struct.pack("<9I",
                      0,                                   # manufacturer
                      0,                                   # product
                      int(round(1e9 / sr)),                # sample period (ns)
                      int(unity_note),                     # dwMIDIUnityNote
                      fractie,                             # dwMIDIPitchFraction
                      0, 0,                                # SMPTE
                      1,                                   # aantal loops
                      0)                                   # sampler data
    lus = struct.pack("<6I", 0, 0, int(loop_start), int(loop_eind_inclusief), 0, 0)
    body = kop + lus
    return b"smpl" + struct.pack("<I", len(body)) + body


def schrijf_smpl(pad, sr, unity_note, loop_start, loop_eind_exclusief, cents=0.0):
    """Zet (of vervang) de smpl-chunk in een WAV en werk de RIFF-lengte bij."""
    with io.open(pad, "rb") as f:
        data = f.read()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("geen WAV-bestand: %s" % pad)

    # Bestaande smpl-chunk eruit (anders staan er straks twee).
    uit = bytearray(data[:12])
    i = 12
    while i + 8 <= len(data):
        naam = data[i:i + 4]
        lengte = struct.unpack("<I", data[i + 4:i + 8])[0]
        blok = data[i:i + 8 + lengte + (lengte & 1)]
        if naam != b"smpl":
            uit += blok
        i += 8 + lengte + (lengte & 1)

    uit += smpl_chunk(sr, unity_note, loop_start, loop_eind_exclusief - 1, cents)
    uit[4:8] = struct.pack("<I", len(uit) - 8)
    with io.open(pad, "wb") as f:
        f.write(bytes(uit))


def lees_smpl(pad):
    """(loop_start, loop_eind_exclusief, unity_note) of None."""
    with io.open(pad, "rb") as f:
        data = f.read()
    i = 12
    while i + 8 <= len(data):
        naam = data[i:i + 4]
        lengte = struct.unpack("<I", data[i + 4:i + 8])[0]
        if naam == b"smpl" and lengte >= 36 + 24:
            body = data[i + 8:i + 8 + lengte]
            unity = struct.unpack("<I", body[12:16])[0]
            s, e = struct.unpack("<2I", body[36 + 8:36 + 16])
            return int(s), int(e) + 1, int(unity)
        i += 8 + lengte + (lengte & 1)
    return None


# ── per bestand ──

JM_REC_COPYRIGHT = "JM-Rec (github.com/orgelmaker/JM-Rec)"


def _schrijf_audio(pad, data, sr, subtype):
    """Audio terugschrijven met de copyright-tags erin, zoals JM-Rec zelf doet."""
    with sf.SoundFile(pad, "w", samplerate=sr, channels=data.shape[1],
                      subtype=subtype) as f:
        try:
            f.copyright = JM_REC_COPYRIGHT
            f.software = "JM-Rec loop_zoeker"
        except Exception:
            pass
        f.write(data)


def verwerk(pad, voet, stemtoon=440.0, drempel=0.50, schrijven=False,
            min_sec=0.4, max_sec=2.0, fade_vanaf=0.06, fade_ms=120.0,
            hold=None, kopie_pad=None):
    """Zoek een loop in één sample. Geeft een dict met de uitkomst.

    Is de naad schoner dan `fade_vanaf`, dan blijft de audio onaangeroerd en
    gaan alleen de looppunten in de smpl-chunk. Zit er meer verschil in, dan
    wordt er een crossfade van `fade_ms` in gemengd zodat de loop toch sluit.
    """
    if not HAS_SF:
        raise RuntimeError("soundfile ontbreekt")
    info = sf.info(pad)
    data, sr = sf.read(pad, dtype="float64", always_2d=True)
    mono = data.mean(axis=1)
    naam = os.path.basename(pad)
    m = re.match(r"^(\d{3})-", naam)
    noot = int(m.group(1)) if m else None
    factor = voet_factor(voet)
    f0 = None
    if noot is not None and factor:
        f0 = gemeten_grondtoon(mono, sr, grondtoon(noot, factor, stemtoon))

    start, eind, fout = zoek_loop(mono, sr, f0, min_sec, max_sec, hold=hold)
    uit = {"bestand": naam, "noot": noot, "f0": f0, "fout": fout,
           "start": start, "eind": eind, "sr": sr, "frames": len(mono),
           "lengte_s": (eind - start) / sr if start is not None else 0.0,
           "sprong": sprong_db(mono, start, eind) if start is not None else 0.0,
           "fade": 0, "goed": False, "geschreven": False}
    if start is None:
        return uit

    # Een overvloeiing moet een flink aantal golven omspannen, anders hoor je
    # hem als een hikje. Bij een Subbas van 33 Hz is 120 ms nog geen vier
    # golven; daar moet hij dus langer.
    fade_n = int(sr * fade_ms / 1000.0)
    if f0 and f0 > 0:
        fade_n = max(fade_n, int(8 * sr / f0))
    schoon = fout <= fade_vanaf
    kan_faden = min(fade_n, start, eind - start - 1) >= 8
    uit["goed"] = schoon or (fout <= drempel and kan_faden)
    uit["fade_nodig"] = not schoon
    if not (schrijven and uit["goed"]):
        return uit

    if not schoon:
        # De audio zelf gaat veranderen. Eerst het origineel opzijzetten:
        # een smpl-chunk kun je er zo weer afhalen, een overvloeiing niet.
        if kopie_pad:
            os.makedirs(os.path.dirname(kopie_pad), exist_ok=True)
            if not os.path.exists(kopie_pad):
                shutil.copy2(pad, kopie_pad)
        uit["fade"] = maak_crossfade(data, start, eind, fade_n)
        _schrijf_audio(pad, data, sr, info.subtype)
        # Na het overvloeien is `fout` niet meer de maat: die vergelijkt de
        # audio ná het loop-einde, en dat stuk wordt nooit meer gespeeld. Wat
        # telt is of er op het sprongpunt nog een stap zit - dat is de tik die
        # je zou horen - en die is nu per definitie weg.
        mono = data.mean(axis=1)
        uit["sprong"] = sprong_db(mono, start, eind)

    unity = noot if noot else 60
    cents = 0.0
    if f0 and noot and factor:
        zuiver = grondtoon(noot, factor, stemtoon)
        cents = 1200.0 * np.log2(f0 / zuiver) if zuiver > 0 else 0.0
        cents = float(np.clip(cents, 0.0, 99.0))
    schrijf_smpl(pad, sr, unity, start, eind, cents)
    uit["geschreven"] = True
    return uit
