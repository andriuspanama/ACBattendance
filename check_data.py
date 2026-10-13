#!/usr/bin/env python3
"""Patikrina visus data/**/matches.csv: trūkstamą lankomumą, dublikatus, keistas reikšmes.

Naudojimas:  python check_data.py            (iš repo šaknies)
             python check_data.py data/bbl   (tik viena lyga)
Nieko nekeičia, tik spausdina ataskaitą.
"""
import csv
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

MIN_OK, MAX_OK = 200, 30000


def load(path: Path):
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def mid(r):
    return r.get("match_id") or r.get("game_id") or "?"


def check(path: Path) -> int:
    rows = load(path)
    problems = 0
    league = path.parent.name if path.parent.name != "data" else "acb"
    print(f"\n=== {league.upper()}  ({path}, {len(rows)} eilučių) ===")
    today = date.today().isoformat()

    # 1. Trūksta lankomumo
    missing = [r for r in rows if not (r.get("attendance") or "").strip()]
    past = [r for r in missing if (r.get("date") or "0000-00-00") <= today]
    if past:
        problems += len(past)
        print(f"\nTrūksta lankomumo ({len(past)}):")
        for r in sorted(past, key=lambda r: r.get("date") or ""):
            d = r.get("date") or "?"
            if d == "0000-00-00":
                d = "data nežinoma"
            print(f"  {d:<14} {r.get('home','?')} – {r.get('away','?')}   (ID {mid(r)})")
    else:
        print("\nTrūkstamo lankomumo nėra.")

    # 2. Dublikatai
    seen = Counter((r.get("date"), r.get("home"), r.get("away")) for r in rows
                   if r.get("home") and r.get("date") not in (None, "", "0000-00-00"))
    dups = [k for k, n in seen.items() if n > 1]
    if dups:
        problems += len(dups)
        print(f"\nDublikatai ({len(dups)}):")
        for d, h, a in dups:
            print(f"  {d} {h} – {a}")

    # 3. Keistos reikšmės
    odd = []
    for r in rows:
        a = (r.get("attendance") or "").strip()
        if a.isdigit() and not (MIN_OK <= int(a) <= MAX_OK):
            odd.append(r)
    if odd:
        problems += len(odd)
        print(f"\nKeisti skaičiai ({len(odd)}):")
        for r in odd:
            print(f"  {r.get('date')} {r.get('home')} – {r.get('away')}: {r.get('attendance')}")

    # 4. Namų rungtynių skaičius pagal klubą
    per = defaultdict(lambda: [0, 0])  # [su lankomumu, be]
    for r in rows:
        h = (r.get("home") or "").strip()
        if not h:
            continue
        per[h][0 if (r.get("attendance") or "").strip() else 1] += 1
    print("\nNamų rungtynės pagal klubą (su lankomumu / be):")
    for club, (ok, no) in sorted(per.items(), key=lambda x: x[0].lower()):
        flag = "   <-- trūksta" if no else ""
        print(f"  {club:<36} {ok} / {no}{flag}")
    return problems


def main():
    args = sys.argv[1:]
    if args:
        files = [Path(a) / "matches.csv" for a in args]
    else:
        files = sorted(Path("data").rglob("matches.csv"))
    files = [f for f in files if f.exists()]
    if not files:
        print("matches.csv failų nerasta"); return 1
    total = sum(check(f) for f in files)
    print(f"\n--- PABAIGA --- rasta problemų: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
