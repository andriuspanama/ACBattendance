#!/usr/bin/env python3
"""
Rankiniu būdu suvesto lankomumo nuskaitymas iš Google Sheets lapo (Turkijai – BSL).

Google Sheets faile sukurkite lapą (numatytas pavadinimas „BSL 26/27 games“) su stulpeliais:
    Date | Home | Away | Arena | Attendance | Link | GeniusId
Lentelę patogiausia įklijuoti mygtuku tools/tbf_bookmarklet.js (tbf.org.tr svetainėje).
Jei „Attendance“ tuščias, bet yra „GeniusId“ – lankomumas paimamas automatiškai iš FIBA LiveStats.
Ranka įrašytas skaičius visada turi pirmenybę. Data: 2026-10-25, 25.10.2026 arba 10/25/2026.

Rezultatas – tie patys failai ir formatas kaip kitų lygų:
  data/bsl/matches.csv, attendance_averages.json/.csv, unresolved_teams.txt

Naudojimas:
  python manual_attendance.py --sheet-id <ID> --sheet "BSL 26/27 games" --league BSL --data-dir data/bsl
"""
from __future__ import annotations

import argparse
import csv
import io
import re
from datetime import datetime, timedelta, timezone
import sys
import urllib.parse

import requests

from official_attendance import Ctx, norm, to_int, needs_fetch, today
from run_status import write_status

SOURCE = "Google Sheets + FIBA LiveStats"


def parse_date(s: str) -> str:
    s = (s or "").strip()
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}"
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        return f"{int(m[3]):04d}-{int(m[2]):02d}-{int(m[1]):02d}"
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)   # Google Sheets JAV formatas M/D/YYYY
    if m:
        return f"{int(m[3]):04d}-{int(m[1]):02d}-{int(m[2]):02d}"
    m = re.match(r"Date\((\d{4}),(\d{1,2}),(\d{1,2})", s)  # gviz Date(2026,9,25) – mėnuo nuo 0
    if m:
        return f"{int(m[1]):04d}-{int(m[2]) + 1:02d}-{int(m[3]):02d}"
    return ""


def main() -> int:
    try:
        return run()
    except Exception as e:  # noqa: BLE001
        print(f"KLAIDA {e}", file=sys.stderr)
        a = parse_args()
        write_status(a.data_dir, a.league, SOURCE, False, f"nepavyko nuskaityti Google Sheets lapo „{a.sheet}“ ({e.__class__.__name__})")
        return 1


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sheet-id", required=True)
    ap.add_argument("--sheet", default="BSL 26/27 games")
    ap.add_argument("--league", default="BSL")
    ap.add_argument("--season", default="2026-27")
    ap.add_argument("--data-dir", default="data/bsl")
    return ap.parse_args()


def run() -> int:
    args = parse_args()

    url = (f"https://docs.google.com/spreadsheets/d/{args.sheet_id}/gviz/tq?tqx=out:csv&headers=1"
           f"&sheet={urllib.parse.quote(args.sheet)}")
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8"))))
    if not rows:
        print("Lapas tuščias", file=sys.stderr)
        write_status(args.data_dir, args.league, SOURCE, False, f"lapas „{args.sheet}“ tuščias arba nerastas")
        return 1
    head = [norm(h) for h in rows[0]]

    def col(*names):
        for n in names:
            for i, h in enumerate(head):
                if h == n or h.startswith(n):
                    return i
        return None

    ci = {"date": col("date", "data"), "home": col("home", "seimininkai"), "away": col("away", "sveciai"),
          "arena": col("arena", "venue", "salon"), "att": col("attendance", "lankomumas", "seyirci"), "link": col("link", "url", "source"),
          "genius": col("geniusid", "genius", "fiba")}
    if ci["home"] is None or ci["att"] is None:
        print(f"Nerasti stulpeliai Home/Attendance. Antraštės: {rows[0]}", file=sys.stderr)
        write_status(args.data_dir, args.league, SOURCE, False, f"lape „{args.sheet}“ nerasti stulpeliai Home / Attendance")
        return 1

    ctx = Ctx(args.league, args.data_dir, args.season)
    old = ctx.matches
    ctx.matches = {}   # lapas – vienintelis šaltinis: kas ištrinta lape, išnyksta ir čia
    cell = lambda r, k: (r[ci[k]].strip() if ci[k] is not None and ci[k] < len(r) else "")  # noqa: E731
    fiba = requests.Session()
    fiba.headers["User-Agent"] = "Mozilla/5.0"
    fetched = 0
    recent_tried = recent_got = 0       # paskutinių 10 d. rungtynės – pagal jas sprendžiama, ar FIBA blokuoja
    recent_from = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
    for r in rows[1:]:
        home, away = cell(r, "home"), cell(r, "away")
        if not home:
            continue
        d = parse_date(cell(r, "date"))
        mid = f"{d or 'nd'}-{norm(home).replace(' ', '-')}-{norm(away).replace(' ', '-')}"
        prev = old.get(mid, {})
        raw_att = cell(r, "att").strip()
        # „0“, „seyircisiz“, „closed“, „be žiūrovų“ – rungtynės be žiūrovų: į vidurkį ir talpą neįskaičiuojamos
        if re.fullmatch(r"0+|-|seyircisiz|closed( doors)?|be žiūrovų|be ziurovu|uždarytos|uzdarytos", raw_att, re.I):
            ctx.matches[mid] = dict(prev) if prev else {}
            ctx.put(mid, d, home, away, None, cell(r, "link") or f"https://docs.google.com/spreadsheets/d/{args.sheet_id}",
                    cell(r, "arena"), count_attempt=False)
            ctx.matches[mid]["attendance"] = ""
            ctx.matches[mid]["attempts"] = "99"
            continue
        att = to_int(raw_att)
        gid = re.sub(r"\D", "", cell(r, "genius"))
        attempt = False
        if not att and prev.get("attendance"):
            att = to_int(prev["attendance"])
        elif not att and gid and d and d <= today() and prev.get("attempts") != "99" \
                and needs_fetch(prev or None, 3) and fetched < 120:
            attempt, fetched = True, fetched + 1
            try:
                j = fiba.get(f"https://fibalivestats.dcd.shared.geniussports.com/data/{gid}/data.json", timeout=20)
                if j.status_code == 200:
                    att = to_int(j.json().get("attendance"))
            except Exception as e:  # noqa: BLE001
                print(f"  {gid}: {e}", file=sys.stderr)
            print(f"  {d} {home} – {away}: {att}")
            if d >= recent_from:
                recent_tried += 1
                recent_got += 1 if att else 0
        if prev:
            ctx.matches[mid] = prev
        ctx.put(mid, d, home, away, att,
                cell(r, "link") or f"https://docs.google.com/spreadsheets/d/{args.sheet_id}", cell(r, "arena"),
                count_attempt=attempt)
        if not gid and not att and not prev:
            ctx.matches[mid]["attempts"] = "0"
    ctx.source = SOURCE
    ctx.listed = None
    if recent_tried >= 3 and not recent_got:
        ctx.problems.append(f"FIBA LiveStats nedavė lankomumo nė vienoms iš {recent_tried} paskutinių rungtynių – gali blokuoti")
    elif ctx.tried and not ctx.got_att:
        # senesnės rungtynės, kurių FIBA lankomumo neturi – įrašykite ranka lape (stulpelis Attendance)
        ctx.notes.append(f"{ctx.tried} rungtynės be lankomumo FIBA LiveStats – įrašykite ranka lape")
    return ctx.finish()


if __name__ == "__main__":
    sys.exit(main())
