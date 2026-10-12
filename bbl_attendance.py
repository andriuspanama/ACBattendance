#!/usr/bin/env python3
"""BBL (easyCredit Basketball Bundesliga) namu rungtyniu lankomumas.

Kiekvienoms rungtynems atsisiuncia boxscore PDF:
  https://api.basketball-bundesliga.de/reports/<comp>/<game_id>/boxscore
PDF teksto sluoksnis sugadintas, todel antraste skaitoma per OCR (tesseract).
Isvestis tokio paties formato kaip ACB / LKL: data/bbl/matches.csv,
attendance_averages.csv / .json, unresolved_teams.txt
"""
import argparse, csv, json, re, subprocess, sys, tempfile, time
from datetime import datetime, timezone
from pathlib import Path

import requests

LEAGUE = "BBL"
URL = "https://api.basketball-bundesliga.de/reports/{comp}/{gid}/boxscore"
MONTHS = {"JAN": 1, "FEB": 2, "MRZ": 3, "MAR": 3, "MÄR": 3, "APR": 4, "MAI": 5,
          "JUN": 6, "JUL": 7, "AUG": 8, "SEP": 9, "OKT": 10, "NOV": 11, "DEZ": 12}
FIELDS = ["league", "season", "game_id", "date", "home", "away", "attendance",
          "url", "fetched_at", "attempts", "home_code", "away_code"]


# ---------- PDF -> tekstas (OCR) ----------
def ocr_pdf(pdf_path: Path) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "p"
        subprocess.run(["pdftoppm", "-png", "-r", "200", "-f", "1", "-l", "1",
                        str(pdf_path), str(base)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        img = next(Path(tmp).glob("p-*.png"))
        out = subprocess.run(["tesseract", str(img), "-", "--psm", "6"],
                             capture_output=True, text=True, check=True)
        return out.stdout


def parse_boxscore(text: str) -> dict | None:
    """Grazina {home, away, home_code, away_code, attendance, date} arba None."""
    m_att = re.search(r"Attendance\s*[:;]?\s*([\d.,\s]{1,9}\d)", text)
    m_date = re.search(r"\b(\d{1,2})\s+([A-ZÄÖÜ]{3})\s+(20\d{2})", text)
    teams = re.findall(r"^\W*([A-Z]{3})\s*[-–—]\s*([^\n(]+?)\s*\(\s*Coach", text, re.M)
    if not (m_att and len(teams) >= 2):
        return None
    attendance = int(re.sub(r"\D", "", m_att.group(1)))  # '4.023' -> 4023
    date = ""
    if m_date and m_date.group(2) in MONTHS:
        date = f"{int(m_date.group(3)):04d}-{MONTHS[m_date.group(2)]:02d}-{int(m_date.group(1)):02d}"
    (hc, hn), (ac, an) = teams[0], teams[1]  # pirma komanda = namu
    return {"home_code": hc, "home": hn.strip(), "away_code": ac,
            "away": an.strip(), "attendance": attendance, "date": date}


# ---------- saugojimas ----------
def load_matches(path: Path) -> dict:
    rows = {}
    if path.exists():
        with path.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                rows[r["game_id"]] = r
    return rows


def save_matches(path: Path, rows: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for gid in sorted(rows, key=int):
            w.writerow({k: rows[gid].get(k, "") for k in FIELDS})


def compute_averages(rows: dict, season: str):
    per = {}
    for r in rows.values():
        if not r.get("attendance"):
            continue
        d = per.setdefault(r["home"], {"code": r["home_code"], "v": []})
        d["v"].append(int(r["attendance"]))
    out = []
    for club, d in per.items():
        v = d["v"]
        out.append({"league": LEAGUE, "season": season, "club": club,
                    "code": d["code"], "home_games_counted": len(v),
                    "average": round(sum(v) / len(v)), "total": sum(v),
                    "max": max(v), "min": min(v)})
    out.sort(key=lambda x: -x["average"])
    return out


def write_outputs(data_dir: Path, rows: list, season: str):
    data_dir.mkdir(parents=True, exist_ok=True)
    cols = ["league", "season", "club", "home_games_counted", "average", "total", "max", "min"]
    with (data_dir / "attendance_averages.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    payload = {"league": LEAGUE, "season": season,
               "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "clubs": rows}
    (data_dir / "attendance_averages.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (data_dir / "unresolved_teams.txt").write_text("", encoding="utf-8")


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default="2026-27")
    ap.add_argument("--comp", default="486", help="ID URL'e tarp /reports/ ir game_id")
    ap.add_argument("--start", type=int, default=2006800, help="pirmas tikrinamas Game-ID")
    ap.add_argument("--ahead", type=int, default=120, help="kiek ID uz didziausio zinomo tikrinti")
    ap.add_argument("--data-dir", default="data/bbl")
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--pdf", help="tik isbandyti vietini PDF (be tinklo)")
    args = ap.parse_args()

    if args.pdf:
        info = parse_boxscore(ocr_pdf(Path(args.pdf)))
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0 if info else 1

    data_dir = Path(args.data_dir)
    matches_path = data_dir / "matches.csv"
    rows = load_matches(matches_path)
    known = [int(g) for g in rows]
    last = max(known) if known else args.start
    end = max(last + args.ahead, args.start + args.ahead)

    sess = requests.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0 (attendance-bot)"
    found = errors = 0

    # Skenuojame nuo mažiausio žinomo / start iki end
    for gid in range(min(args.start, last), end + 1):
        key = str(gid)
        if key in rows and rows[key].get("attendance"):
            continue
        url = URL.format(comp=args.comp, gid=gid)
        try:
            resp = sess.get(url, timeout=30)
        except requests.RequestException as e:
            print(f"{gid}: tinklo klaida {e}")
            errors += 1
            continue
        time.sleep(args.delay)
        if resp.status_code != 200 or not resp.content.startswith(b"%PDF"):
            continue  # rungtyniu dar nera / nesuzaistos
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            tmp.write(resp.content)
            tmp.flush()
            try:
                info = parse_boxscore(ocr_pdf(Path(tmp.name)))
            except Exception as e:  # OCR/PDF klaida
                print(f"{gid}: OCR klaida {e}")
                errors += 1
                continue
        if not info:
            print(f"{gid}: nepavyko perskaityti antrastes")
            errors += 1
            continue
        rows[key] = {
            "league": LEAGUE,
            "season": args.season,
            "game_id": key,
            "date": info["date"],
            "home": info["home"],
            "away": info["away"],
            "attendance": info["attendance"],

            "url": url,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "attempts": 1,
            "home_code": info["home_code"],
            "away_code": info["away_code"],
        }
        found += 1
        print(f"{gid}: {info['home']} - {info['away']}  {info['attendance']}  {info['date']}")

    save_matches(matches_path, rows)
    avg = compute_averages(rows, args.season)
    write_outputs(data_dir, avg, args.season)
    print(f"--- PABAIGA --- nauju: {found}; klaidu: {errors}; klubu su lankomumu: {len(avg)}")
    for r in avg:
        print(f"  {r['club']:<32} {r['average']:>6}  ({r['home_games_counted']} namu rungt.)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
