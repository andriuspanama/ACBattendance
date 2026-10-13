#!/usr/bin/env python3
"""BBL patikra: palygina data/bbl/matches.csv (Flashscore) su oficialiais boxscore PDF.

Skenuoja Game-ID ruožą, atsisiunčia egzistuojančius PDF, OCR'u nuskaito antraštę
(komandos, data, Attendance) ir išspausdina:
  - NESUTAMPA: abiejuose šaltiniuose yra, bet lankomumas skiriasi
  - TRŪKSTA CSV: oficialus PDF yra, o CSV eilutės nėra / lankomumas tuščias
Nieko nekeičia. PDF, datuoti anksčiau nei --season-start (taurė, draugiškos),
praleidžiami.

  python check_bbl.py --start 2006500 --end 2007100
"""
import argparse
import csv
import re
import subprocess
import tempfile
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import requests

URL = "https://api.basketball-bundesliga.de/reports/{comp}/{gid}/boxscore"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
MONTHS = {"JAN": 1, "FEB": 2, "MRZ": 3, "MAR": 3, "MÄR": 3, "APR": 4, "MAI": 5,
          "JUN": 6, "JUL": 7, "AUG": 8, "SEP": 9, "OKT": 10, "NOV": 11, "DEZ": 12}
# raktažodis klubo pavadinime -> BBL trumpinys
CODES = {"alba": "BER", "oldenburg": "OLD", "trier": "TRI", "bonn": "BON",
         "bayern": "FCB", "ulm": "ULM", "chemnitz": "CHE", "bamberg": "BAM",
         "rostock": "HRO", "ludwigsburg": "LUD", "braunschweig": "BRA",
         "frankfurt": "FRS", "hagen": "HAG", "vechta": "VEC", "wurzburg": "WUE",
         "hamburg": "HAM", "jena": "JEN", "mbc": "MBC"}


def code_of(name):
    n = unicodedata.normalize("NFD", name or "")
    n = "".join(c for c in n if not unicodedata.combining(c)).lower()
    for k, v in CODES.items():
        if k in n:
            return v
    return None


def ocr_pdf(pdf_path, dpi=200, psm=6):
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "p"
        subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-f", "1", "-l", "1",
                        str(pdf_path), str(base)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        img = next(Path(tmp).glob("p-*.png"))
        out = subprocess.run(["tesseract", str(img), "-", "--psm", str(psm)],
                             capture_output=True, text=True, check=True)
        return out.stdout


def parse(text):
    m_att = re.search(r"Attendance\s*[:;]?\s*([\d.,\s]{1,9}\d)", text)
    m_date = re.search(r"\b(\d{1,2})\s+([A-ZÄÖÜ]{3})\s+(20\d{2})", text)
    teams = re.findall(r"^\W*([A-Z]{3})\s*[-–—]\s*([^\n(]+?)\s*\(\s*Coach", text, re.M)
    if not (m_att and len(teams) >= 2):
        return None
    d = ""
    if m_date and m_date.group(2) in MONTHS:
        d = f"{int(m_date.group(3)):04d}-{MONTHS[m_date.group(2)]:02d}-{int(m_date.group(1)):02d}"
    return {"att": int(re.sub(r"\D", "", m_att.group(1))), "date": d,
            "home": teams[0][0], "away": teams[1][0]}


def read_pdf(path):
    """OCR su atsargine eile: kitas DPI / kitas puslapio režimas."""
    last = ""
    for dpi, psm in ((200, 6), (300, 6), (300, 4)):
        try:
            last = ocr_pdf(path, dpi, psm)
        except Exception:
            continue
        info = parse(last)
        if info:
            return info, last
    return None, last


def fetch(args_gid):
    comp, gid = args_gid
    try:
        r = requests.get(URL.format(comp=comp, gid=gid), headers={"User-Agent": UA}, timeout=30)
        if r.status_code == 200 and r.content.startswith(b"%PDF"):
            return gid, r.content
    except requests.RequestException:
        pass
    return gid, None


def load_csv(path):
    rows = {}
    with path.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            h, a = code_of(r.get("home")), code_of(r.get("away"))
            if h and a:
                rows[(h, a)] = r
    return rows


def days_apart(d1, d2):
    try:
        return abs((date.fromisoformat(d1) - date.fromisoformat(d2)).days)
    except ValueError:
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--comp", default="485")
    ap.add_argument("--start", type=int, default=2006500)
    ap.add_argument("--end", type=int, default=2007100)
    ap.add_argument("--csv", default="data/bbl/matches.csv")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--season-start", default="2026-09-17",
                    help="anksčiau datuoti PDF (taurė, draugiškos) praleidžiami")
    args = ap.parse_args()

    csv_rows = load_csv(Path(args.csv))
    ids = [(args.comp, g) for g in range(args.start, args.end + 1)]
    print(f"Skenuojama {args.start}–{args.end} ({len(ids)} ID)...")
    with ThreadPoolExecutor(args.workers) as ex:
        results = [x for x in ex.map(fetch, ids) if x[1]]
    print(f"Rasta PDF: {len(results)}")

    official, unreadable, skipped = {}, [], []
    for gid, data in sorted(results):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as t:
            t.write(data); t.flush()
            info, text = read_pdf(Path(t.name))
        if not info:
            unreadable.append((gid, text[:150].replace("\n", " | ")))
            continue
        info["gid"] = gid
        if info["date"] and info["date"] < args.season_start:
            skipped.append(info)
            continue
        official[(info["home"], info["away"])] = info
        print(f"  ID {gid}  {info['date']}  {info['home']}–{info['away']}  {info['att']}")

    bad, missing, ok = [], [], 0
    for key, o in official.items():
        row = csv_rows.get(key)
        if row and row.get("date") and o["date"] and days_apart(row["date"], o["date"]) > 3:
            row = None  # ta pati pora, bet kita data -> kitos rungtynės
        cur = (row or {}).get("attendance", "").strip()
        if not row or not cur:
            missing.append((key, o))
        elif int(cur) != o["att"]:
            bad.append((key, o, int(cur)))
        else:
            ok += 1

    link = lambda o: URL.format(comp=args.comp, gid=o["gid"])
    print(f"\nSutampa su Flashscore: {ok} iš {len(official)}")
    print("\n=== NESUTAMPA (Flashscore vs oficialus) ===")
    for (h, a), o, cur in bad:
        print(f"  {h}–{a} {o['date']}: CSV {cur}, oficialus {o['att']}  "
              f"(skirtumas {o['att']-cur:+d})  {link(o)}")
    if not bad:
        print("  nėra")
    print("\n=== TRŪKSTA CSV (oficialus PDF yra) ===")
    for (h, a), o in missing:
        print(f"  {h}–{a} {o['date']}: oficialus {o['att']}  {link(o)}")
    if not missing:
        print("  nėra")
    if skipped:
        print(f"\nPraleista (data prieš {args.season_start}, ne lygos rungtynės): "
              + ", ".join(f"{o['gid']} {o['home']}–{o['away']}" for o in skipped))
    if unreadable:
        print("\nNepavyko perskaityti OCR:")
        for gid, snip in unreadable:
            print(f"  {gid}: {snip}")
    not_found = [f"{h}–{a}" for (h, a) in csv_rows if (h, a) not in official]
    print(f"\nCSV eilučių be oficialaus PDF skenuotame ruože: {len(not_found)}")
    print("  " + ", ".join(sorted(not_found)))
    print("--- PABAIGA --- OCR gali suklysti; neatitikimus patikrink atidaręs PDF nuorodą.")


if __name__ == "__main__":
    main()
