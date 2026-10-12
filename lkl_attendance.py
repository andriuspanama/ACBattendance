#!/usr/bin/env python3
"""
LKL lankomumo rinkėjas (Flashscore versija).
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

import requests
from bs4 import BeautifulSoup

LEAGUE = "LKL"
FLASHSCORE_FIXTURES = "https://www.flashscore.com/basketball/lithuania/lkl/fixtures/"
FLASHSCORE_RESULTS = "https://www.flashscore.com/basketball/lithuania/lkl/results/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

MATCH_FIELDS = [
    "league", "season", "match_id", "date", "home", "away",
    "attendance", "source_url", "checked_at", "attempts",
]

ATTENDANCE_RE = re.compile(r"Attendance:\s*([\d\s\u00a0]+)", re.I)
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
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    
    attendance = parse_attendance(text)
    
    title = (soup.title.string or "").strip() if soup.title else ""
    home = away = None
    
    clean_title = re.sub(r"(rezultatai.*|results.*|Basketball.*|– Flashscore.*|LIVE.*)", "", title, flags=re.I).strip()
    if " - " in clean_title:
        parts = clean_title.split(" - ")
    elif " v " in clean_title:
        parts = clean_title.split(" v ")
    elif " vs " in clean_title:
        parts = clean_title.split(" vs ")
    else:
        parts = []

    if len(parts) >= 2:
        home = parts[0].strip()
        away = parts[1].strip()

    date = parse_lt_date(text)

    if not (home and away):
        return None

    rename = {
        "Jonavos Hipocredit": "Hipocredit",
        "Jonava Hipocredit": "Hipocredit",
        "BC Juventus": "Juventus",
        "Kėdainių Juventus": "Juventus",
        "Kedainiu Juventus": "Juventus",
        "BC Rytas": "Rytas",
        "Žalgiris Kaunas": "Žalgiris",
        "Zalgiris Kaunas": "Žalgiris",
    }
    home = rename.get(home, home)
    away = rename.get(away, away)

    return {
        "home": home,
        "away": away,
        "date": date or "",
        "attendance": attendance,
    }


class Fetcher:
    def __init__(self, delay: float = 1.0, raw_dir: Path | None = None):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "lt-LT,lt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.flashscore.com/",
        })
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


def discover_flashscore_games(fx: Fetcher) -> dict:
    games = {}
    for url in [FLASHSCORE_FIXTURES, FLASHSCORE_RESULTS]:
        html = fx.get(url, tag=f"flashscore_{url.split('/')[-2]}")
        if not html:
            continue
        match_paths = re.findall(r'href="(/match/basketball/[^"]+)"', html)
        for path in match_paths:
            full_url = "https://www.flashscore.com" + path
            mid_match = re.search(r"mid=([a-zA-Z0-9]+)", path)
            mid = mid_match.group(1) if mid_match else path.strip("/").split("/")[-1]
            games[mid] = {"url": full_url}
        
    print(f"Rasta rungtynių iš Flashscore (iš viso): {len(games)}")
    return games


def load_matches(path: Path) -> dict:
    matches = {}
    if not path.exists():
        return matches
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            mid = (r.get("match_id") or "").strip()
            if mid:
                # Ignoruojame senas lkl.lt nuorodas
                url = r.get("source_url", "")
                if "lkl.lt" not in url:
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


def update_matches(fx: Fetcher, games: dict, matches: dict, season: str):
    done = 0
    for mid, g in games.items():
        prev = matches.get(mid, {})
        if prev.get("attendance"):
            continue
        
        url = g["url"]
        html = fx.get(url, tag=f"match_{mid}")
        if not html:
            continue
        
        info = parse_game(html)
        if not info:
            continue

        matches[mid] = {
            "league": LEAGUE,
            "season": season,
            "match_id": mid,
            "date": info["date"] or prev.get("date", ""),
            "home": info["home"],
            "away": info["away"],
            "attendance": info["attendance"] if info["attendance"] is not None else "",
            "source_url": url,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "attempts": int(prev.get("attempts") or 0) + 1,
        }
        done += 1
        print(f"  {mid}: {info['home']} – {info['away']}  att={info['attendance'] or '—'}")

    got = sum(1 for m in matches.values() if m.get("attendance") and m.get("season") == season)
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
        print("Ieškoma rungtynių per Flashscore...")
        games = discover_flashscore_games(fx)
        
        # Prijungiame jau turėtas Flashscore nuorodas iš matches.csv, kad jos nepasimestų
        for mid, m in matches.items():
            if mid not in games and m.get("source_url"):
                games[mid] = {"url": m["source_url"]}

        update_matches(fx, games, matches, args.season)
        save_matches(matches_path, matches)

    rows, unresolved = compute_averages(matches, aliases, args.season)
    write_outputs(data_dir, rows, unresolved, args.season)
    print(f"Klubų: {len(rows)}")
    for r in rows:
        avg = r["average"] if r["average"] is not None else "—"
        print(f"  {r['club']:<32} {avg:>6}  ({r['home_games_counted']} namų)")


if __name__ == "__main__":
    main()
