#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Looppunten zoeken in een hele sampleset, vanaf de opdrachtregel.

De rekenkern staat in loops.py naast jm_rec.py; dit is de schil eromheen die
een heel project aflooopt. Zo blijft er één implementatie, die de app en dit
gereedschap allebei gebruiken.

    python tools/loop_zoeker.py <project>.jm-rec.json --kort
    python tools/loop_zoeker.py <project>.jm-rec.json --schrijf
        --reservekopie <map voor de originelen>

Copyright (c) 2026 Martijn van der Kolk (orgelmaker) - alle rechten
voorbehouden. Zie LICENSE.
"""
import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from loops import (grondtoon, gemeten_grondtoon, lees_smpl, maak_crossfade,  # noqa: F401
                   nsd, nuldoorgang, schrijf_smpl, sprong_db, stabiel_deel,
                   verwerk, voet_factor, zoek_loop)

def registers_uit_manifest(manifest):
    """{(klavier, map): voetmaat} uit een JM-Rec-projectbestand."""
    man = json.load(io.open(manifest, encoding="utf-8"))
    uit = {}
    for kb in man.get("keyboards", []):
        for r in kb.get("registers", []):
            uit[(kb["name"], r["name"])] = r.get("foot", "")
            if kb.get("tremulant"):
                uit[(kb["name"], r["name"] + "_trem")] = r.get("foot", "")
    for r in man.get("pedal_registers", []):
        uit[("Pedaal", r["name"])] = r.get("foot", "")
    return uit, man


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("manifest", help="pad naar <project>.jm-rec.json")
    p.add_argument("--register", action="append",
                   help="alleen dit register (Klavier/Map), mag meermaals")
    p.add_argument("--noten", type=int, default=0,
                   help="hoogstens zoveel noten per register (0 = alle)")
    p.add_argument("--schrijf", action="store_true",
                   help="smpl-chunk echt wegschrijven (anders alleen meten)")
    p.add_argument("--drempel", type=float, default=0.50,
                   help="slechtste naad die met overvloeien nog mag (0,50)")
    p.add_argument("--schoon", type=float, default=0.06,
                   help="tot deze fout blijft de audio onaangeroerd (0,06)")
    p.add_argument("--stemtoon", type=float, default=440.0)
    p.add_argument("--kort", action="store_true",
                   help="alleen een regel per register")
    p.add_argument("--reservekopie",
                   help="map waarin originelen worden bewaard van samples waarvan\nde audio wordt aangepast (alleen bij --schrijf)")
    a = p.parse_args()

    kaart, man = registers_uit_manifest(a.manifest)
    hold = float((man.get('settings') or {}).get('midi_hold_seconds') or 0) or None
    basis = os.path.dirname(os.path.abspath(a.manifest))
    gewenst = set(a.register or [])

    totaal = goed = 0
    print("%-34s %5s %8s %7s %7s %8s %s"
          % ("sample", "noot", "f0 (Hz)", "lengte", "fout", "naad", ""))
    for (kb, reg), voet in sorted(kaart.items()):
        if gewenst and ("%s/%s" % (kb, reg)) not in gewenst:
            continue
        map_ = os.path.join(basis, kb, reg)
        if not os.path.isdir(map_):
            continue
        # Ook de submappen: een gedeeld register staat in _bas/_dis, en bij
        # meerdere microfoons krijgt elke positie een eigen map.
        bestanden = []
        for wortel, _mappen, namen in os.walk(map_):
            for f in sorted(namen):
                if f.lower().endswith(".wav"):
                    bestanden.append(os.path.relpath(os.path.join(wortel, f), map_))
        bestanden.sort()
        if a.noten:
            bestanden = bestanden[::max(1, len(bestanden) // a.noten)][:a.noten]
        if not a.kort:
            print("\n== %s / %s  (%s voet) ==" % (kb, reg, voet or "-"))
        r_goed = r_schoon = r_fade = 0
        slecht = []
        for fn in bestanden:
            kopie = (os.path.join(a.reservekopie, kb, reg, fn)
                     if a.reservekopie else None)
            r = verwerk(os.path.join(map_, fn), voet, a.stemtoon, a.drempel,
                        a.schrijf, fade_vanaf=a.schoon, hold=hold,
                        kopie_pad=kopie)
            totaal += 1
            goed += 1 if r["goed"] else 0
            r_goed += 1 if r["goed"] else 0
            if r["goed"] and not r.get("fade_nodig"):
                r_schoon += 1
            elif r["goed"]:
                r_fade += 1
            else:
                slecht.append(fn.split("-")[0])
            if a.kort:
                continue
            if not r["goed"]:
                stand = "GEEN LOOP"
            elif r.get("fade"):
                stand = "ok, overgevloeid %d ms" % (r["fade"] * 1000 // r["sr"])
            elif r.get("fade_nodig"):
                stand = "ok, overvloeien nodig"
            else:
                stand = "ok, schone naad"
            print("%-34s %5s %8s %6.2fs %7.3f %6.0f dB  %s" % (
                fn, r["noot"],
                "%.1f" % r["f0"] if r["f0"] else "-",
                r["lengte_s"], r["fout"], r["sprong"], stand))
        if a.kort and bestanden:
            print("%-14s %-18s %-7s %3d noten  %3d schoon  %3d overvloeien  %s"
                  % (kb, reg, voet or "-", len(bestanden), r_schoon, r_fade,
                     ("geen loop: " + ", ".join(slecht)) if slecht else ""))
    if totaal:
        print("\n%d van %d samples kregen een bruikbare loop (%.0f%%)"
              % (goed, totaal, 100.0 * goed / totaal))


if __name__ == "__main__":
    main()
