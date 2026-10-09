#!/usr/bin/env python3
"""
ACB (Liga Endesa) lankomumo rinkėjas.

Ką daro:
  1. Suranda sezono rungtynių nuorodas acb.com kalendoriuje.
  2. Iš kiekvienų rungtynių puslapio ištraukia šeimininkus, svečius, datą ir "Público".
  3. Saugo viską data/matches.csv (be dublikatų, pagal match_id).
  4. Perskaičiuoja namų lankomumo vidurkius kiekvienam klubui -> data/attendance_averages.json/.csv

Naudojimas:
  pip install requests beautifulsoup4
  python acb_attendance.py --temporada 91 --season 2026-27
  python acb_attendance.py --recompute-only          # tik perskaičiuoti vidurkius

SVARBU: HTML struktūra (nuorodų šablonas, "Público" laukelis) NEPATIKRINTA su gyvu
acb.com puslapiu. Jei kas nors neranda, paleiskite su --save-html, pažiūrėkite
data/raw/*.html ir pataisykite konstantas GAME_LINK_RE / parse_game.
"""
import argparse
import csv
import json
import re
import sys
import time
import unicodedata
import urllib.robotparser
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

LEAGUE = "ACB"
BASE = "https://www.acb.com"
GAME_BASE = "https://live.acb.com"
CALENDAR_URL = BASE + "/es/calendario?temporada={temporada}"

# --- Šias konstantas gali tekti pataisyti pagal tikrą puslapį -----------------
# Patikrinta pagal gyvą kalendorių: https://live.acb.com/partidos/<slug>-<id>/resumen (arba /estadisticas)
GAME_PATH_RE = re.compile(r"/partidos/([a-z0-9-]+?)-(\d{4,})(?:/|$)")
RAW_GAME_RE = re.compile(r"/partidos/([a-z0-9-]+?)-(\d{4,})/(?:resumen|estadisticas)")
# "Público: 15.068" arba "Público 15068"
ATTENDANCE_RE = re.compile(r"P[úu]blico\s*:?\s*(\d{1,3}(?:[.,]\d{3})+|\d+)", re.I)
MAX_PLAUSIBLE_ATTENDANCE = 30000
# -----------------------------------------------------------------------------

UA = "Mozilla/5.0 (compatible; basketball-finance-attendance-bot/0.1)"
MATCH_FIELDS = ["league", "season", "match_id", "date", "home", "away",
                "attendance", "source_url", "checked_at", "attempts"]


# ----------------------------- pagalbinės funkcijos --------------------------
def norm(name: str) -> str:
    """Pavadinimo normalizavimas aliasų paieškai: be akcentų, mažosios, be skyrybos."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def parse_attendance(text: str):
    m = ATTENDANCE_RE.search(text)
    if not m:
        return None
    n = int(re.sub(r"[.,]", "", m.group(1)))
    return n if 0 < n < MAX_PLAUSIBLE_ATTENDANCE else None


def _jsonld_event(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("homeTeam") and item.get("awayTeam"):
                return item
    return None


def parse_game(html: str, slug_teams=None):
    """Grąžina dict(home, away, date, attendance) arba None, jei nepavyko rasti komandų.
    slug_teams = (home, away) iš nuorodos – patikimesnis už puslapio antraštę."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    home = away = date = None
    if slug_teams:
        home, away = slug_teams

    ev = _jsonld_event(soup) if not (home and away) else None  # 1 strategija: JSON-LD SportsEvent
    if ev:
        get = lambda t: t.get("name") if isinstance(t, dict) else str(t)
        home, away = get(ev["homeTeam"]), get(ev["awayTeam"])
        date = (ev.get("startDate") or "")[:10] or None

    if not (home and away):  # 2 strategija: <title>/og:title "Šeimininkai - Svečiai | ..."
        og = soup.find("meta", property="og:title")
        title = (og.get("content") if og else None) or (soup.title.string if soup.title else "")
        title = (title or "").split("|")[0]
        parts = re.split(r"\s+(?:-|–|—|vs\.?)\s+", title.strip())
        if len(parts) >= 2:
            home, away = parts[0].strip(), parts[1].strip()

    if not date:
        t = soup.find("time", attrs={"datetime": True})
        if t:
            date = t["datetime"][:10]
        else:
            m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
            if m:
                date = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"

    if not (home and away):
        return None
    return {"home": home, "away": away, "date": date or "",
            "attendance": parse_attendance(text)}


