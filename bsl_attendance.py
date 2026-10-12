#!/usr/bin/env python3
"""
BSL (Basketbol Süper Ligi) namų rungtynių lankomumas iš TBF oficialios svetainės.

Ką daro:
  1. Surenka sezono mačų ID iš TBF mačų sąrašo.
  2. Kiekvienam mačui atidaro mac-detay puslapį ir ištraukia
     "Seyirci Sayısı", komandas, datą.
  3. Saugo data/bsl/matches.csv
  4. Perskaičiuoja namų lankomumo vidurkius -> attendance_averages.json/.csv

Naudojimas:
  pip install requests beautifulsoup4 playwright
  playwright install chromium
  python bsl_attendance.py --season 2026-27 --max-fetch 80
  python bsl_attendance.py --recompute-only
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

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    sync_playwright = None  # type: ignore

LEAGUE = "BSL"
BASE = "https://www.tbf.org.tr"
RESULTS_URL = "https://www.tbf.org.tr/ligler/bsl-2026-2027/maclar"
MATCH_URL = "https://www.tbf.org.tr/ligler/bsl-2026-2027/mac-detay/{mid}"
SEASON_START = "2026-09-01"
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
        for mid in sorted(matches.keys(), key=lambda x: int(x) if x.isdigit() else 0):
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
    m = re.search(r"Seyirci\s+Sayısı\s*[:：]?\s*([\d.,\s]+)", text, re.I)
    if not m:
        return None
    n = int(re.sub(r"[^\d]", "", m.group(1)))
    return n if 0 < n < 30000 else None


def parse_date_from_text(text: str) -> str:
    # Formatai: "4 Ekim 2026", "10 Ekim 2026 Pazar 15:30"
    months = {
        "ocak": 1, "şubat": 2, "mart": 3, "nisan": 4, "mayıs": 5, "haziran": 6,
        "temmuz": 7, "ağustos": 8, "eylül": 9, "ekim": 10, "kasım": 11, "aralık": 12,
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    }
    m = re.search(r"(\d{1,2})\s+([A-Za-zşŞıİğĞüÜöÖçÇ]+)\s+(20\d{2})", text)
    if m:
        day = int(m.group(1))
        mon_name = m.group(2).lower()
        year = int(m.group(3))
        mon = months.get(mon_name)
        if mon:
            return f"{year:04d}-{mon:02d}-{day:02d}"
    return ""


def clean_team_name(name: str) -> str:
    if not name:
        return name
    name = name.strip()
    name = re.sub(r"\s+", " ", name)
    return name


def compute_averages(matches: dict, aliases: dict, season: str):
    unresolved: set[str] = set()
    per_club: dict[str, list[int]] = defaultdict(list)
    all_homes: set[str] = set()

    for m in matches.values():
        if m.get("season") != season:
            continue
        match_date = (m.get("date") or "").strip()
        if match_date and match_date < SEASON_START:
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


def collect_match_ids(page, max_pages: int = 10) -> list[str]:
    """Surenka mačų ID iš mačų sąrašo puslapio."""
    ids: set[str] = set()

    page.goto(RESULTS_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)

    for _ in range(max_pages):
        # Surenkame visas mac-detay nuorodas
        links = page.eval_on_selector_all(
            "a[href*='/mac-detay/']",
            "els => els.map(e => e.getAttribute('href'))"
        )
        for href in links or []:
            m = re.search(r"/mac-detay/(\d+)", href or "")
            if m:
                ids.add(m.group(1))

        # Bandome spausti "kitas" / "next" arba scroll
        try:
            next_btn = page.locator("text=Sonraki, text=Next, text=›, text=»").first
            if next_btn.is_visible(timeout=1000):
                next_btn.click()
                page.wait_for_timeout(1500)
            else:
                break
        except Exception:
            break

    return sorted(ids, key=int)


def scrape_match(page, mid: str) -> dict | None:
    """Atidaro mačo detalių puslapį ir ištraukia lankomumą."""
    url = MATCH_URL.format(mid=mid)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(1800)

        text = page.inner_text("body")
        title = page.title()

        att = parse_attendance(text)
        date = parse_date_from_text(text)

        # Komandos – dažniausiai matomos antraštėje arba scoreboard
        home, away = "", ""
        # Bandome iš title arba iš matomų komandų pavadinimų
        m = re.search(r"([A-ZÇĞİÖŞÜ0-9\s\.\-]+)\s+(\d+)\s*[-–]\s*(\d+)\s+([A-ZÇĞİÖŞÜ0-9\s\.\-]+)", text)
        if m:
            home = clean_team_name(m.group(1))
            away = clean_team_name(m.group(4))
        else:
            # Alternatyva – ieškome didžiųjų raidžių komandų
            teams = re.findall(r"\b([A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜ\s\.\-]{4,40})\b", text)
            teams = [t.strip() for t in teams if len(t.strip()) > 4 and "SEYIRCI" not in t.upper()]
            if len(teams) >= 2:
                home, away = teams[0], teams[1]

        return {
            "match_id": mid,
            "date": date,
            "home": home,
            "away": away,
            "attendance": att,
            "source_url": url,
        }
    except PlaywrightTimeout:
        return None
    except Exception as e:
        print(f"  Klaida {url}: {e}", file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", default="2026-27")
    ap.add_argument("--data-dir", default="data/bsl")
    ap.add_argument("--max-fetch", type=int, default=60)
    ap.add_argument("--max-pages", type=int, default=8)
    ap.add_argument("--recompute-only", action="store_true")
    ap.add_argument("--delay", type=float, default=1.0)
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
            locale="tr-TR",
        )
        page = context.new_page()

        print("Renku mačų ID...")
        ids = collect_match_ids(page, max_pages=args.max_pages)
        print(f"Rasta mačų ID: {len(ids)}")

        to_fetch = [mid for mid in ids if mid not in matches or not matches[mid].get("attendance")]
        to_fetch = to_fetch[: args.max_fetch]
        print(f"Bus tikrinama: {len(to_fetch)}")

        found = 0
        for i, mid in enumerate(to_fetch, 1):
            print(f"[{i}/{len(to_fetch)}] {mid}")
            info = scrape_match(page, mid)
            time.sleep(args.delay)
            if not info:
                continue

            match_date = info["date"] or ""
            if match_date and match_date < SEASON_START:
                print(f"  -> praleista (prieš sezoną – {match_date})")
                continue

            prev = matches.get(mid, {})
            attempts = int(prev.get("attempts") or 0) + 1
            home_clean = clean_team_name(info["home"] or prev.get("home", ""))
            away_clean = clean_team_name(info["away"] or prev.get("away", ""))

            matches[mid] = {
                "league": LEAGUE,
                "season": args.season,
                "match_id": mid,
                "date": match_date,
                "home": home_clean,
                "away": away_clean,
                "attendance": info["attendance"] if info["attendance"] is not None else prev.get("attendance", ""),
                "source_url": info["source_url"],
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "attempts": str(attempts),
            }
            if info["attendance"] is not None:
                found += 1
                print(f"  -> {home_clean} vs {away_clean}: {info['attendance']} ({match_date})")
            else:
                print(f"  -> {home_clean} vs {away_clean}: (nėra attendance)")

        browser.close()

    save_matches(matches_path, matches)
    rows, unresolved = compute_averages(matches, aliases, args.season)
    write_outputs(data_dir, rows, unresolved, args.season)

    print(f"--- PABAIGA --- naujų su lankomumu: {found}; viso: {len(matches)}; klubų: {len(rows)}")
    for r in rows:
        avg = r["average"] if r["average"] is not None else "—"
        print(f"  {r['club']:<40} {avg:>6}  ({r['home_games_counted']} namų)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
