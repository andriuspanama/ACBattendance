#!/usr/bin/env python3
"""
LKL lankomumo skaičiuotuvas (CSV pagrindu).
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import unicodedata
import re

LEAGUE = "LKL"

MATCH_FIELDS = [
    "league", "season", "match_id", "date", "home", "away",
    "attendance", "source_url", "checked_at", "attempts",
]


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def load_matches(path: Path) -> dict:
    matches = {}
    if not path.exists():
        return matches
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            mid = (r.get("match_id") or "").strip()
            if mid:
                matches[mid] = r
    return matches


def load_aliases(path: Path) -> dict:
    aliases = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("alias") and r.get("club_name"):
                    aliases[norm(r["alias"])] = r["club_name"].strip()
    return aliases


def resolve_club(raw: str, aliases: dict, unresolved: set) -> str:
    key = norm(raw)
    if key in aliases:
        return aliases[key]
    unresolved.add(raw)
    return raw


def compute_averages(matches: dict, aliases: dict, season: str):
    unresolved = set()
    per_club = defaultdict(list)
    all_homes = set()
    
    for m in matches.values():
        if m.get("season") != season:
            continue
        home_raw = (m.get("home") or "").strip()
        if not home_raw:
            continue
        club = resolve_club(home_raw, aliases, unresolved)
        all_homes.add(club)
        att = m.get("attendance")
        if att not in (None, ""):
            try:
                per_club[club].append(int(att))
            except (TypeError, ValueError):
                pass
                
    rows = []
    for club in all_homes:
        vals = per_club.get(club, [])
        if vals:
            rows.append({
                "league": LEAGUE, "season": season, "club": club,
                "home_games_counted": len(vals),
                "average": round(sum(vals) / len(vals)),
                "total": sum(vals), "max": max(vals), "min": min(vals),
            })
        else:
            rows.append({
                "league": LEAGUE, "season": season, "club": club,
                "home_games_counted": 0,
                "average": None, "total": 0, "max": None, "min": None,
            })
            
    rows.sort(key=lambda r: (-(r["average"] if r["average"] is not None else -1), r["club"]))
    return rows, unresolved


def write_outputs(data_dir: Path, rows, unresolved, season: str):
    data_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    
    (data_dir / "attendance_averages.json").write_text(
        json.dumps(
            {"league": LEAGUE, "season": season, "updated": now, "clubs": rows},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    
    if rows:
        fields = list(rows[0].keys())
        with (data_dir / "attendance_averages.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow({k: ("" if r.get(k) is None else r[k]) for k in fields})
                
    if unresolved:
        (data_dir / "unresolved_teams.txt").write_text(
            "\n".join(sorted(unresolved)) + "\n", encoding="utf-8"
        )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", default="2026-27")
    ap.add_argument("--data-dir", default="data/lkl")
    ap.add_argument("--recompute-only", action="store_true")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    matches_path = data_dir / "matches.csv"
    matches = load_matches(matches_path)
    aliases = load_aliases(data_dir / "team_aliases.csv")

    print(f"Įkraunama rungtynių iš CSV: {len(matches)}")
    rows, unresolved = compute_averages(matches, aliases, args.season)
    write_outputs(data_dir, rows, unresolved, args.season)
    
    print(f"Klubų vidurkiai sėkmingai atnaujinti. Klubų: {len(rows)}")
    for r in rows:
        avg = r["average"] if r["average"] is not None else "—"
        print(f"  {r['club']:<32} {avg:>6}  ({r['home_games_counted']} namų rungtynės)")


if __name__ == "__main__":
    main()
