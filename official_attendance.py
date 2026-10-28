#!/usr/bin/env python3
"""
Namų rungtynių lankomumas iš OFICIALIŲ lygų svetainių (be Flashscore).

Lygos:
  aus   NBL (Australija)            schedule.nbl.com.au duomenų srautas
  fin   Korisliiga (Suomija)        basket.fi (Sportradar) duomenų srautas
  lvee  Latvijos–Estijos lyga       estlatbl.com + FIBA LiveStats
  bbl   BBL (Vokietija)             easycredit-bbl.de rungtynių puslapiai
  aba   ABA lyga                    aba-liga.com
  ita   LBA (Italija)               legabasket.it duomenų srautas
  pol   PLK (Lenkija)               plk.pl (arena) + Flashscore (lankomumas)
  gre   GBL (Graikija)              esake.gr (arena) + Flashscore (lankomumas)

Kiekvienai lygai rašomi tie patys failai ir tas pats formatas kaip LKL/BBL:
  data/<aplankas>/matches.csv               (+ papildomas stulpelis „arena“ gale)
  data/<aplankas>/attendance_averages.json  ({league, season, updated, clubs:[...]})
  data/<aplankas>/attendance_averages.csv
  data/<aplankas>/unresolved_teams.txt      (klubai, kurių namų rungtynėse dar nėra lankomumo)

Naudojimas:
  pip install requests beautifulsoup4
  python official_attendance.py --league aus
  python official_attendance.py --league all
  python official_attendance.py --league ita --recompute-only
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import re
import sys
import time
import unicodedata
import zlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import os

import requests
from bs4 import BeautifulSoup

from run_status import write_status
from regular_season import playoff_ids

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

MATCH_FIELDS = [
    "league", "season", "match_id", "date", "home", "away",
    "attendance", "source_url", "checked_at", "attempts", "arena", "capacity",
]

# Jei lankomumo dar nėra – bandome kelis kartus, paskui tik kelias dienas po rungtynių.
MAX_ATTEMPTS = 4
RECHECK_DAYS = 3


# --------------------------------------------------------------------------- bendra

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def season_start(season: str) -> str:
    m = re.match(r"(\d{4})", season or "")
    y = int(m.group(1)) if m else datetime.now(timezone.utc).year
    return f"{y:04d}-08-01"


def season_year(season: str) -> int:
    return int(season_start(season)[:4])


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("ł", "l").replace("Ł", "L").replace("ı", "i").replace("ø", "o").replace("đ", "d")
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def clean(s) -> str:
    """Tekstas be kablelių/naujų eilučių (ACBattendance puslapis CSV skaido paprastai per kablelius)."""
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s.replace(", ", " ").replace(",", " ").replace('"', "")


def to_int(v):
    if v in (None, ""):
        return None
    try:
        n = int(re.sub(r"[^\d]", "", str(v)) or "x")
    except ValueError:
        return None
    return n if n > 0 else None


def make_session(referer: str | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    if referer:
        s.headers["Referer"] = referer
    return s


STATUS = {"written": False}
BLOCK = {"hits": 0, "last": ""}   # kiek kartų svetainė atsakė „uždrausta“ / patikra prieš robotus
CHALLENGE = re.compile(r"cf-chl|challenge-platform|Just a moment\.\.\.|captcha|Access Denied", re.I)


def _note_block(r) -> None:
    head = r.text[:3000] if "html" in (r.headers.get("Content-Type") or "") else ""
    if r.status_code in (403, 429, 503) or (head and CHALLENGE.search(head)):
        BLOCK["hits"] += 1
        BLOCK["last"] = f"HTTP {r.status_code} {r.url.split('?')[0]}"


def get(sess: requests.Session, url: str, *, expect_json=False, tries=3, **kw):
    last = None
    for i in range(tries):
        try:
            r = sess.get(url, timeout=30, **kw)
            _note_block(r)
            if r.status_code in (400, 403, 404):
                return r
            r.raise_for_status()
            if expect_json:
                r.json()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def load_matches(path: Path) -> dict:
    out = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                mid = (r.get("match_id") or "").strip()
                if mid:
                    out[mid] = r
    return out


def save_matches(path: Path, matches: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MATCH_FIELDS)
        w.writeheader()
        for mid in sorted(matches, key=lambda k: (matches[k].get("date", ""), k)):
            w.writerow({k: matches[mid].get(k, "") for k in MATCH_FIELDS})


def needs_fetch(existing: dict | None, recheck_days: int) -> bool:
    if not existing:
        return True
    if existing.get("attendance"):
        return False
    if int(existing.get("attempts") or 0) < MAX_ATTEMPTS:
        return True
    d = (existing.get("date") or "").strip()
    try:
        age = datetime.now(timezone.utc).date() - datetime.strptime(d, "%Y-%m-%d").date()
    except ValueError:
        return False
    return age <= timedelta(days=recheck_days)


def compute_averages(matches: dict, season: str, league: str):
    start = season_start(season)
    per_club: dict[str, list[int]] = defaultdict(list)
    arenas: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    caps: dict[str, int] = {}
    homes: set[str] = set()
    not_regular = playoff_ids(league, matches, season)   # tik reguliarusis sezonas
    for m in matches.values():
        if m.get("season") != season:
            continue
        if str(m.get("match_id")) in not_regular:
            continue
        if (m.get("date") or "") and m["date"] < start:
            continue
        club = (m.get("home") or "").strip()
        if not club:
            continue
        homes.add(club)
        att = to_int(m.get("attendance"))
        if att:
            per_club[club].append(att)
            if (m.get("arena") or "").strip():
                arenas[club][m["arena"].strip()] += 1
                if to_int(m.get("capacity")):
                    caps[m["arena"].strip()] = to_int(m["capacity"])

    rows = []
    for club in sorted(homes):
        vals = per_club.get(club, [])
        row = {"league": league, "season": season, "club": club,
               "home_games_counted": len(vals),
               "average": round(sum(vals) / len(vals)) if vals else None,
               "total": sum(vals),
               "max": max(vals) if vals else None,
               "min": min(vals) if vals else None}
        if arenas.get(club):
            row["arenas"] = [dict({"arena": a, "games": n}, **({"capacity": caps[a]} if a in caps else {}))
                             for a, n in sorted(arenas[club].items(), key=lambda x: -x[1])]
        rows.append(row)
    rows.sort(key=lambda r: (-(r["average"] if r["average"] is not None else -1), r["club"]))
    unresolved = {r["club"] for r in rows if r["average"] is None}
    return rows, unresolved


def write_outputs(data_dir: Path, rows, unresolved, season: str, league: str):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "attendance_averages.json").write_text(
        json.dumps({"league": league, "season": season, "updated": now_iso(), "clubs": rows},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    fields = ["league", "season", "club", "home_games_counted", "average", "total", "max", "min"]
    with (data_dir / "attendance_averages.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r[k]) for k in fields})
    (data_dir / "unresolved_teams.txt").write_text(
        ("\n".join(sorted(unresolved)) + "\n") if unresolved else "", encoding="utf-8")


class Ctx:
    """Vienos lygos būsena per paleidimą."""

    def __init__(self, league: str, data_dir: str, season: str, recheck_days: int = RECHECK_DAYS):
        self.league = league
        self.season = season
        self.dir = Path(data_dir)
        self.path = self.dir / "matches.csv"
        self.matches = load_matches(self.path)
        self.recheck = recheck_days
        self.start = season_start(season)
        self.new_att = 0
        self.fetched = 0
        self.tried = 0        # kiek sužaistų rungtynių tikrinta šį kartą
        self.got_att = 0      # iš jų – su lankomumu
        self.listed = None    # kiek sužaistų rungtynių rasta lygos sąraše (None – nežinoma)
        self.problems: list[str] = []
        self.notes: list[str] = []
        self.source = ""

    def wants(self, mid: str) -> bool:
        return needs_fetch(self.matches.get(str(mid)), self.recheck)

    def put(self, mid, date, home, away, attendance, url, arena="", count_attempt=True, capacity=None):
        mid = str(mid)
        prev = self.matches.get(mid, {})
        att = to_int(attendance)
        if att and not prev.get("attendance"):
            self.new_att += 1
        if count_attempt:
            self.tried += 1
            self.got_att += 1 if att else 0
        self.matches[mid] = {
            "league": self.league, "season": self.season, "match_id": mid,
            "date": date or prev.get("date", ""),
            "home": clean(home) or prev.get("home", ""),
            "away": clean(away) or prev.get("away", ""),
            "attendance": str(att) if att else prev.get("attendance", ""),
            "source_url": url or prev.get("source_url", ""),
            "checked_at": now_iso(),
            "attempts": str(int(prev.get("attempts") or 0) + (1 if count_attempt else 0)),
            "arena": clean(arena) or prev.get("arena", ""),
            "capacity": str(to_int(capacity)) if to_int(capacity) else prev.get("capacity", ""),
        }

    def finish(self) -> int:
        save_matches(self.path, self.matches)
        rows, unresolved = compute_averages(self.matches, self.season, self.league)
        write_outputs(self.dir, rows, unresolved, self.season, self.league)
        games = [m for m in self.matches.values() if m.get("season") == self.season]
        with_att = sum(1 for m in games if to_int(m.get("attendance")))
        print(f"--- {self.league}: naujai rasta su lankomumu {self.new_att}; sezono rungtynių {len(games)}, "
              f"iš jų su lankomumu {with_att}; klubų {len(rows)}")
        for r in rows:
            avg = r["average"] if r["average"] is not None else "—"
            ar = " / ".join(a["arena"] for a in r.get("arenas", []))
            print(f"  {r['club']:<38} {avg:>6}  ({r['home_games_counted']} namų)  {ar}")
        return self.report()

    def report(self) -> int:
        """Ar nepanašu, kad svetainė blokuoja / pasikeitė – įrašoma į run_status.json (rodoma puslapyje)."""
        known = sum(1 for m in self.matches.values() if m.get("season") == self.season)
        late = today() > (datetime.strptime(self.start, "%Y-%m-%d") + timedelta(days=40)).date().isoformat()
        if BLOCK["hits"] >= 3 and not self.got_att:
            self.problems.append(f"svetainė {BLOCK['hits']} k. atsisakė duoti duomenis ({BLOCK['last']}) – greičiausiai blokuoja")
        if not self.problems and self.listed == 0 and (known or late):
            self.problems.append("lygos svetainėje nerasta nė vienų sužaistų rungtynių"
                                 + (f", nors anksčiau buvo {known}" if known else "")
                                 + " – svetainė pasikeitė arba blokuoja skaitymą")
        if self.tried >= 4 and not self.got_att:
            self.notes.append(f"Patikrinta {self.tried} sužaistų rungtynių, bet nė vienose nerasta lankomumo")
        up = os.environ.get("UPSTREAM_OUTCOME", "")
        if up == "failure":
            self.problems.append(os.environ.get("UPSTREAM_NAME", "ankstesnis žingsnis") + " nepavyko")
        STATUS["written"] = True
        write_status(self.dir, self.league, self.source or SOURCES.get(self.league, ""), not self.problems,
                     "; ".join(self.problems), self.notes, self.new_att)
        return 1 if self.problems else 0


def date_from_iso(s: str) -> str:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def date_from_dmy(s: str) -> str:
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", s or "")
    return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else ""


# --------------------------------------------------------------------------- Australija (NBL)

def run_aus(args) -> int:
    ctx = Ctx("NBL", args.data_dir or "data/nbl", args.season)
    base = "https://schedule.nbl.com.au"
    sess = make_session(base + "/nbl")
    url = f"{base}/api/calendar/schedule?league=nbl&limit=500&offset=0"
    j = get(sess, url, expect_json=True).json()
    if str(j.get("year") or "") not in ("", str(season_year(args.season))):
        j = get(sess, url + f"&year={season_year(args.season)}", expect_json=True).json()
    games = [m for m in j.get("matches", [])
             if m.get("season_type") == "regular" and m.get("phase") == "complete"]   # be Blitz / ikisezoninių
    print(f"NBL: baigtų sezono rungtynių {len(games)}")
    ctx.listed = len(games)
    todo = [m for m in games if ctx.wants(m["id"])][: args.max_fetch]
    for m in todo:
        d = datetime.fromtimestamp(m["starts_at_ms"] / 1000, tz=timezone(timedelta(hours=10))).date().isoformat() \
            if m.get("starts_at_ms") else ""
        det = get(sess, f"{base}/api/calendar/match?match={m['id']}&league=nbl", expect_json=True).json()
        mm = det.get("match") or {}
        venue = (mm.get("venue") or {}).get("name") or m.get("venue_name") or ""
        ctx.put(m["id"], d, m["home"]["name"], m["away"]["name"], mm.get("attendance"),
                f"{base}/match?league=NBL&match={m['id']}", venue)
        print(f"  {d} {m['home']['name']} – {m['away']['name']}: {mm.get('attendance')} ({venue})")
        time.sleep(args.delay)
    return ctx.finish()


# --------------------------------------------------------------------------- Suomija (Korisliiga, Sportradar)

def sr_state(obj: dict) -> str:
    raw = zlib.compress(json.dumps(obj).encode())
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def run_fin(args) -> int:
    ctx = Ctx("Korisliiga", args.data_dir or "data/fin", args.season)
    api = "https://embed-api.eui.connect.sportradar.com/v1/embed/322"
    sess = make_session("https://basket.fi/")
    sess.headers["Origin"] = "https://basket.fi"
    sid = args.season_id or get(sess, f"{api}/fixtures", expect_json=True).json()["data"]["seasonId"]
    st = sr_state({"s": sid, "l": "en-EN", "z": "RESULTS"})
    fx = get(sess, f"{api}/fixtures?state={st}", expect_json=True).json()["data"]["fixtures"] or []
    print(f"Korisliiga: sezonas {sid}, rezultatų {len(fx)}")
    ctx.listed = len(fx)
    for f in fx:
        # tik runkosarja + jatkosarja (kaip Korisliiga Excel faile); be pudotuspelit / karsinnat
        pool = f.get("pool") or ""
        if pool and not re.search(r"runkosarja|jatkosarja|loppusarja|regular", pool, re.I):
            continue
        comp = f.get("competitors") or []
        if len(comp) < 2 or any(c.get("score") is None for c in comp):
            continue
        home = next((c for c in comp if c.get("isHome")), comp[0])
        away = next((c for c in comp if c is not home), comp[1])
        d = date_from_iso(f.get("startTimeLocal") or f.get("startTimeUTC") or "")
        if d and d < ctx.start:
            continue
        mid = f["fixtureId"]
        if not ctx.wants(mid):
            continue
        link = "https://basket.fi/live/?~w=f~" + sr_state({"s": sid, "l": "en-EN", "f": mid, "z": "statistics"})
        ctx.put(mid, d, home["name"], away["name"], f.get("attendance"), link, f.get("venue") or "")
    return ctx.finish()


# --------------------------------------------------------------------------- Latvijos–Estijos lyga

def run_lvee(args) -> int:
    ctx = Ctx("LEBL", args.data_dir or "data/lvee", args.season)
    site = "https://www.estlatbl.com"
    sess = make_session(site + "/en/results")
    html = get(sess, f"{site}/en/results").text
    soup = BeautifulSoup(html, "html.parser")
    rows = [td.find_parent("tr") for td in soup.select("td.homeTeam")]
    played = []
    for tr in rows:
        if tr is None:
            continue
        m = re.search(r"results/(\d+)", str(tr)) or re.search(r"gid=(\d+)", str(tr))
        res = tr.select_one("td.result")
        if not m or not res or not re.search(r"\d+\s*:\s*\d+", res.get_text()):
            continue
        dt = tr.select_one(".dateAndTime")
        ar = tr.select_one(".arena")
        h = tr.select_one(".homeTeamNameDesktop")
        a = tr.select_one(".visitorTeamNameDesktop")
        played.append({
            "gid": m.group(1),
            "date": date_from_dmy(dt.get_text() if dt else ""),
            "arena": ar.get_text(" ", strip=True) if ar else "",
            "home": h.get_text(strip=True) if h else "",
            "away": a.get_text(strip=True) if a else "",
        })
    print(f"LEBL: sužaistų rungtynių sąraše {len(played)}")
    ctx.listed = len(played)
    todo = [g for g in played if (not g["date"] or g["date"] >= ctx.start) and ctx.wants(g["gid"])]
    for g in todo[: args.max_fetch]:
        att, src = None, f"{site}/en/results/{g['gid']}/boxscore"
        try:
            r = get(sess, f"{site}/en/results/{g['gid']}/live-stats", allow_redirects=True)
            fm = re.search(r"geniussports\.com/u/[^/]+/(\d+)", r.url) or \
                re.search(r"fibalivestats[^\"']*/(\d{6,})", r.text)
            if fm:
                fid = fm.group(1)
                src = f"https://fibalivestats.dcd.shared.geniussports.com/u/EBF/{fid}/"
                dj = get(make_session(src), f"https://fibalivestats.dcd.shared.geniussports.com/data/{fid}/data.json",
                         expect_json=True)
                if dj.status_code == 200:
                    att = dj.json().get("attendance")
        except Exception as e:  # noqa: BLE001
            print(f"  {g['gid']}: klaida {e}", file=sys.stderr)
        ctx.put(g["gid"], g["date"], g["home"], g["away"], att, src, g["arena"])
        print(f"  {g['date']} {g['home']} – {g['away']}: {att} ({g['arena']})")
        time.sleep(args.delay)
    return ctx.finish()


# --------------------------------------------------------------------------- Vokietija (BBL, oficiali svetainė)

BBL = "https://www.easycredit-bbl.de"
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def bbl_game(sess, gid) -> dict | None:
    r = get(sess, f"{BBL}/spiele/{gid}")
    if r.status_code != 200:
        return None
    m = NEXT_RE.search(r.text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))["props"]["pageProps"]["initialGameData"]
    except Exception:  # noqa: BLE001
        return None


def bbl_recent(sess, yr) -> list[str] | None:
    """Paskutinės 9 sužaistos reguliariojo sezono (Hauptrunde) rungtynės – tas pats sąrašas, kurį rodo
    easycredit-bbl.de/saison/spielplaene_liga-pokalspiele/hauptrunde. Lyga leidžia skaityti tik 1 puslapį,
    todėl juo surandamos naujos rungtynės, o visas sezonas – pagal rungtynių ID."""
    url = (f"https://api.basketball-bundesliga.de/games?currentPage=1&gameType=finished"
           f"&competition=BBL&stages[]=MAIN_ROUND&seasonId={yr}")
    try:
        r = sess.get(url, timeout=30, headers={"Origin": BBL, "Referer": BBL + "/"})
        if r.status_code != 200:
            return None
        return [str(x["id"]) for x in r.json().get("items", []) if x.get("id")]
    except Exception:  # noqa: BLE001
        return None


def run_bbl(args) -> int:
    data_dir = Path(args.data_dir or "data/bbl")
    ctx = Ctx("BBL", str(data_dir), args.season)
    # Senos Flashscore eilutės (kiti match_id) – oficialūs duomenys jas pakeičia
    fs_rows = {k: v for k, v in ctx.matches.items() if "flashscore" in (v.get("source_url") or "")}
    ctx.matches = {k: v for k, v in ctx.matches.items() if k not in fs_rows}
    sess = make_session(BBL + "/")
    yr = season_year(args.season)
    cache_path = data_dir / "official_games.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    seen: dict[str, dict] = {}

    def probe(gid) -> bool:
        g = bbl_game(sess, gid)
        time.sleep(0.15)
        if not g or str(g.get("seasonId")) != str(yr):
            return False
        seen[str(gid)] = g
        cache[str(gid)] = {"t": g.get("scheduledTime") or "", "c": g.get("competition") or "", "s": g.get("stage") or ""}
        return True

    recent = bbl_recent(sess, yr)
    if not cache:
        seeds = sorted({int(x) for x in re.findall(r"/spiele/(\d{6,})", get(sess, BBL + "/").text)}
                       | {int(x) for x in (recent or [])})
        if not seeds:
            print("BBL: nerasta rungtynių nuorodų pradiniame puslapyje", file=sys.stderr)
            return 1
        for s in seeds:
            probe(s)
        lo, hi = min(seeds), max(seeds)
        for step, start in ((-1, lo - 1), (1, hi + 1)):
            miss, gid = 0, start
            while miss < 40:
                miss = 0 if probe(gid) else miss + 1
                gid += step
        lo = min(int(k) for k in cache) if cache else lo
        for gid in range(lo, max(int(k) for k in cache) + 1):  # spragos tarp
            if str(gid) not in cache:
                probe(gid)
    elif recent is not None:  # naujos rungtynės – iš lygos sąrašo (1 užklausa vietoj ID skenavimo)
        for gid in recent:
            if gid not in cache:
                probe(int(gid))
    else:  # sąrašas nepasiekiamas – kaip anksčiau, tikrinami ID sąrašo gale
        miss, gid = 0, max(int(k) for k in cache) + 1
        while miss < 25:
            miss = 0 if probe(gid) else miss + 1
            gid += 1
    print(f"BBL: žinomų sezono rungtynių {len(cache)}")

    now = datetime.now(timezone.utc).isoformat()
    # tik BBL (ne Pokal) ir tik reguliarusis sezonas (stage MAIN_ROUND, ne playoffs)
    due = sorted((k for k, v in cache.items() if v["c"] == "BBL" and v.get("s", "") in ("", "MAIN_ROUND")
                  and v["t"] and v["t"] < now),
                 key=lambda k: cache[k]["t"])
    todo = [k for k in due if ctx.wants(k)][: args.max_fetch]
    for gid in todo:
        g = seen.get(gid) or bbl_game(sess, gid)
        if not g:
            continue
        cache[gid]["t"] = g.get("scheduledTime") or cache[gid]["t"]
        cache[gid]["s"] = g.get("stage") or ""
        if g.get("stage") and g.get("stage") != "MAIN_ROUND":
            continue  # atkrintamosios – neįskaičiuojamos
        if g.get("gameProgress") != "E" and g.get("status") not in ("OFFICIAL", "FINISHED", "ENDED"):
            continue  # dar nesužaista / atidėta
        sp = (g.get("result") or {}).get("spectators")
        v = g.get("venue") or {}
        home = (g.get("homeTeam") or {}).get("name", "")
        away = (g.get("guestTeam") or {}).get("name", "")
        d = date_from_iso(g.get("scheduledTime") or "")
        ctx.put(gid, d, home, away, sp, f"{BBL}/spiele/{gid}", v.get("name", ""), capacity=v.get("capacity"))
        print(f"  {d} {home} – {away}: {sp} ({v.get('name', '')}, talpa {v.get('capacity')})")
        if gid not in seen:
            time.sleep(args.delay)
    if not any(to_int(m.get("attendance")) for m in ctx.matches.values()):
        print("BBL: oficialioje svetainėje nepavyko gauti lankomumo – failai nekeičiami", file=sys.stderr)
        return 1
    # Apsauga: jei oficialioje svetainėje rasta mažiau rungtynių su lankomumu nei turėjo Flashscore –
    # nieko nekeičiame (senieji duomenys lieka), kitą kartą bandoma iš naujo
    n_off = sum(1 for m in ctx.matches.values() if m.get("season") == args.season and to_int(m.get("attendance")))
    n_fs = sum(1 for m in fs_rows.values() if m.get("season") == args.season and to_int(m.get("attendance")))
    if n_off < n_fs:
        print(f"BBL: oficialioje svetainėje su lankomumu {n_off}, Flashscore – {n_fs}; failai nekeičiami", file=sys.stderr)
        write_status(ctx.dir, "BBL", SOURCES["BBL"], True, "",
                     [f"Oficialioje svetainėje kol kas rasta {n_off} rungtynių su lankomumu (Flashscore – {n_fs}), "
                      f"todėl rodomi ankstesni Flashscore duomenys"])
        STATUS["written"] = True
        return 0
    # Sąrašas įrašomas tik po sėkmingo paleidimo: jo buvimas reiškia, kad oficialus šaltinis veikia
    cache_path.write_text(json.dumps(cache, indent=0, sort_keys=True), encoding="utf-8")
    return ctx.finish()


# --------------------------------------------------------------------------- ABA lyga

def run_aba(args) -> int:
    ctx = Ctx("ABA", args.data_dir or "data/aba", args.season)
    site = "https://www.aba-liga.com"
    code = args.season_id or str(season_year(args.season) - 2000)
    sess = make_session(site + "/")
    soup = BeautifulSoup(get(sess, f"{site}/calendar/{code}/1/").text, "html.parser")
    played = []
    for tr in soup.select("table.league_calendar_table tbody tr"):
        # turo pavadinimas (ROUND 5, Top8 - R1, Play-out - R3, Play-in, Quarter-finals…): be atkrintamųjų
        panel = tr.find_parent("div", class_="panel")
        title = panel.select_one(".panel-title").get_text(" ", strip=True) if panel and panel.select_one(".panel-title") else ""
        if re.search(r"play-?in|playoff|play-off|quarter|semi|final|third|bronze", title, re.I):
            continue
        tds = tr.find_all("td")
        a = tr.find("a", href=re.compile(rf"/match/\d+/{code}/1/Overview/"))
        if len(tds) < 3 or not a:
            continue
        if not re.search(r"\d+\s*:\s*\d+", tds[1].get_text()):
            continue
        mid = re.search(r"/match/(\d+)/", a["href"]).group(1)
        href = a["href"] if a["href"].startswith("http") else site + a["href"]
        played.append((mid, href, date_from_dmy(tds[2].get_text())))
    print(f"ABA: sužaistų rungtynių {len(played)}")
    ctx.listed = len(played)
    todo = [p for p in played if ctx.wants(p[0])][: args.max_fetch]
    for mid, href, d in todo:
        ps = BeautifulSoup(get(sess, href).text, "html.parser")
        home = (ps.find("input", id="match_data_home") or {}).get("value", "")
        away = (ps.find("input", id="match_data_guest") or {}).get("value", "")
        box = ps.select_one(".dateAndVenue_container")
        att, venue = None, ""
        if box:
            icon = box.find("i", class_=re.compile("fa-users"))
            if icon:
                sib = icon.next_sibling
                att = to_int(sib if isinstance(sib, str) else (sib.get_text() if sib else ""))
            vm = re.search(r"Venue:\s*(.+)", box.get_text("\n"))
            venue = vm.group(1).strip() if vm else ""
            d = date_from_dmy(box.get_text()) or d
        ctx.put(mid, d, home, away, att, href, venue)
        print(f"  {d} {home} – {away}: {att} ({venue})")
        time.sleep(args.delay)
    return ctx.finish()


# --------------------------------------------------------------------------- Italija (LBA)

def run_ita(args) -> int:
    # LBA dažnai įrašo 0 žiūrovų – tokios rungtynės tikrinamos dar 3 savaites
    ctx = Ctx("LBA", args.data_dir or "data/ita", args.season, recheck_days=21)
    site = "https://www.legabasket.it"
    champ = args.season_id or "602"   # 602 = Regular Season 2026/27
    sess = make_session(site + "/")
    cal = lambda d: get(sess, f"{site}/api/championships/get-championships-calendar-by-id?id={champ}&d={d}",  # noqa: E731
                        expect_json=True).json()
    first = cal(1)
    ndays = len((first.get("filters") or {}).get("days") or []) or 40
    played = []
    for d in range(1, ndays + 1):
        j = first if d == 1 else cal(d)
        ms = j.get("matches") or []
        played += [m for m in ms if str(m.get("game_status")) == "2"]
        if ms and all(str(m.get("game_status")) == "0" for m in ms):
            break  # toliau – dar nežaistos dienos
        time.sleep(0.2)
    print(f"LBA: sužaistų rungtynių {len(played)}")
    ctx.listed = len(played)
    for m in [m for m in played if ctx.wants(m["id"])][: args.max_fetch]:
        det = (get(sess, f"{site}/api/championships/get-championships-matches-by-id?id={m['id']}",
                   expect_json=True).json().get("match") or {})
        d = date_from_iso(det.get("match_datetime") or m.get("match_datetime") or "")
        venue = det.get("plant_name") or m.get("plant_name") or ""
        ctx.put(m["id"], d, m["h_team_name"], m["v_team_name"], det.get("spectators"),
                f"{site}/game/{m['id']}", venue)
        print(f"  {d} {m['h_team_name']} – {m['v_team_name']}: {det.get('spectators')} ({venue})")
        time.sleep(args.delay)
    return ctx.finish()


# --------------------------------------------------------------------------- Lenkija (PLK + Flashscore)

PLK = "https://plk.pl"


def plk_game(sess, gid) -> dict | None:
    r = get(sess, f"{PLK}/api/webpage/game/{gid}")
    if r.status_code != 200:
        return None
    try:
        return r.json().get("game")
    except ValueError:
        return None


def plk_regular(g: dict) -> bool:
    r = ((g.get("round") or {}).get("name") or "").lower()
    return not r or "zasadnicz" in r


def tokens(s: str) -> set[str]:
    stop = {"mks", "ks", "wks", "bc", "kk", "sks", "basket", "energa", "pge", "enea", "start", "the"}
    return {t for t in norm(s).split() if len(t) >= 3 and t not in stop}


def run_pol(args) -> int:
    data_dir = Path(args.data_dir or "data/pol")
    ctx = Ctx("PLK", str(data_dir), args.season, recheck_days=5)
    sess = make_session(PLK + "/")
    cache_path = data_dir / "plk_games.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    html = get(sess, f"{PLK}/terminarz-i-wyniki").text.replace('\\"', '"')
    seeds = [(int(a), b) for a, b in re.findall(r'"id":"(\d{6})","seasonId":"(\d+)","league":\{"id":2,', html)]
    season_id = args.season_id or (str(max(int(b) for _, b in seeds)) if seeds else None)
    if not season_id:
        print("PLK: nepavyko nustatyti sezono", file=sys.stderr)
        return 1

    def probe(gid) -> bool:
        g = plk_game(sess, gid)
        time.sleep(0.15)
        if not g or str(g.get("seasonId")) != str(season_id) or (g.get("league") or {}).get("id") != 2:
            return False
        # tik „Runda Zasadnicza“ (reguliarusis sezonas); atkrintamosios pažymimos „x“ ir nebetikrinamos
        cache[str(gid)] = (g.get("dateLocal") or "")[:10] if plk_regular(g) else "x"
        return True

    ids = [s for s, b in seeds if b == season_id]
    if not cache and ids:
        for s in ids:
            probe(s)
        for step, start in ((-1, min(ids) - 1), (1, max(ids) + 1)):
            miss, gid = 0, start
            while miss < 30:
                miss = 0 if probe(gid) else miss + 1
                gid += step
    elif cache:
        miss, gid = 0, max(int(k) for k in cache) + 1
        while miss < 15:
            miss = 0 if probe(gid) else miss + 1
            gid += 1
    cache_path.write_text(json.dumps(cache, indent=0, sort_keys=True), encoding="utf-8")
    print(f"PLK: žinomų sezono rungtynių {len(cache)}")

    # Flashscore lankomumas (renka flashscore_attendance.py --league pol)
    fs = load_matches(data_dir / "flashscore" / "matches.csv")
    fs = [m for m in fs.values() if to_int(m.get("attendance"))]

    used = {m.get("source_url") for m in ctx.matches.values() if "flashscore" in (m.get("source_url") or "")}

    def fs_att(d: str, home: str, away: str):
        best, score = None, 0
        th, ta = tokens(home), tokens(away)
        for m in fs:
            try:
                dd = abs((datetime.strptime(m["date"], "%Y-%m-%d") - datetime.strptime(d, "%Y-%m-%d")).days)
            except ValueError:
                continue
            if dd > 1 or m.get("source_url") in used:
                continue
            fh, fa = tokens(m.get("home", "")), tokens(m.get("away", ""))
            sc = (2 if th & fh else 0) + (1 if ta & fa else 0)
            if sc > score:
                best, score = m, sc
        if best and score >= 3:   # sutampa ir šeimininkai, ir svečiai
            used.add(best.get("source_url"))
            return best["attendance"], best["source_url"]
        return None, ""

    td = today()
    for gid in sorted((k for k, d in cache.items() if d and d <= td), key=lambda k: cache[k]):
        prev = ctx.matches.get(gid)
        if prev and prev.get("attendance"):
            continue
        if prev and prev.get("home"):   # arena jau žinoma – tik ieškome lankomumo Flashscore
            att, src = fs_att(prev["date"], prev["home"], prev["away"])
            if att:
                ctx.put(gid, prev["date"], prev["home"], prev["away"], att, src, prev.get("arena", ""),
                        count_attempt=False)
            continue
        g = plk_game(sess, gid)
        time.sleep(args.delay)
        if not g or not g.get("isFinished"):
            continue
        if not plk_regular(g):
            cache[gid] = "x"
            continue
        d = (g.get("dateLocal") or "")[:10]
        home, away = g.get("homeTeamName", ""), g.get("guestTeamName", "")
        att, src = fs_att(d, home, away)
        ctx.put(gid, d, home, away, att, src or f"{PLK}/mecz/{gid}/{g.get('slug', '')}",
                (g.get("venue") or {}).get("name", ""))
        print(f"  {d} {home} – {away}: {att} ({(g.get('venue') or {}).get('name', '')})")
    return ctx.finish()


# --------------------------------------------------------------------------- Graikija (esake.gr + Flashscore)

ESAKE = "https://www.esake.gr"
GR = {"α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i", "θ": "th", "ι": "i", "κ": "k",
      "λ": "l", "μ": "m", "ν": "n", "ξ": "x", "ο": "o", "π": "p", "ρ": "r", "σ": "s", "ς": "s", "τ": "t",
      "υ": "y", "φ": "f", "χ": "ch", "ψ": "ps", "ω": "o"}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def greeklish(s: str) -> str:
    s = unicodedata.normalize("NFD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    for a, b in (("ευ", "ef"), ("αυ", "af"), ("ου", "ou"), ("γκ", "g"), ("ντ", "d")):
        s = s.replace(a, b)
    return "".join(GR.get(c, c) for c in s)


GR_STOP = {"betsson", "bc", "elabet", "aktor", "cola", "hotels", "collection", "as", "bk", "ke", "basket", "athens",
           "rhodes", "iaponiki", "patras", "patron", "doxa", "kae"}


def gr_tokens(s: str) -> list[str]:
    t = [t for t in re.sub(r"[^a-z0-9 ]+", " ", greeklish(s)).split() if len(t) >= 3 and t not in GR_STOP]
    return [x for x in t if x != "vikos"] if "promitheas" in t else t   # „ΒΙΚΟΣ COLA“ – Promitheo rėmėjas


def gr_same(a: str, b: str) -> bool:
    A, B = gr_tokens(a), gr_tokens(b)
    return any(x[:4] == y[:4] for x in A for y in B)


def run_gre(args) -> int:
    data_dir = Path(args.data_dir or "data/gre")
    ctx = Ctx("GBL", str(data_dir), args.season)
    sess = make_session(ESAKE + "/")
    yr = season_year(args.season)
    first = get(sess, f"{ESAKE}/en/action/EsakeResults").text
    champ = args.season_id
    if not champ:
        opts = re.findall(r'<option value="([0-9A-F]{8})"[^>]*>([^<]+)</option>', first)
        # tik lygos sezonas (pvz. „Stoiximan GBL 2026-2027“), ne taurė
        champ = next((v for v, t in opts if f"{yr}-{yr + 1}" in t and re.search(r"GBL|League", t, re.I)
                      and not re.search(r"cup|κύπελλο|kypello", t, re.I)), None)
    if not champ:
        print("GBL: nerastas sezono ID", file=sys.stderr)
        return 1
    soup = BeautifulSoup(get(sess, f"{ESAKE}/en/action/EsakeResults?idchampionship={champ}").text, "html.parser")
    series = list(dict.fromkeys(o.get("value") for o in soup.select("#series option") if o.get("value"))) \
        or [f"{i:02d}" for i in range(1, 27)]

    games = []
    for sr in series:
        ps = BeautifulSoup(get(sess, f"{ESAKE}/en/action/EsakeResults?idchampionship={champ}&idteam=&idseason=00000001&series={sr}").text,
                           "html.parser")
        found = 0
        for g in ps.select(".esake-program-game"):
            m = re.search(r"idgame=([0-9A-F]+)", str(g))
            info = [i.get_text(" ", strip=True) for i in g.select(".esake-program-game-info")]
            spans = []
            for sp in g.select(".esake-program-game-col-2 span"):
                for br in sp.find_all("br"):
                    br.replace_with(" ")
                spans.append(re.sub(r"\s+", " ", sp.get_text(" ")).strip())
            if not m or len(spans) < 3:
                continue
            found += 1
            if not re.search(r"\d+\s*-\s*\d+", spans[1]):
                continue  # dar nesužaista
            dm = re.search(r"(\d{1,2})\s+([A-Za-z]{3})", info[0] if info else "")
            d = ""
            if dm and dm.group(2).lower() in MONTHS:
                mo = MONTHS[dm.group(2).lower()]
                d = f"{yr + (0 if mo >= 8 else 1):04d}-{mo:02d}-{int(dm.group(1)):02d}"
            arena = info[1] if len(info) > 1 else ""
            closed = bool(re.search(r"ΚΕΚΛΕΙΣΜΕΝΩΝ", arena))
            arena = re.sub(r"\s*-?\s*ΚΕΚΛΕΙΣΜΕΝΩΝ.*$", "", arena).strip()
            games.append({"id": m.group(1), "date": d, "home": spans[0], "away": spans[2], "arena": arena, "closed": closed})
        if not found:
            break
        time.sleep(0.2)
    print(f"GBL: sužaistų rungtynių {len(games)}")
    ctx.listed = len(games)

    fs = list(load_matches(data_dir / "flashscore" / "matches.csv").values())

    def fs_match(g):
        best, score = None, 0
        for m in fs:
            try:
                dd = abs((datetime.strptime(m["date"], "%Y-%m-%d") - datetime.strptime(g["date"], "%Y-%m-%d")).days)
            except ValueError:
                continue
            if dd > 1:
                continue
            sc = (2 if gr_same(g["home"], m.get("home", "")) else 0) + (1 if gr_same(g["away"], m.get("away", "")) else 0)
            if sc > score:
                best, score = m, sc
        return best if score >= 3 else None

    # Graikiški pavadinimai -> Flashscore (lotyniški) – kad klubo vardas būtų vienodas visose rungtynėse
    names = {}
    pairs = [(g, fs_match(g)) for g in games]
    for g, m in pairs:
        if m:
            names.setdefault(g["home"], m["home"])
            names.setdefault(g["away"], m["away"])
    nice = lambda n: names.get(n) or " ".join(w.capitalize() for w in greeklish(n).split())  # noqa: E731
    for g, m in pairs:
        att = None if g["closed"] else (m.get("attendance") if m else None)
        src = (m or {}).get("source_url") or f"{ESAKE}/en/action/EsakegameView?idgame={g['id']}&mode=3"
        prev = ctx.matches.get(g["id"], {})
        if prev.get("attendance") and not att:
            continue
        ctx.put(g["id"], g["date"], nice(g["home"]), nice(g["away"]), att, src, g["arena"], count_attempt=False)
    return ctx.finish()


# --------------------------------------------------------------------------- paleidimas

DIRS = {"aus": "data/nbl", "fin": "data/fin", "lvee": "data/lvee", "bbl": "data/bbl",
        "aba": "data/aba", "ita": "data/ita", "pol": "data/pol", "gre": "data/gre"}
NAMES = {"aus": "NBL", "fin": "Korisliiga", "lvee": "LEBL", "bbl": "BBL",
         "aba": "ABA", "ita": "LBA", "pol": "PLK", "gre": "GBL"}
SOURCES = {"NBL": "nbl.com.au", "Korisliiga": "basket.fi (Sportradar)", "LEBL": "estlatbl.com + FIBA LiveStats",
           "BBL": "easycredit-bbl.de", "ABA": "aba-liga.com", "LBA": "legabasket.it",
           "PLK": "plk.pl + Flashscore", "GBL": "esake.gr + Flashscore"}

LEAGUES = {
    "aus": run_aus, "fin": run_fin, "lvee": run_lvee, "bbl": run_bbl,
    "aba": run_aba, "ita": run_ita, "pol": run_pol, "gre": run_gre,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--league", required=True, help="aus, fin, lvee, bbl, aba, ita, pol, gre arba all")
    ap.add_argument("--season", default="2026-27")
    ap.add_argument("--data-dir", default=None, help="pakeisti numatytąjį aplanką")
    ap.add_argument("--season-id", default=None, help="lygos sezono ID (jei automatiškai nerandamas)")
    ap.add_argument("--max-fetch", type=int, default=150)
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--recompute-only", action="store_true")
    args = ap.parse_args()

    keys = list(LEAGUES) if args.league == "all" else [args.league]
    rc = 0
    for k in keys:
        if k not in LEAGUES:
            print(f"Nežinoma lyga: {k}", file=sys.stderr)
            return 2
        print(f"=== {k} · {args.season}")
        try:
            if args.recompute_only:
                c = Ctx(NAMES[k], args.data_dir or DIRS[k], args.season)
                save_matches(c.path, c.matches)
                rows, unresolved = compute_averages(c.matches, c.season, c.league)
                write_outputs(c.dir, rows, unresolved, c.season, c.league)
                continue
            BLOCK["hits"], BLOCK["last"] = 0, ""
            STATUS["written"] = False
            r = LEAGUES[k](args)
            if r and not STATUS["written"]:
                write_status(args.data_dir or DIRS[k], NAMES[k], SOURCES[NAMES[k]], False,
                             "nepavyko nuskaityti lygos svetainės" + (f" ({BLOCK['last']})" if BLOCK["last"] else "")
                             + " – žr. GitHub Actions žurnalą")
            rc = max(rc, r)
        except Exception as e:  # noqa: BLE001
            print(f"{k}: KLAIDA {e}", file=sys.stderr)
            if not args.recompute_only:
                msg = re.sub(r"https?://\S+?:\s*", "", str(e))
                if re.search(r"ConnectionPool|Max retries|timed out|Timeout|Connection (reset|refused|aborted)", msg):
                    msg = "svetainė nepasiekiama arba neatsako"
                msg = msg[:200]
                err = (f"svetainė atsisakė duoti duomenis ({BLOCK['last']}) – greičiausiai blokuoja skaitymą"
                       if BLOCK["hits"] else f"klaida skaitant svetainę: {msg}")
                write_status(args.data_dir or DIRS[k], NAMES[k], SOURCES[NAMES[k]], False, err)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
