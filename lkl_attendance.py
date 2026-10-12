#!/usr/bin/env python3
"""
LKL (Lietuvos krepšinio lyga) namų rungtynių lankomumas iš Flashscore.

Ką daro:
  1. Surenka sezono rungtynių nuorodas iš Flashscore LKL results puslapio.
  2. Kiekvienoms rungtynėms atidaro puslapį (Playwright) ir ištraukia
     Attendance, datą, komandas.
  3. Saugo data/lkl/matches.csv
  4. Perskaičiuoja namų lankomumo vidurkius -> attendance_averages.json/.csv

Naudojimas:
  pip install requests beautifulsoup4 playwright
  playwright install chromium
  python lkl_attendance.py --season 2026-27 --max-fetch 80
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

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    sync_playwright = None  # type: ignore

LEAGUE = "LKL"
BASE = "https://www.flashscore.com"
RESULTS_URL = "https://www.flashscore.com/basketball/lithuania/lkl/results/"
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
    return n if 50 < n < 25000 else None


def parse_date(text: str) -> str:
    # 12.10.2025 or 12.10.2025 09:50
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return ""


def extract_teams_from_title(title: str) -> tuple[str, str]:
    # "Juventus v Siauliai 12/10/2025 | Basketball - Flashscore"
    m = re.search(r"^(.+?)\s+v\s+(.+?)(?:\s+\d|/|\|)", title, re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", ""


def compute_averages(matches: dict, aliases: dict, season: str):
    unresolved: set[str] = set()
    per_club: dict[str, list[int]] = defaultdict(list)
    all_homes: set[str] = set()

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
                per_club[club].append(int(att)
... 