# ----------------------------- saugykla --------------------------------------
def load_matches(path: Path):
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {r["match_id"]: r for r in csv.DictReader(f)}


def save_matches(path: Path, matches: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MATCH_FIELDS)
        w.writeheader()
        for mid in sorted(matches, key=lambda x: int(x) if x.isdigit() else 0):
            w.writerow({k: matches[mid].get(k, "") for k in MATCH_FIELDS})


def load_aliases(path: Path):
    """team_aliases.csv: alias,club_name  (club_name = jūsų lentelės klubo pavadinimas)."""
    aliases = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("alias") and r.get("club_name"):
                    aliases[norm(r["alias"])] = r["club_name"].strip()
    return aliases


def resolve_club(raw: str, aliases: dict, unresolved: set):
    key = norm(raw)
    if key in aliases:
        return aliases[key]
    unresolved.add(raw)
    return raw  # kol nėra aliaso, naudojam šaltinio pavadinimą


# ----------------------------- vidurkiai -------------------------------------
def compute_averages(matches: dict, aliases: dict, season: str):
    """Skaičiuoja namų lankomumo vidurkius. Įtraukia VISUS sezono šeimininkus,
    net jei lankomumas dar neįrašytas (average=None, home_games_counted=0)."""
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
                "average": None,
                "total": 0, "max": None, "min": None,
            })
    # Su lankomumu – pagal vidurkį desc; be – gale, alfabetu
    rows.sort(key=lambda r: (-(r["average"] if r["average"] is not None else -1), r["club"]))
    return rows, unresolved


