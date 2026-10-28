#!/usr/bin/env python3
"""
BBL (easyCredit Basketball Bundesliga) namų rungtynių lankomumas iš Flashscore.

Ką daro:
  1. Surenka sezono rungtynių nuorodas iš Flashscore BBL results puslapio.
  2. Kiekvienoms rungtynėms atidaro puslapį (Playwright) ir ištraukia
     Attendance, datą, komandas.
  3. Saugo data/bbl/matches.csv
  4. Perskaičiuoja namų lankomumo vidurkius -> attendance_averages.json/.csv

Naudojimas:
  pip install requests beautifulsoup4 playwright
  playwright install chromium
  python bbl_attendance.py --season 2026-27 --max-fetch 80
  python bbl_attendance.py --recompute-only
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
from regular_season import playoff_ids

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    sync_playwright = None  # type: ignore

LEAGUE = "BBL"
BASE = "https://www.flashscore.com"
RESULTS_URL = "https://www.flashscore.com/basketball/germany/bbl/results/"
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


def save_matches(path: Path, matches: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MATCH_FIELDS)
        w.writeheader()
        for mid in sorted(matches.keys()):
            row = {k: matches[mid].get(k, "") for k in MATCH_FIELDS}
            w.writerow(row)


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
    n = int(re.sub(r"[^\d]", "", m.group(1)))
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
    - "Frankfurt v Hamburg 11/10/2026 | Basketball - Flashscore"
    - "FRA 89-78 HAM | Frankfurt v Hamburg 11/10/2026 | Basketball - Flashscore | Match"
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


def compute_averages(matches: dict, aliases: dict, season: str):
    unresolved: set[str] = set()
    per_club: dict[str, list[int]] = defaultdict(list)
    all_homes: set[str] = set()

    not_regular = playoff_ids(LEAGUE, matches, season)   # tik reguliarusis sezonas
    for m in matches.values():
        if m.get("season") != season:
            continue
        if str(m.get("match_id")) in not_regular:
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
    else:
        (data_dir / "unresolved_teams.txt").write_text("", encoding="utf-8")


def collect_match_urls(page, max_pages: int = 15) -> list[str]:
    """Surenka unikalias rungtynių nuorodas (su mid parametru)."""
    urls: set[str] = set()

    for _ in range(max_pages):
        links = page.eval_on_selector_all(
            "a[href*='/match/basketball/']",
            """els => els.map(e => e.href).filter(h => h && h.includes('/match/basketball/'))"""
        )
        for href in links:
            if re.search(r"/match/basketball/[^/]+/[^/]+", href):
                clean = href.split("#")[0]
                urls.add(clean)

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


def scrape_match(page, url: str) -> dict | None:
    """Atidaro rungtynių puslapį ir ištraukia lankomumą + komandas + datą."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(1400)

        # Cookie banner
        try:
            for sel in [
                "button:has-text('I Accept')",
                "button:has-text('Accept All')",
                "button:has-text('Sutinku')",
                "#onetrust-accept-btn-handler",
            ]:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=700):
                    btn.click()
                    page.wait_for_timeout(600)
                    break
        except Exception:
            pass

        text = page.inner_text("body")
        title = page.title()

        att = parse_attendance(text)
        date = parse_date(text)
        home, away = extract_teams_from_title(title)

        # Fallback – imame tikrus komandų pavadinimus iš puslapio
        if not home or not away or re.search(r"\d", home) or re.search(r"\d", away):
            try:
                names = page.eval_on_selector_all(
                    ".duelParticipant__home .participant__participantName, "
                    ".duelParticipant__away .participant__participantName",
                    "els => els.map(e => e.textContent.trim()).filter(Boolean)"
                )
                if len(names) >= 2:
                    home, away = names[0], names[1]
            except Exception:
                pass

        home = clean_team_name(home)
        away = clean_team_name(away)

        mid_match = re.search(r"[?&]mid=([A-Za-z0-9]+)", url)
        match_id = mid_match.group(1) if mid_match else url.rstrip("/").split("/")[-1]

        return {
            "match_id": match_id,
            "date": date,
            "home": home,
            "away": away,
            "attendance": att,
            "source_url": url.split("?")[0] + (f"?mid={match_id}" if mid_match else ""),
        }
    except PlaywrightTimeout:
        return None
    except Exception as e:
        print(f"  Klaida {url}: {e}", file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", default="2026-27")
    ap.add_argument("--data-dir", default="data/bbl")
    ap.add_argument("--max-fetch", type=int, default=80, help="max naujų rungtynių per paleidimą")
    ap.add_argument("--max-pages", type=int, default=15, help="kiek 'Show more' paspausti")
    ap.add_argument("--recompute-only", action="store_true")
    ap.add_argument("--save-html", action="store_true", help="išsaugoti raw HTML derinimui")
    ap.add_argument("--delay", type=float, default=0.8)
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    matches_path = data_dir / "matches.csv"
    matches = load_matches(matches_path)
    aliases = load_aliases(data_dir / "team_aliases.csv")

    if args.recompute_only:
        print(f"Įkraunama rungtynių iš CSV: {len(matches)}")
        rows, unresolved = compute_averages(matches, aliases, args.season)
        write_outputs(data_dir, rows, unresolved, args.season)
        print(f"Klubų vidurkiai atnaujinti. Klubų: {len(rows)}")
        for r in rows:
            avg = r["average"] if r["average"] is not None else "—"
            print(f"  {r['club']:<40} {avg:>6}  ({r['home_games_counted']} namų)")
        return 0

    if sync_playwright is None:
        print("Reikia playwright: pip install playwright && playwright install chromium", file=sys.stderr)
        return 1

    print(f"Sezonas {args.season}. Esama rungtynių: {len(matches)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="en-US",
        )
        page = context.new_page()

        print("Renku rungtynių nuorodas iš results...")
        page.goto(RESULTS_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

        try:
            page.locator("button:has-text('I Accept')").first.click(timeout=2000)
            page.wait_for_timeout(800)
        except Exception:
            pass

        urls = collect_match_urls(page, max_pages=args.max_pages)
        print(f"Rasta unikalių match URL: {len(urls)}")

        to_fetch = []
        for url in urls:
            mid_match = re.search(r"[?&]mid=([A-Za-z0-9]+)", url)
            mid = mid_match.group(1) if mid_match else url.rstrip("/").split("/")[-1]
            existing = matches.get(mid)
            if existing and existing.get("attendance"):
                continue
            to_fetch.append(url)

        to_fetch = to_fetch[: args.max_fetch]
        print(f"Bus tikrinama: {len(to_fetch)}")

        found = 0
        for i, url in enumerate(to_fetch, 1):
            print(f"[{i}/{len(to_fetch)}] {url}")
            info = scrape_match(page, url)
            time.sleep(args.delay)
            if not info:
                continue
            mid = info["match_id"]
            prev = matches.get(mid, {})
            attempts = int(prev.get("attempts") or 0) + 1
            matches[mid] = {
                "league": LEAGUE,
                "season": args.season,
                "match_id": mid,
                "date": info["date"] or prev.get("date", ""),
                "home": info["home"] or prev.get("home", ""),
                "away": info["away"] or prev.get("away", ""),
                "attendance": info["attendance"] if info["attendance"] is not None else prev.get("attendance", ""),
                "source_url": info["source_url"],
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "attempts": str(attempts),
            }
            if info["attendance"] is not None:
                found += 1
                print(f"  -> {info['home']} vs {info['away']}: {info['attendance']} ({info['date']})")
            else:
                print(f"  -> {info['home']} vs {info['away']}: (nėra attendance)")

        browser.close()

    save_matches(matches_path, matches)
    rows, unresolved = compute_averages(matches, aliases, args.season)
    write_outputs(data_dir, rows, unresolved, args.season)

    print(f"--- PABAIGA --- naujų su lankomumu: {found}; viso rungtynių: {len(matches)}; klubų: {len(rows)}")
    for r in rows:
        avg = r["average"] if r["average"] is not None else "—"
        print(f"  {r['club']:<40} {avg:>6}  ({r['home_games_counted']} namų)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
