#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Maak een proefbestand waarin de loop echt rondgaat, zodat je hem kunt horen.

Een looppunt beoordelen op een getal is prima voor een machine, maar je wilt
het horen. Dit gereedschap speelt een sample na zoals een sampler dat doet:
aanspraak, dan de loop een aantal keer rond, dan de uitklank. Zit er een tik in
de naad, dan hoor je die hier elke paar seconden terugkomen.

Zonder `--loop` worden de looppunten uit de smpl-chunk van het bestand gelezen;
anders worden ze ter plekke gezocht.

    python tools/loop_beluisteren.py sample.wav proef.wav --rondjes 8

Copyright (c) 2026 Martijn van der Kolk (orgelmaker) - alle rechten
voorbehouden. Zie LICENSE.
"""
import argparse
import os
import sys

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import loop_zoeker as L


def bouw(data, sr, start, eind, rondjes):
    """Aanspraak + loop x rondjes + uitklank aan elkaar."""
    kop = data[:start]
    lus = data[start:eind]
    staart = data[eind:]
    return np.concatenate([kop] + [lus] * max(1, rondjes) + [staart], axis=0)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("bron")
    p.add_argument("doel")
    p.add_argument("--rondjes", type=int, default=8)
    p.add_argument("--voet", default="8", help="voetmaat, als er gezocht moet worden")
    p.add_argument("--stemtoon", type=float, default=440.0)
    p.add_argument("--hold", type=float, default=None, help="aanhoudtijd van de opname")
    p.add_argument("--zoek", action="store_true",
                   help="looppunten zelf zoeken in plaats van uit de smpl-chunk lezen")
    p.add_argument("--schoon", type=float, default=0.06,
                   help="tot deze fout niet overvloeien (zelfde drempel als bij het schrijven)")
    a = p.parse_args()

    data, sr = sf.read(a.bron, dtype="float64", always_2d=True)
    punten = None if a.zoek else L.lees_smpl(a.bron)
    if punten:
        start, eind = punten[0], punten[1]
        bron = "smpl-chunk"
    else:
        mono = data.mean(axis=1)
        noot = int(os.path.basename(a.bron)[:3]) if os.path.basename(a.bron)[:3].isdigit() else 60
        factor = L.voet_factor(a.voet)
        f0 = (L.gemeten_grondtoon(mono, sr, L.grondtoon(noot, factor, a.stemtoon))
              if factor else None)
        start, eind, fout = L.zoek_loop(mono, sr, f0, hold=a.hold)
        if start is None:
            sys.exit("geen bruikbare loop gevonden in %s" % a.bron)
        bron = "zelf gezocht (fout %.3f)" % fout
        # Net als bij het echte wegschrijven: is de naad niet schoon, dan wordt
        # er overgevloeid. Anders klinkt dit proefbestand slechter dan wat er
        # straks in de sampleset komt, en beoordeel je het verkeerde.
        if fout > a.schoon:
            n = int(sr * 0.12)
            if f0:
                n = max(n, int(8 * sr / f0))
            n = L.maak_crossfade(data, start, eind, n)
            bron += ", overgevloeid %d ms" % (n * 1000 // sr)

    uit = bouw(data, sr, start, eind, a.rondjes)
    sf.write(a.doel, uit, sr, subtype=sf.info(a.bron).subtype)
    print("%s -> %s" % (os.path.basename(a.bron), a.doel))
    print("  looppunten uit %s: %.3f - %.3f s (%.0f ms), %d rondjes"
          % (bron, start / sr, eind / sr, (eind - start) * 1000.0 / sr, a.rondjes))
    print("  duur %.1f s (was %.1f s)" % (len(uit) / sr, len(data) / sr))


if __name__ == "__main__":
    main()
