#!/usr/bin/env python3
"""
Kiekvienų ACB ir LKL rungtynių arena – kad talpa būtų skaičiuojama pagal tikrą areną,
kaip ir kitose lygose. Esamų ACB/LKL rinkiklių (acb_attendance.py, lkl_attendance.py) NEKEIČIA:
jų matches.csv tik skaitomas, o arenos įrašomos į atskirą failą.

  ACB: live.acb.com rungtynių „estadisticas“ puslapis – laukelis „Pabellón“
       -> data/game_arenas.json            {match_id: arena}
  LKL: lkl.lt/rungtynes/<id> – antraštėje „data · arena · N žiūrovai“
       -> data/lkl/lkl_games.json          (lkl.lt rungtynių sąrašas, kad nereikėtų skenuoti iš naujo)
       -> data/lkl/game_arenas.json        {Flashscore match_id: arena}  (sujungta pagal datą ir komandas)

capacities.py šiuos failus naudoja, kai matches.csv neturi „arena“ stulpelio.

Naudojimas:  python game_arenas.py [--season 2026-27] [--only acb|lkl]
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from run_status import write_status

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
LT_MONTHS = {"sausio": 1, "vasario": 2, "kovo": 3, "balandzio": 4, "geguzes": 5, "birzelio": 6, "liepos": 7,
             "rugpjucio": 8, "rugsejo": 9, "spalio": 10, "lapkricio": 11, "gruodzio": 12}
LT_ALIAS = {"hipocredit": "jonava", "jonavos": "jonava"}
CHALLENGE = re.compile(r"cf-chl|challenge-platform|Just a moment\.\.\.|captcha|Access Denied", re.I)
BLOCK = {"hits": 0, "last": ""}


def blocked(r) -> bool:
    if r.status_code in (403, 429, 503) or CHALLENGE.search(r.text[:3000]):
        BLOCK["hits"] += 1
        BLOCK["last"] = f"HTTP {r.status_code}"
        return True
    return False


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s)).strip()


def toks(s: str) -> list[str]:
    out = []
    for t in norm(s).split():
        t = LT_ALIAS.get(t, t)
        if len(t) >= 3 and t not in {"bc", "kk", "klubas", "paskolu", "kaunas", "vilnius"}:
            out.append(t)
    return out


def same(a: str, b: str) -> bool:
    return any(x[:5] == y[:5] for x in toks(a) for y in toks(b))


def read_csv(p: Path) -> list[dict]:
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def save_json(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9,lt;q=0.8,en;q=0.7"})
    return s


# --------------------------------------------------------------------------- ACB

def run_acb(season: str, root: Path, max_fetch: int) -> int:
    path = root / "data" / "game_arenas.json"
    cache = load_json(path, {})
    rows = [r for r in read_csv(root / "data" / "matches.csv")
            if r.get("season") == season and r.get("attendance") and r.get("match_id") not in cache]
    s = session()
    got = tried = 0
    BLOCK["hits"] = 0
    for r in rows[:min(max_fetch, 40)]:
        if BLOCK["hits"]:
            break   # svetainė ėmė blokuoti – daugiau šį kartą nebandome
        url = (r.get("source_url") or "").replace("/resumen", "/estadisticas")
        if "live.acb.com" not in url:
            continue
        if "/es/" not in url:
            url = url.replace("live.acb.com/", "live.acb.com/es/")
        tried += 1
        try:
            resp = s.get(url, timeout=30)
            blocked(resp)
            h = resp.text
        except Exception as e:  # noqa: BLE001
            print(f"  ACB {r['match_id']}: {e}", file=sys.stderr)
            continue
        m = re.search(r"Pabell[oó]n</p>\s*<p[^>]*>([^<]+)</p>", h)
        if m:
            cache[r["match_id"]] = re.sub(r"\s+", " ", m.group(1)).strip()
            got += 1
        time.sleep(2 + 2 * (time.time() % 1))
    save_json(path, cache)
    print(f"ACB: arenų žinoma {len(cache)} (nauja {got})")
    err = ""
    if BLOCK["hits"] and not got:
        err = f"live.acb.com atsisakė duoti puslapį ({BLOCK['last']}) – greičiausiai blokuoja"
    elif tried >= 3 and not got:
        err = (f"live.acb.com neatidavė arenos nė vienoms iš {tried} rungtynių"
               + (f" ({BLOCK['last']}) – greičiausiai blokuoja" if BLOCK["hits"] else " – pasikeitė puslapis arba blokuoja"))
    write_status(root / "data", "ACB", "live.acb.com (arenos)", not err, err)
    return 1 if err else 0


# --------------------------------------------------------------------------- LKL

def lkl_game_html(h: str) -> dict | None:
    title = re.sub(r"\s*-\s*LKL\.LT\s*$", "", (re.search(r"<title>([^<]+)", h) or [None, ""])[1]).strip()
    m = re.search(r"(\d{4})\s+m\.\s+([^\s<]+)\s+(\d+)\s+d\.,\s*[\d:]+\s*<span>·</span>([\s\S]{0,400}?)</div>", h)
    if not m or " - " not in title:
        return None
    mo = LT_MONTHS.get(norm(m.group(2)))
    if not mo:
        return None
    parts = [re.sub(r"<[^>]+>", "", p).strip() for p in re.split(r"<span>·</span>", m.group(4))]
    att = re.search(r"(\d+)\s*žiūrov", parts[1] if len(parts) > 1 else "")
    home, away = title.split(" - ", 1)
    return {"date": f"{int(m.group(1)):04d}-{mo:02d}-{int(m.group(3)):02d}", "home": home.strip(), "away": away.strip(),
            "arena": re.sub(r"\s+", " ", parts[0]).strip() if parts else "", "att": int(att.group(1)) if att else None}


def run_lkl(season: str, root: Path, max_fetch: int) -> int:
    """Mažai užklausų, kad lkl.lt nepalaikytų robotu (anksčiau dėl to lankomumas perkeltas į Flashscore):
    - sąrašas imamas iš lkl.lt/rezultatai (tik sužaistos rungtynės, 1 puslapis per paleidimą;
      pirmą kartą – visi sezono puslapiai);
    - kiekvienų rungtynių puslapis atidaromas vieną kartą (pakartotinai – tik jei trūko arenos,
      ne dažniau kaip kas 12 val., iki 3 kartų);
    - ne daugiau 12 puslapių per paleidimą, 3–6 s pertrauka tarp jų;
    - jei svetainė atsako „uždrausta“ / patikra prieš robotus – iškart sustojama ir 3 d. nebandoma
      (tuo metu naudojama klubo arena iš lapo „Arenas“)."""
    import random
    ddir = root / "data" / "lkl"
    games = load_json(ddir / "lkl_games.json", {})
    meta = games.pop("_meta", {}) if isinstance(games.get("_meta"), dict) else {}
    start = f"{int(season[:4]):04d}-08-01"
    now = datetime.now(timezone.utc)
    s = session()
    s.headers["Referer"] = "https://lkl.lt/rezultatai"
    BLOCK["hits"] = 0
    err = ""
    opened = 0

    def pause() -> None:
        meta["paused_until"] = (now + timedelta(days=3)).isoformat(timespec="seconds")

    def get_page(url: str):
        r = s.get(url, timeout=30)
        if blocked(r):
            pause()
            raise PermissionError(f"lkl.lt atsisakė duoti puslapį ({BLOCK['last']}) – greičiausiai laiko robotu; "
                                  f"kitas bandymas po 3 d.")
        return r

    paused = meta.get("paused_until", "")
    skipped = bool(paused and paused > now.isoformat())
    if skipped:
        print(f"LKL: lkl.lt skaitymas sustabdytas iki {paused} (buvo blokuota)")
        err = f"lkl.lt buvo užblokavęs skaitymą – pauzė iki {paused[:10]}; arenos imamos iš lapo „Arenas“ (klubo arena)"
    else:
        try:
            first_run = not games
            ids: list[str] = []
            for page in range(1, 9 if first_run else 2):
                r = get_page(f"https://lkl.lt/rezultatai?page={page}")
                if r.status_code != 200:
                    break
                found = [x for x in dict.fromkeys(re.findall(r"/rungtynes/(\d+)", r.text)) if x not in ids]
                if not found:
                    break
                ids += found
                time.sleep(random.uniform(3, 6))
            if not ids:
                err = "lkl.lt/rezultatai nerasta nė vienų rungtynių – puslapis pasikeitė arba blokuoja"
            for gid in ids:
                games.setdefault(gid, {})       # įsimename – kiti paleidimai atidarys likusias
            todo = []
            for gid in sorted(games, key=lambda k: -int(k) if k.isdigit() else 0):
                g = games.get(gid)
                if g and g.get("arena") and g.get("att"):
                    continue
                if g and (g.get("tries", 0) >= 3 or g.get("last_try", "") > (now - timedelta(hours=12)).isoformat()):
                    continue
                todo.append(gid)
            for gid in todo[:min(max_fetch, 12)]:
                opened += 1
                r = get_page(f"https://lkl.lt/rungtynes/{gid}")
                g = lkl_game_html(r.text) if r.status_code == 200 else None
                prev = games.get(gid, {})
                if g and g["date"] >= start:
                    games[gid] = dict(g, tries=prev.get("tries", 0) + 1, last_try=now.isoformat(timespec="seconds"))
                elif g is None:
                    games[gid] = dict(prev, tries=prev.get("tries", 0) + 1, last_try=now.isoformat(timespec="seconds"))
                time.sleep(random.uniform(3, 6))
            if opened >= 3 and not any(games.get(g, {}).get("arena") for g in todo[:opened]):
                err = f"lkl.lt atidaryta {opened} rungtynių puslapių, bet arenos nerasta – puslapis pasikeitė"
            meta.pop("paused_until", None)
        except PermissionError as e:
            err = str(e)
        except Exception as e:  # noqa: BLE001
            m = str(e)
            err = ("lkl.lt nepasiekiama arba neatsako" if re.search(r"ConnectionPool|Max retries|timed out|Timeout", m)
                   else f"lkl.lt klaida: {m[:150]}")
    print(f"LKL: lkl.lt puslapių atidaryta {opened}" + (f"; {err}" if err else ""))
    save_json(ddir / "lkl_games.json", dict(games, _meta=meta) if meta else games)

    # sujungiame su Flashscore rungtynėmis (data/lkl/matches.csv)
    out = {}
    for r in read_csv(ddir / "matches.csv"):
        if r.get("season") != season or not r.get("date"):
            continue
        try:
            d0 = datetime.strptime(r["date"], "%Y-%m-%d")
        except ValueError:
            continue
        best, score = None, 0
        for g in games.values():
            if not g.get("date"):
                continue
            if abs((datetime.strptime(g["date"], "%Y-%m-%d") - d0).days) > 1:
                continue
            sc = (2 if same(r.get("home", ""), g["home"]) else 0) + (1 if same(r.get("away", ""), g["away"]) else 0)
            if sc > score:
                best, score = g, sc
        # abi komandos turi sutapti – lkl.lt/rezultatai rodo ir Citadele KMT taurės rungtynes
        if best and score >= 3 and best.get("arena"):
            out[r["match_id"]] = best["arena"]
    save_json(ddir / "game_arenas.json", out)
    print(f"LKL: lkl.lt rungtynių {len(games)}, susieta su Flashscore {len(out)}")
    write_status(ddir, "LKL", "lkl.lt (arenos)", not err, err)
    return 1 if err and not skipped else 0   # per pauzę GitHub nebežymi raudonai (laiškas jau išsiųstas)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--season", default="2026-27")
    ap.add_argument("--root", default=".")
    ap.add_argument("--only", choices=["acb", "lkl"])
    ap.add_argument("--max-fetch", type=int, default=120)
    a = ap.parse_args()
    rc = 0
    for k, fn in (("acb", run_acb), ("lkl", run_lkl)):
        if a.only and a.only != k:
            continue
        try:
            rc = max(rc, fn(a.season, Path(a.root), a.max_fetch))
        except Exception as e:  # noqa: BLE001
            print(f"{k}: KLAIDA {e}", file=sys.stderr)
            d = Path(a.root) / ("data" if k == "acb" else "data/lkl")
            write_status(d, k.upper(), f"{'live.acb.com' if k == 'acb' else 'lkl.lt'} (arenos)", False,
                         "svetainė nepasiekiama arba neatsako" if re.search(r"ConnectionPool|Max retries|timed out", str(e))
                         else f"arenų nuskaitymo klaida: {str(e)[:150]}")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
