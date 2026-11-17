#!/usr/bin/env python3
"""
Sužaistos reguliariojo sezono rungtynės, kurių lankomumo dar nėra – puslapis rodo „su lankomumu / iš viso“.

Skaito kiekvienos lygos data/<aplankas>/matches.csv ir rašo data/<aplankas>/missing_games.json:
  played / with_attendance / missing – lygos sumos,
  clubs  – kiekvieno klubo namų rungtynės (iš viso / su lankomumu),
  games  – rungtynės be lankomumo (id, data, šeimininkai, svečiai, nuoroda).

Taisyklės:
  * tik reguliarusis sezonas (regular_season.playoff_ids – kaip vidurkiuose);
  * rungtynės be žiūrovų (attempts = 99, pvz. BSL „seyircisiz“) – nei „trūksta“, nei „sužaista“;
  * rungtynės laikomos sužaistomis, jei data ankstesnė nei šiandien (UTC); šiandienos dar nežymimos,
    nes lankomumas paskelbiamas po rungtynių;
  * ACB rinkiklis datų nefiksuoja – ten sužaistomis laikomos rungtynės, kurių ID ne didesnis už didžiausią ID
    su lankomumu (didesni ID – dar nesužaistos). Atidėta rungtynė su mažesniu ID gali būti parodyta per anksti.
Rinkikliai šio failo neliečia; atnaujinamas kartu su talpomis (capacity.yml).

Naudojimas:  python missing_games.py --season 2026-27
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from capacities import league_dirs, to_int
from regular_season import playoff_ids

DATE_RE = re.compile(r"20\d\d-\d\d-\d\d$")


def read_matches(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build(league: str, matches: list[dict], season: str, today: str) -> dict:
    rows = [m for m in matches if m.get("season") == season and (m.get("home") or "").strip()]
    not_regular = playoff_ids(league, rows, season)
    rows = [m for m in rows if str(m.get("match_id")) not in not_regular and (m.get("attempts") or "") != "99"]
    ids_with = [int(m["match_id"]) for m in rows if to_int(m.get("attendance")) and str(m.get("match_id")).isdigit()]
    max_id = max(ids_with, default=0)

    clubs: dict[str, dict] = {}
    games = []
    for m in rows:
        att = to_int(m.get("attendance"))
        d = (m.get("date") or "")[:10]
        if att:
            played = True
        elif DATE_RE.match(d) and d > "2000-01-01":
            played = d < today
        else:   # ACB: datos nėra – pagal ID eilę
            mid = str(m.get("match_id") or "")
            played = mid.isdigit() and int(mid) <= max_id
        if not played:
            continue
        c = clubs.setdefault(m["home"].strip(), {"played": 0, "with_attendance": 0})
        c["played"] += 1
        if att:
            c["with_attendance"] += 1
        else:
            games.append({"id": str(m.get("match_id")), "date": d if DATE_RE.match(d) and d > "2000-01-01" else "",
                          "home": m["home"].strip(), "away": (m.get("away") or "").strip(),
                          "url": m.get("source_url") or ""})
    games.sort(key=lambda g: (g["date"] or "9999", g["id"]))
    played_n = sum(c["played"] for c in clubs.values())
    with_n = sum(c["with_attendance"] for c in clubs.values())
    return {"league": league, "season": season, "played": played_n, "with_attendance": with_n,
            "missing": played_n - with_n, "clubs": dict(sorted(clubs.items())), "games": games}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--season", default="2026-27")
    ap.add_argument("--root", default=".")
    args = ap.parse_args()
    today = datetime.now(timezone.utc).date().isoformat()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for d in league_dirs(args.root):
        ddir = Path(args.root) / d
        avg = ddir / "attendance_averages.json"
        if not avg.exists():
            continue
        league = json.loads(avg.read_text(encoding="utf-8")).get("league", "")
        out = build(league, read_matches(ddir / "matches.csv"), args.season, today)
        path = ddir / "missing_games.json"
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            same = {k: v for k, v in old.items() if k != "updated"} == out
        except Exception:  # noqa: BLE001
            same = False
        if not same:   # rašome tik pasikeitus (kad nebūtų tuščių commit'ų)
            path.write_text(json.dumps({**out, "updated": now}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{league:<13} sužaista {out['played']:>3}, su lankomumu {out['with_attendance']:>3}, trūksta {out['missing']:>3}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
