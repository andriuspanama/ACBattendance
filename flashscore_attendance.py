#!/usr/bin/env python3
"""
Bendras namų rungtynių lankomumo rinkiklis iš Flashscore – bet kuriai lygai.
(Prancūzija, Italija ir Turkija čia neįtrauktos – Flashscore jų lankomumo nerodo.)

Veikia taip pat kaip lkl_attendance.py / bbl_attendance.py:
  1. Iš lygos „results“ puslapio surenka sezono rungtynių nuorodas.
  2. Kiekvienoms rungtynėms atidaro puslapį (Playwright) ir ištraukia
     Attendance, datą ir komandas.
  3. Saugo data/<aplankas>/matches.csv
  4. Perskaičiuoja namų lankomumo vidurkius -> attendance_averages.json/.csv
     (tokiu pat formatu, kokį jau skaito svetainė).

Nauja lyga pridedama viena eilute LEAGUES sąraše žemiau.

Naudojimas:
  pip install requests beautifulsoup4 playwright
  playwright install chromium
  python flashscore_attendance.py --league fra --season 2026-27
  python flashscore_attendance.py --league fra --recompute-only
  python flashscore_attendance.py --list
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    sync_playwright = None  # type: ignore

BASE = "https://www.flashscore.com"

# raktas: (lygos pavadinimas JSON faile, Flashscore kelias, duomenų aplankas)
LEAGUES = {
    # Lenkija: Flashscore duoda lankomumą, o arenas ima official_attendance.py iš plk.pl
    "pol":  ("PLK",              "basketball/poland/basket-liga",             "data/pol/flashscore"),
    # Graikija: Flashscore duoda lankomumą (ne visoms rungtynėms), arenas ima official_attendance.py iš esake.gr
    "gre":  ("GBL",              "basketball/greece/basket-league",           "data/gre/flashscore"),
}

MATCH_FIELDS = [
    "league", "season", "match_id", "date", "home", "away",
    "attendance", "source_url", "checked_at", "attempts",
]

# Jei rungtynėse lankomumo nėra, bandome dar kelis kartus (Flashscore kartais
# jį įrašo vėliau), o vėliau nebetikriname, kad nebūtų tikrinama be galo.
MAX_ATTEMPTS = 4
RECHECK_DAYS = 3


def season_start(season: str) -> str:
    """2026-27 -> 2026-08-01. Atmeta praėjusio sezono rungtynes."""
    m = re.match(r"(\d{4})", season or "")
    y = int(m.group(1)) if m else datetime.now(timezone.utc).year
    return f"{y:04d}-08-01"


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


def save_matches(path: Path, matches: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MATCH_FIELDS)
        w.writeheader()
        for mid in sorted(matches.keys()):
            w.writerow({k: matches[mid].get(k, "") for k in MATCH_FIELDS})


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


def parse_attendance(text: str) -> int | None:
    m = re.search(r"Attendance[:\s]*([\d\s,.]+)", text, re.I)
    if not m:
        return None
    digits = re.sub(r"[^\d]", "", m.group(1))
    if not digits:
        return None
    n = int(digits)
    return n if 50 < n < 30000 else None


def parse_date(text: str) -> str:
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return ""


def extract_teams_from_title(title: str) -> tuple[str, str]:
    """
    Flashscore title formatai:
    - "Rytas v Neptunas 10/10/2026 | Basketball - Flashscore"
    - "RYT 86-77 NEP | Rytas v Neptunas 10/10/2026 | Basketball - Flashscore | Match"
    """
    m = re.search(r"\|\s*([^|]+?)\s+v\s+([^|]+?)(?:\s+\d|/|\||$)", title, re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.search(r"^(.+?)\s+v\s+(.+?)(?:\s+\d|/|\||$)", title, re.I)
    if m:
        home, away = m.group(1).strip(), m.group(2).strip()
        if not re.search(r"\d", home) and not re.search(r"\d", away):
            return home, away
    return "", ""


def clean_team_name(name: str) -> str:
    """Išvalo Flashscore scoreboard šiukšles iš komandos pavadinimo."""
    if not name:
        return name
    name = name.strip()
    if "|" in name:
        name = name.split("|")[-1].strip()
    name = re.sub(r"^[A-Z]{2,4}\s+\d{1,3}[-–]\d{1,3}\s+[A-Z]{2,4}\s*", "", name)
    name = re.sub(r"^[A-Z]{2,4}\s+", "", name)
    return name.strip()


def compute_averages(matches: dict, aliases: dict, season: str, league: str):
    start = season_start(season)
    unresolved: set[str] = set()
    per_club: dict[str, list[int]] = defaultdict(list)
    all_homes: set[str] = set()

    for m in matches.values():
        if m.get("season") != season:
            continue
        match_date = (m.get("date") or "").strip()
        if match_date and match_date < start:
            continue
        home_raw = (m.get("home") or "").strip()
        if not home_raw:
            continue
        club = resolve_club(clean_team_name(home_raw), aliases, unresolved)
        all_homes.add(club)
        att = m.get("attendance")
        if att not in (None, ""):
            try:
                per_club[club].append(int(att))
            except (TypeError, ValueError):
                pass

    rows = []
    for club in sorted(all_homes):
        vals = per_club.get(club, [])
        if vals:
            rows.append({
                "league": league, "season": season, "club": club,
                "home_games_counted": len(vals),
                "average": round(sum(vals) / len(vals)),
                "total": sum(vals), "max": max(vals), "min": min(vals),
            })
        else:
            rows.append({
                "league": league, "season": season, "club": club,
                "home_games_counted": 0,
                "average": None, "total": 0, "max": None, "min": None,
            })

    rows.sort(key=lambda r: (-(r["average"] if r["average"] is not None else -1), r["club"]))
    return rows, unresolved


def write_outputs(data_dir: Path, rows, unresolved, season: str, league: str, stats: dict):
    data_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (data_dir / "attendance_averages.json").write_text(
        json.dumps(
            {"league": league, "season": season, "updated": now,
             "games": stats, "clubs": rows},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    fields = ["league", "season", "club", "home_games_counted", "average", "total", "max", "min"]
    with (data_dir / "attendance_averages.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r[k]) for k in fields})
    (data_dir / "unresolved_teams.txt").write_text(
        ("\n".join(sorted(unresolved)) + "\n") if unresolved else "", encoding="utf-8"
    )


def match_id_of(url: str) -> str:
    m = re.search(r"[?&]mid=([A-Za-z0-9]+)", url)
    return m.group(1) if m else url.rstrip("/").split("/")[-1]


def needs_fetch(existing: dict | None) -> bool:
    """Ar dar verta atidaryti rungtynių puslapį."""
    if not existing:
        return True
    if existing.get("attendance"):
        return False
    attempts = int(existing.get("attempts") or 0)
    if attempts < MAX_ATTEMPTS:
        return True
    d = (existing.get("date") or "").strip()
    if not d:
        return False
    try:
        age = datetime.now(timezone.utc).date() - datetime.strptime(d, "%Y-%m-%d").date()
    except ValueError:
        return False
    return age <= timedelta(days=RECHECK_DAYS)


def accept_cookies(page):
    for sel in [
        "#onetrust-accept-btn-handler",
        "button:has-text('I Accept')",
        "button:has-text('Accept All')",
        "button:has-text('Accept')",
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=700):
                btn.click()
                page.wait_for_timeout(600)
                return
        except Exception:
            pass


def collect_match_urls(page, max_pages: int) -> list[str]:
    urls: set[str] = set()
    for _ in range(max_pages):
        links = page.eval_on_selector_all(
            "a[href*='/match/basketball/']",
            "els => els.map(e => e.href).filter(h => h && h.includes('/match/basketball/'))",
        )
        for href in links:
            if re.search(r"/match/basketball/[^/]+/[^/]+", href):
                urls.add(href.split("#")[0])
        try:
            btn = page.locator("text=Show more matches").first
            if btn.is_visible(timeout=1200):
                btn.click()
                page.wait_for_timeout(1600)
            else:
                break
        except Exception:
            break
    return sorted(urls)


def scrape_match(page, url: str, save_html: Path | None = None) -> dict | None:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(1400)
        accept_cookies(page)
        text = page.inner_text("body")
        title = page.title()
        if save_html:
            save_html.mkdir(parents=True, exist_ok=True)
            (save_html / f"{match_id_of(url)}.html").write_text(page.content(), encoding="utf-8")

        att = parse_attendance(text)
        date = parse_date(text)
        home, away = extract_teams_from_title(title)
        if not home or not away or re.search(r"\d", home) or re.search(r"\d", away):
            try:
                names = page.eval_on_selector_all(
                    ".duelParticipant__home .participant__participantName, "
                    ".duelParticipant__away .participant__participantName",
                    "els => els.map(e => e.textContent.trim()).filter(Boolean)",
                )
                if len(names) >= 2:
                    home, away = names[0], names[1]
            except Exception:
                pass

        mid = match_id_of(url)
        has_mid = bool(re.search(r"[?&]mid=", url))
        return {
            "match_id": mid,
            "date": date,
            "home": clean_team_name(home),
            "away": clean_team_name(away),
            "attendance": att,
            "source_url": url.split("?")[0] + (f"?mid={mid}" if has_mid else ""),
        }
    except PlaywrightTimeout:
        return None
    except Exception as e:
        print(f"  Klaida {url}: {e}", file=sys.stderr)
        return None


def run_league(key: str, args) -> int:
    league, path, data_dir = LEAGUES[key]
    data_dir = Path(data_dir)
    results_url = f"{BASE}/{path}/results/"
    start = season_start(args.season)
    matches_path = data_dir / "matches.csv"
    matches = load_matches(matches_path)
    aliases = load_aliases(data_dir / "team_aliases.csv")
    found = 0
    blocked = ""

    if not args.recompute_only:
        if sync_playwright is None:
            print("Reikia playwright: pip install playwright && playwright install chromium", file=sys.stderr)
            return 1
        print(f"=== {league} ({key}) · sezonas {args.season} (nuo {start}) · esama rungtynių: {len(matches)}")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-US",
            )
            page = context.new_page()
            page.goto(results_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            accept_cookies(page)

            urls = collect_match_urls(page, max_pages=args.max_pages)
            print(f"Rasta rungtynių nuorodų: {len(urls)}")
            to_fetch = [u for u in urls if needs_fetch(matches.get(match_id_of(u)))][: args.max_fetch]
            print(f"Bus tikrinama: {len(to_fetch)}")
            if not urls:
                blocked = "Flashscore rezultatų puslapyje nerasta nė vienų rungtynių (blokuoja arba pasikeitė puslapis)"
            failed = 0

            raw_dir = (data_dir / "raw") if args.save_html else None
            for i, url in enumerate(to_fetch, 1):
                info = scrape_match(page, url, raw_dir)
                time.sleep(args.delay)
                if not info:
                    print(f"[{i}/{len(to_fetch)}] nepavyko: {url}")
                    failed += 1
                    continue
                d = info["date"] or ""
                mid = info["match_id"]
                if d and d < start:
                    # Įsimename, kad kitą kartą nebeatidarytume (į vidurkius neįskaičiuojama)
                    matches[mid] = {"league": league, "season": "praeitas", "match_id": mid, "date": d,
                                    "home": info["home"], "away": info["away"], "attendance": "",
                                    "source_url": info["source_url"],
                                    "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                    "attempts": str(MAX_ATTEMPTS)}
                    print(f"[{i}/{len(to_fetch)}] {info['home']} – {info['away']}: praleista (praėjęs sezonas, {d})")
                    continue
                prev = matches.get(mid, {})
                matches[mid] = {
                    "league": league,
                    "season": args.season,
                    "match_id": mid,
                    "date": d or prev.get("date", ""),
                    "home": info["home"] or prev.get("home", ""),
                    "away": info["away"] or prev.get("away", ""),
                    "attendance": info["attendance"] if info["attendance"] is not None else prev.get("attendance", ""),
                    "source_url": info["source_url"],
                    "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "attempts": str(int(prev.get("attempts") or 0) + 1),
                }
                if info["attendance"] is not None:
                    found += 1
                print(f"[{i}/{len(to_fetch)}] {d} {info['home']} – {info['away']}: "
                      f"{info['attendance'] if info['attendance'] is not None else 'nėra lankomumo'}")
            browser.close()
            if to_fetch and len(to_fetch) >= 3 and failed == len(to_fetch):
                blocked = f"Flashscore neatidarė nė vieno iš {failed} rungtynių puslapių (blokuoja?)"

    for m in matches.values():
        m["home"] = clean_team_name(m.get("home", ""))
        m["away"] = clean_team_name(m.get("away", ""))
    save_matches(matches_path, matches)

    season_games = [m for m in matches.values() if m.get("season") == args.season
                    and not ((m.get("date") or "") and m["date"] < start)]
    stats = {"checked": len(season_games),
             "with_attendance": sum(1 for m in season_games if m.get("attendance"))}
    rows, unresolved = compute_averages(matches, aliases, args.season, league)
    write_outputs(data_dir, rows, unresolved, args.season, league, stats)

    print(f"--- {league}: naujų su lankomumu {found}; sezono rungtynių {stats['checked']}, "
          f"iš jų su lankomumu {stats['with_attendance']}; klubų {len(rows)}")
    for r in rows:
        avg = r["average"] if r["average"] is not None else "—"
        print(f"  {r['club']:<34} {avg:>6}  ({r['home_games_counted']} namų)")
    if blocked:
        print(f"!!! {league}: {blocked}", file=sys.stderr)
        if args.fail_if_empty:
            return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--league", help="lygos raktas (pvz. fra) arba 'all'")
    ap.add_argument("--list", action="store_true", help="parodyti visas lygas")
    ap.add_argument("--season", default="2026-27")
    ap.add_argument("--max-fetch", type=int, default=80, help="max rungtynių puslapių per paleidimą")
    ap.add_argument("--max-pages", type=int, default=15, help="kiek kartų spausti „Show more matches“")
    ap.add_argument("--recompute-only", action="store_true")
    ap.add_argument("--save-html", action="store_true", help="išsaugoti rungtynių HTML derinimui")
    ap.add_argument("--delay", type=float, default=0.8)
    ap.add_argument("--fail-if-empty", action="store_true",
                    help="baigti klaida, jei Flashscore negrąžino rungtynių (kad GitHub pažymėtų nesėkmę)")
    args = ap.parse_args()

    if args.list or not args.league:
        for k, (lg, path, dd) in LEAGUES.items():
            print(f"{k:<5} {lg:<12} {BASE}/{path}/results/  -> {dd}")
        return 0
    keys = list(LEAGUES) if args.league == "all" else [args.league]
    bad = [k for k in keys if k not in LEAGUES]
    if bad:
        print(f"Nežinoma lyga: {', '.join(bad)}. Galimos: {', '.join(LEAGUES)}", file=sys.stderr)
        return 2
    rc = 0
    for k in keys:
        rc = max(rc, run_league(k, args))
    return rc


if __name__ == "__main__":
    sys.exit(main())