def write_outputs(data_dir: Path, rows, unresolved, season):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (data_dir / "attendance_averages.json").write_text(
        json.dumps({"league": LEAGUE, "season": season, "updated": now, "clubs": rows},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    if rows:
        fields = list(rows[0].keys())
        with (data_dir / "attendance_averages.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow({k: ("" if r.get(k) is None else r[k]) for k in fields})
    if unresolved:
        (data_dir / "unresolved_teams.txt").write_text(
            "\n".join(sorted(unresolved)) + "\n", encoding="utf-8")
        print(f"[!] {len(unresolved)} pavadinimų be aliaso -> data/unresolved_teams.txt "
              f"(įrašykite į data/team_aliases.csv)", file=sys.stderr)


# ----------------------------- tinklas ---------------------------------------
class Fetcher:
    def __init__(self, delay: float, raw_dir: Path = None):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.delay, self.raw_dir, self._last = delay, raw_dir, 0.0
        self._robots = {}

    def allowed(self, url):
        p = urlparse(url)
        host = f"{p.scheme}://{p.netloc}"
        if host not in self._robots:
            rp = urllib.robotparser.RobotFileParser(host + "/robots.txt")
            try:
                rp.read()
            except Exception as e:  # noqa
                print(f"[!] {host}/robots.txt nepavyko nuskaityti ({e}); tęsiu atsargiai", file=sys.stderr)
                rp = None
            self._robots[host] = rp
        rp = self._robots[host]
        return rp is None or rp.can_fetch(UA, url)

    def get(self, url, tag=None):
        if not self.allowed(url):
            print(f"[!] robots.txt draudžia: {url}", file=sys.stderr)
            return None
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


def slug_to_teams(slug: str):
    """'real-madrid-vs-unicaja' -> ('Real Madrid', 'Unicaja'); šeimininkai rašomi pirmi."""
    if "-vs-" not in slug:
        return None
    h, a = slug.split("-vs-", 1)
    title = lambda x: " ".join(w.capitalize() for w in x.split("-"))
    return title(h), title(a)


def add_game(games: dict, slug: str, mid: str):
    games.setdefault(mid, {
        "slug": slug,
        "teams": slug_to_teams(slug),
        "url": f"{GAME_BASE}/partidos/{slug}-{mid}/resumen",
        "alt_url": f"{GAME_BASE}/partidos/{slug}-{mid}/estadisticas",
    })


def discover_games(fx: Fetcher, temporada: str, max_pages: int = 40):
    """Grąžina {match_id: {...}}. Eina per kalendorių ir per jornadų nuorodas, jei tokių yra."""
    start = CALENDAR_URL.format(temporada=temporada)
    queue, seen, games = [start], set(), {}
    while queue and len(seen) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        html = fx.get(url, tag=f"calendar_{len(seen)}")
        if not html:
            continue
        for m in RAW_GAME_RE.finditer(html):       # nuorodos ir JSON viduje
            add_game(games, m.group(1), m.group(2))
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"])
            p = urlparse(href)
            m = GAME_PATH_RE.search(p.path)
            if m and p.netloc.endswith("acb.com"):
                add_game(games, m.group(1), m.group(2))
            elif ("jornada=" in href and f"temporada={temporada}" in href
                  and p.netloc.endswith("acb.com")):
                queue.append(href)
    if not games:
        print_diagnostics(fx, start)
    return games


def print_diagnostics(fx: Fetcher, url: str):
    """Kai rungtynių nerasta – parodo, ką scriptas mato kalendoriaus puslapyje."""
    html = fx.get(url)
    print("=== DIAGNOSTIKA (rungtynių nerasta) ===")
    if not html:
        print("Puslapis neatsisiuntė (žr. [!] eilutes aukščiau).")
        return
    soup = BeautifulSoup(html, "html.parser")
    hrefs = [a["href"] for a in soup.find_all("a", href=True)]
    print(f"URL: {url}")
    print(f"HTML ilgis: {len(html)}; <a> nuorodų: {len(hrefs)}; <script>: {len(soup.find_all('script'))}")
    interesting = [h for h in hrefs if re.search(r"partido|jornada|calendario|resultado|match|game", h, re.I)]
    print(f"Įdomios nuorodos ({len(interesting)}), pirmos 15:")
    for h in interesting[:15]:
        print("  ", h)
    print("=== DIAGNOSTIKOS PABAIGA ===")


def game_diagnostics(html: str, url: str):
    """Parodo, kodėl rungtynių puslapyje nerastas lankomumas (pirmoms kelioms rungtynėms)."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    low = text.lower()
    print(f"--- RUNGTYNIŲ DIAGNOSTIKA: {url}")
    print(f"HTML ilgis: {len(html)}; teksto ilgis: {len(text)}; <script>: {len(soup.find_all('script'))}; "
          f"__NEXT_DATA__: {'__NEXT_DATA__' in html}")
    for key in ("público", "publico", "espectadores", "pabell", "asistencia"):
        i = low.find(key)
        if i >= 0:
            print(f"'{key}' tekste: ...{text[max(0, i - 80): i + 120]}...")
    if not any(k in low for k in ("público", "publico", "espectadores", "asistencia")):
        print("Raktažodžių apie lankomumą tekste nėra. Pradžia:", text[:300])
    print("--- PABAIGA")


def needs_fetch(rec, now):
    if rec is None:
        return True
    if rec.get("attendance"):
        return False
    attempts = int(rec.get("attempts") or 0)
    date = rec.get("date")
    if date and attempts >= 4:  # seniai sužaistos rungtynės be skaičiaus – nebetikrinam
        try:
            if now.date() - datetime.fromisoformat(date).date() > timedelta(days=21):
                return False
        except ValueError:
            pass
    last = rec.get("checked_at")
    if not last:
        return True
    hours = 6 if attempts < 4 else 24
    return now - datetime.fromisoformat(last) > timedelta(hours=hours)


def update_matches(fx, games, matches, season, max_fetch=60, max_miss_streak=10):
    """Eina rungtynes didėjančia ID tvarka. Sustoja, kai iš eilės max_miss_streak rungtynių
    neturi lankomumo (greičiausiai dar nesužaistos)."""
    now = datetime.now(timezone.utc)
    todo = [m for m in sorted(games, key=int) if needs_fetch(matches.get(m), now)]
    print(f"Reikia tikrinti: {len(todo)} (šįkart ne daugiau {max_fetch})")
    done = streak = diags = 0
    for mid in todo:
        if done >= max_fetch or streak >= max_miss_streak:
            break
        g = games[mid]
        html = fx.get(g["url"], tag=f"game_{mid}")
        done += 1
        if not html:  # pvz. 404: užfiksuojam bandymą, kad nebandytume kiekvieną paleidimą
            if g["teams"]:
                prev = matches.get(mid, {})
                matches[mid] = {
                    "league": LEAGUE, "season": season, "match_id": mid,
                    "date": prev.get("date", ""), "home": g["teams"][0], "away": g["teams"][1],
                    "attendance": "", "source_url": g["url"],
                    "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "attempts": int(prev.get("attempts") or 0) + 1,
                }
            continue
        info = parse_game(html, g["teams"])
        if not info:
            print(f"[!] nepavyko perskaityti rungtynių {mid}: {g['url']}", file=sys.stderr)
            continue
        if not info["attendance"]:
            html2 = fx.get(g["alt_url"], tag=f"game_{mid}_stats")
            if html2:
                info["attendance"] = parse_attendance(BeautifulSoup(html2, "html.parser").get_text(" ", strip=True))
                if not info["date"]:
                    info["date"] = (parse_game(html2, g["teams"]) or {}).get("date", "")
        if info["attendance"]:
            streak = 0
        else:
            streak += 1
            if diags < 2:
                game_diagnostics(html, g["url"])
                diags += 1
        prev = matches.get(mid, {})
        matches[mid] = {
            "league": LEAGUE, "season": season, "match_id": mid,
            "date": info["date"], "home": info["home"], "away": info["away"],
            "attendance": info["attendance"] or "", "source_url": g["url"],
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "attempts": int(prev.get("attempts") or 0) + 1,
        }
    got = sum(1 for m in matches.values() if m.get("attendance"))
    print(f"Patikrinta šįkart: {done}; rungtynių su lankomumu iš viso: {got}")


# ----------------------------- main ------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--temporada", help="acb.com sezono ID (calendario?temporada=N)")
    ap.add_argument("--season", default="2026-27", help="sezono žymė, pvz. 2026-27")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--max-fetch", type=int, default=60, help="maks. rungtynių puslapių per paleidimą")
    ap.add_argument("--delay", type=float, default=1.5, help="sekundės tarp užklausų")
    ap.add_argument("--save-html", action="store_true", help="išsaugoti gautą HTML į data/raw/ derinimui")
    ap.add_argument("--recompute-only", action="store_true")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    matches_path = data_dir / "matches.csv"
    matches = load_matches(matches_path)
    aliases = load_aliases(data_dir / "team_aliases.csv")

    if not args.recompute_only:
        if not args.temporada:
            ap.error("reikia --temporada (arba --recompute-only)")
        fx = Fetcher(args.delay, data_dir / "raw" if args.save_html else None)
        games = discover_games(fx, args.temporada)
        print(f"Rasta rungtynių nuorodų: {len(games)}")
        update_matches(fx, games, matches, args.season, args.max_fetch)
        save_matches(matches_path, matches)

    rows, unresolved = compute_averages(matches, aliases, args.season)
    write_outputs(data_dir, rows, unresolved, args.season)
    print(f"Klubų su lankomumu: {len(rows)}")
    for r in rows:
        print(f"  {r['club']:<32} {r['average']:>6}  ({r['home_games_counted']} namų rungt.)")


if __name__ == "__main__":
    main()
