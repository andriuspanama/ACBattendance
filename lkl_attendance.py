#!/usr/bin/env python3
"""
LKL lankomumo rinkėjas (pagal ACB botą).

Ką daro:
  1. Suranda rungtynių nuorodas lkl.lt/tvarkarastis (su puslapiais).
  2. Iš kiekvienų rungtynių puslapio ištraukia šeimininkus, svečius, datą ir žiūrovų skaičių.
  3. Saugo data/lkl/matches.csv (be dublikatų, pagal match_id).
  4. Perskaičiuoja namų lankomumo vidurkius -> data/lkl/attendance_averages.json/.csv

Naudojimas:
  pip install requests beautifulsoup4
  python lkl_attendance.py --season 2026-27
  python lkl_attendance.py --recompute-only
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

LEAGUE = "LKL"
BASE = "https://lkl.lt"
SCHEDULE_URL = BASE + "/tvarkarastis"
MATCH_URL = BASE + "/rungtynes/{mid}"

UA = "Mozilla/5.0 (compatible; basketball-finance-attendance-bot/0.1; +lkl)"
MATCH_FIELDS = [
    "league", "season", "match_id", "date", "home", "away",
    "attendance", "source_url", "checked_at", "attempts",
]

# "1584 žiūrovai" / "1 584 žiūrovai"
ATTENDANCE_RE = re.compile(r"(\d[\d\s\u00a0]*)\s*žiūrov", re.I)
MATCH_ID_RE = re.compile(r"/rungtynes/(\d+)")
MAX_PLAUSIBLE = 20000

LT_MONTHS = {
    "sausio": 1, "vasario": 2, "kovo": 3, "balandžio": 4,
    "gegužės": 5, "birželio": 6, "liepos": 7, "rugpjūčio": 8,
    "rugsėjo": 9, "spalio": 10, "lapkričio": 11, "gruodžio": 12,
}


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def parse_lt_date(text: str) -> str | None:
    """'2025 m. Spalio 26 d., 19:10' -> '2025-10-26'"""
    m = re.search(
        r"(\d{4})\s*m\.\s*([A-Za-ząčęėįšųūžĄČĘĖĮŠŲŪŽ]+)\s*(\d{1,2})\s*d",
        text,
        re.I,
    )
    if not m:
        return None
    year, mon_s, day = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    mon = LT_MONTHS.get(mon_s)
    if not mon:
        return None
    return f"{year:04d}-{mon:02d}-{day:02d}"


def parse_attendance(text: str) -> int | None:
    m = ATTENDANCE_RE.search(text)
    if not m:
        return None
    n = int(re.sub(r"\D", "", m.group(1)))
    return n if 0 < n < MAX_PLAUSIBLE else None


def parse_game(html: str) -> dict | None:
    """home, away, date, attendance iš rungtynių HTML."""
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string or "").strip() if soup.title else ""
    # "Neptūnas - Jonavos Hipocredit - LKL.LT"
    title = re.sub(r"\s*-\s*LKL\.LT\s*$", "", title, flags=re.I).strip()
    home = away = None
    if " - " in title:
        home, away = [p.strip() for p in title.split(" - ", 1)]

    text = soup.get_text(" ", strip=True)
    attendance = parse_attendance(html) or parse_attendance(text)
    date = parse_lt_date(html) or parse_lt_date(text)

    if not (home and away):
        return None
    return {
        "home": home,
        "away": away,
        "date": date or "",
        "attendance": attendance,
    }


class Fetcher:
    def __init__(self, delay: float = 1.0, raw_dir: Path | None = None):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.delay = delay
        self.raw_dir = raw_dir
        self._last = 0.0

    def get(self, url: str, tag: str | None = None) -> str | None:
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        try:
            r = self.s.get(url, timeout=30)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"[!] {url}: {e}", file=sys.stderr)
            return None
        if self.raw_dir and tag:
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            (self.raw_dir / f"{tag}.html").write_text(r.text, encoding="utf-8")
        return r.text


def discover_games(fx: Fetcher, max_pages: int = 20, id_lookback: int = 120) -> dict:
    """{match_id: {"url": ...}} iš tvarkaraščio + ID intervalas atgal (praėjusios rungtynės)."""
    games = {}
    for page in range(1, max_pages + 1):
        url = SCHEDULE_URL if page == 1 else f"{SCHEDULE_URL}?page={page}"
        html = fx.get(url, tag=f"schedule_{page}")
        if not html:
            break
        found = MATCH_ID_RE.findall(html)
        if not found:
            break
        for mid in found:
            games.setdefault(mid, {"url": MATCH_URL.format(mid=mid)})
        print(f"  tvarkaraštis p.{page}: +{len(set(found))} (iš viso {len(games)})")

    # Praėjusios rungtynės dažnai nėra pirmame tvarkaraščio puslapyje —
    # užpildome ID tarpą ir lookback nuo minimalaus rastų ID.
    if games:
        ids = [int(m) for m in games]
        lo, hi = min(ids), max(ids)
        start = max(1, lo - id_lookback)
        extra = 0
        for n in range(start, hi + 1):
            mid = str(n)
            if mid not in games:
                games[mid] = {"url": MATCH_URL.format(mid=mid)}
                extra += 1
        print(f"  ID intervalas {start}–{hi}: +{extra} (iš viso {len(games)})")
    return games


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


def save_matches(path: Path, matches: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(matches.values(), key=lambda r: r.get("match_id") or "")
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MATCH_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in MATCH_FIELDS})


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


def update_matches(fx: Fetcher, games: dict, matches: dict, season: str, max_fetch: int):
    # Prioritetas: dar be lankomumo; senesni ID pirma (dažniau jau sužaista)
    todo = []
    for mid, g in games.items():
        prev = matches.get(mid, {})
        if prev.get("attendance"):
            continue
        # jei jau bandyta 3x ir nieko — praleisti (pvz. dar neįvykusios)
        if int(prev.get("attempts") or 0) >= 3 and not prev.get("home"):
            continue
        todo.append((mid, g))
    todo.sort(key=lambda x: int(x[0]))
    todo = todo[:max_fetch]

    done = 0
    for mid, g in todo:
        html = fx.get(g["url"], tag=f"match_{mid}")
        if not html:
            continue
        info = parse_game(html)
        if not info:
            print(f"[!] nepavyko parsinti {g['url']}", file=sys.stderr)
            continue
        prev = matches.get(mid, {})
        matches[mid] = {
            "league": LEAGUE,
            "season": season,
            "match_id": mid,
            "date": info["date"],
            "home": info["home"],
            "away": info["away"],
            "attendance": info["attendance"] if info["attendance"] is not None else "",
            "source_url": g["url"],
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "attempts": int(prev.get("attempts") or 0) + 1,
        }
        done += 1
        att = info["attendance"]
        print(f"  {mid}: {info['home']} – {info['away']}  att={att or '—'}")

    got = sum(1 for m in matches.values() if m.get("attendance"))
    print(f"Patikrinta šįkart: {done}; su lankomumu iš viso: {got}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--season", default="2026-27")
    ap.add_argument("--data-dir", default="data/lkl")
    ap.add_argument("--max-fetch", type=int, default=80)
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--save-html", action="store_true")
    ap.add_argument("--recompute-only", action="store_true")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    matches_path = data_dir / "matches.csv"
    matches = load_matches(matches_path)
    aliases = load_aliases(data_dir / "team_aliases.csv")

    if not args.recompute_only:
        fx = Fetcher(args.delay, data_dir / "raw" if args.save_html else None)
        print("Ieškoma rungtynių tvarkaraštyje…")
        games = discover_games(fx, args.max_pages)
        print(f"Rasta rungtynių: {len(games)}")
        update_matches(fx, games, matches, args.season, args.max_fetch)
        save_matches(matches_path, matches)

    rows, unresolved = compute_averages(matches, aliases, args.season)
    write_outputs(data_dir, rows, unresolved, args.season)
    print(f"Klubų: {len(rows)}")
    for r in rows:
        avg = r["average"] if r["average"] is not None else "—"
        print(f"  {r['club']:<32} {avg:>6}  ({r['home_games_counted']} namų)")


if __name__ == "__main__":
    main()
