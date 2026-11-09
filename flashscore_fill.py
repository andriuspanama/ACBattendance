#!/usr/bin/env python3
"""
Flashscore atsarginis lankomumas oficialių lygų svetainių duomenims.

Oficialios svetainės (NBL, Korisliiga, LEBL, ABA, LBA, Winner League) kartais lankomumo nepublikuoja.
flashscore_attendance.py surenka Flashscore lankomumą į data/<lyga>/flashscore/matches.csv, o šis skriptas
iš jo užpildo TIK tas rungtynes, kurių oficialiame faile (data/<lyga>/matches.csv) skaičiaus nėra.
Oficialus skaičius niekada nekeičiamas.

Rungtynės suporuojamos tik kai visos sąlygos tinka vienu metu:
  * data skiriasi ne daugiau kaip 1 diena;
  * šeimininkai ir svečiai (ta pačia tvarka) vienareikšmiškai atitinka oficialų klubą;
  * rasta lygiai viena tokia Flashscore rungtynių eilutė.
Klubo vardas atitinka, kai: (1) yra data/<lyga>/team_aliases.csv (stulpeliai alias,club_name – alias yra
Flashscore pavadinimas, club_name – oficialus), arba (2) pavadinimai sutampa, arba (3) vieno visi žodžiai yra kito
viduje ir toks oficialus klubas lygoje tik vienas. Kitu atveju neatspėjama – pavadinimas išvedamas į žurnalą,
kad jį būtų galima įrašyti į team_aliases.csv.

Naudojimas:
  python flashscore_fill.py --league fin --season 2026-27 --data-dir data/fin
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

from official_attendance import (Ctx, NAMES, DIRS, compute_averages, write_outputs, save_matches,
                                 fs_full_name, _fs_words, to_int, today)
from flashscore_attendance import load_matches as load_fs_matches


def load_alias_file(path: Path) -> dict:
    """Flashscore pavadinimas (žodžiai) -> oficialus klubo vardas."""
    out = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                a, c = (r.get("alias") or "").strip(), (r.get("club_name") or "").strip()
                if a and c:
                    out[" ".join(_fs_words(a))] = c
    return out


def candidates(fs_name: str, clubs: list[str], alias: dict) -> list[str]:
    key = " ".join(_fs_words(fs_name))
    if not key:
        return []
    if key in alias:
        return [alias[key]] if alias[key] in clubs else []
    exact = [c for c in clubs if " ".join(_fs_words(c)) == key]
    if exact:
        return exact
    fw = set(key.split())
    out = []
    for c in clubs:
        cw = set(_fs_words(c))
        if cw and (fw <= cw or cw <= fw):
            out.append(c)
    return out


def day_diff(a: str, b: str) -> int | None:
    try:
        return abs((datetime.strptime(a, "%Y-%m-%d") - datetime.strptime(b, "%Y-%m-%d")).days)
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--league", required=True, help="aus, fin, lvee, aba, ita arba isr")
    ap.add_argument("--season", default="2026-27")
    ap.add_argument("--data-dir", default=None)
    args = ap.parse_args()
    key = args.league
    if key not in NAMES:
        print(f"Nežinoma lyga: {key}", file=sys.stderr)
        return 2
    data_dir = Path(args.data_dir or DIRS[key])
    ctx = Ctx(NAMES[key], str(data_dir), args.season)
    fs = [r for r in load_fs_matches(data_dir / "flashscore" / "matches.csv").values()
          if r.get("season") == args.season and to_int(r.get("attendance"))]
    alias = load_alias_file(data_dir / "team_aliases.csv")
    clubs = sorted({(m.get("home") or "").strip() for m in ctx.matches.values()}
                   | {(m.get("away") or "").strip() for m in ctx.matches.values()} - {""})
    clubs = [c for c in clubs if c]
    for a, c in alias.items():
        if c not in clubs:
            print(f"  ! team_aliases.csv: „{c}“ nėra tarp oficialių klubų", file=sys.stderr)

    missing = [(mid, m) for mid, m in ctx.matches.items()
               if m.get("season") == args.season and not to_int(m.get("attendance"))
               and (m.get("date") or "") and m["date"] <= today()]
    print(f"=== {NAMES[key]}: oficialiame faile sužaistų rungtynių be lankomumo {len(missing)}; "
          f"Flashscore su lankomumu {len(fs)}")

    filled, unmatched, ambiguous = 0, set(), 0
    for mid, m in missing:
        near = [r for r in fs if (lambda d: d is not None and d <= 1)(day_diff(r.get("date", ""), m["date"]))]
        hits, partial = [], []
        for r in near:
            url = r.get("source_url", "")
            ch = candidates(fs_full_name(r.get("home", ""), url), clubs, alias)
            ca = candidates(fs_full_name(r.get("away", ""), url), clubs, alias)
            if ch == [m["home"]] and ca == [m["away"]]:
                hits.append(r)
            elif ch == [m["home"]] or ca == [m["away"]]:
                partial.append((r, ch, ca))
        if len(hits) == 1:
            r = hits[0]
            m["attendance"] = str(to_int(r["attendance"]))
            m["source_url"] = r.get("source_url") or m.get("source_url", "")
            filled += 1
            print(f"  + {m['date']} {m['home']} – {m['away']}: {m['attendance']} (Flashscore)")
        elif len(hits) > 1:
            ambiguous += 1
            print(f"  ? {m['date']} {m['home']} – {m['away']}: Flashscore rado {len(hits)} tinkamų eilučių – praleista")
        else:
            for r, ch, ca in partial:
                url = r.get("source_url", "")
                for side, got in (("home", ch), ("away", ca)):
                    if len(got) != 1:
                        unmatched.add(fs_full_name(r.get(side, ""), url))
    if unmatched:
        print("Neatpažinti Flashscore klubai (jei tai oficialaus klubo kitas pavadinimas – įrašykite į "
              f"{data_dir}/team_aliases.csv): " + "; ".join(sorted(unmatched)))
    print(f"--- užpildyta iš Flashscore: {filled}; dviprasmiškų: {ambiguous}")

    if filled:
        save_matches(ctx.path, ctx.matches)
        rows, unresolved = compute_averages(ctx.matches, ctx.season, ctx.league)
        write_outputs(ctx.dir, rows, unresolved, ctx.season, ctx.league)
    return 0


if __name__ == "__main__":
    sys.exit(main())
