#!/usr/bin/env python3
"""
Kiekvieno klubo PAGRINDINĖ arena šiam sezonui – žemėlapiui (krepsiniofinansai arenamap.js).
Rezultatas: data/main_arenas.json   { "clubs": { "<klubas kaip Budgets lape>": {arena, city, lat, lon, …} } }

Kaip parenkama (pirmas tinkamas):
  1. „Main arena override“ stulpelis lape „Arenas“ – ranka įrašyti klubai (išimtys, pvz. laikinas
     persikraustymas, kai žemėlapyje norima rodyti tikrąją namų areną).
  2. Lygoms, kurių rungtynes renka šis repo – arena, kurioje klubas šį sezoną sužaidė (ar pagal
     tvarkaraštį žais) DAUGIAUSIA reguliariojo sezono namų rungtynių. Lygiosios – vėliausių rungtynių arena.
  3. „Main arena for“ stulpelis lape – kitoms lygoms ir klubams, kurie dar nežaidė namuose.

Lygos klubo pavadinimas (pvz. „Galatasaray Mct Technıc“) susiejamas su Budgets pavadinimu („Galatasaray S.K.“):
  a) per arenos eilutę: jei „Home clubs“ nurodytas vienas (ne „occasional“) klubas – tai jis;
  b) kitaip – pagal bendrus pavadinimo žodžius tarp tos lygos klubų („Home clubs“ stulpelis);
  c) nepavykus – klubas įrašomas į „unmatched“ (jį galima įrašyti į „Main arena override“).
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from capacities import fix_text, norm, read_matches, to_int
from regular_season import playoff_ids

GENERIC = {"bc", "kk", "bk", "fc", "cb", "sk", "basket", "basketball", "basketbol", "baloncesto", "basquet",
           "club", "pallacanestro", "spor", "kulubu", "bkk", "okk", "ks", "mks", "the", "de", "b", "c", "k", "s",
           "j", "baskets", "team", "sport", "city", "sc", "ii", "kosarlabda", "kts", "bt", "rgu"}
UNKNOWN = {"", "tbc", "tba", "atanmadi", "koht tapsustamisel", "nezinoma", "unknown", "null", "none"}
LEAGUE_DIRS = ["data", "data/lkl", "data/bbl", "data/nbl", "data/fin", "data/lvee",
               "data/aba", "data/ita", "data/pol", "data/bsl", "data/gre", "data/isr"]


def _words(s):
    return {t for t in norm(s).split() if len(t) >= 3 and t not in GENERIC}


def _names(cell):
    """„A; B (occasional); C (2024-25)“ -> [(A, main), (B, occasional), (C, old)]"""
    out = []
    for part in (cell or "").split(";"):
        part = fix_text(part.strip())
        if not part:
            continue
        # pastabos skliaustuose: (occasional), (2024-25), (pre-season …), (listed …); kiti skliaustai –
        # pavadinimo dalis, pvz. „Korfez Basket (Manisa)“
        note_re = r"\s*\(((?:[^)]*occasional[^)]*)|(?:[^)]*\d{4}[^)]*)|(?:pre-season[^)]*)|(?:listed[^)]*))\)"
        notes = re.findall(note_re, part)
        name = re.sub(note_re, "", part).strip()
        kind = "occasional" if any("occasional" in n for n in notes) else (
            "old" if any(re.search(r"\d{4}", n) or "pre-season" in n for n in notes) else "main")
        if name and "neutral venue" not in name:
            out.append((name, kind))
    return out


def sheet_raw_csv(sheet_id, sheet="Arenas"):
    """Lapo reikšmės be formatavimo (gviz JSON „v“): CSV eksportas skaičius suapvalina taip, kaip jie
    rodomi lape (pvz. 41.443 vietoj 41.44250) – koordinatėms tai ~100 m paklaida."""
    import urllib.parse
    import requests
    url = (f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:json&headers=1"
           f"&sheet={urllib.parse.quote(sheet)}")
    try:
        t = requests.get(url, timeout=30).content.decode("utf-8")
        j = json.loads(t[t.index("(") + 1: t.rindex(")")])
        cols = [c.get("label") or "" for c in j["table"]["cols"]]
        rows = [cols]
        for r in j["table"]["rows"]:
            vals = []
            for c in r.get("c") or []:
                v = None if c is None else c.get("v")
                if isinstance(v, float):
                    v = repr(v) if not v.is_integer() else str(int(v))
                vals.append("" if v is None else str(v))
            rows.append(vals)
        if not any(norm(c) == "latitude" for c in cols):
            return None
        buf = io.StringIO()
        csv.writer(buf).writerows(rows)
        return buf.getvalue()
    except Exception as e:  # noqa: BLE001
        print(f"! lapo reikšmių be formatavimo gauti nepavyko ({e}) – naudojamas CSV")
        return None


def _coord(v, lim):
    """„54.8904“ arba „54,8904“ -> 54.8904. Jei lapas skaičių sugadino (pvz. „548,904“ – taškas palaikytas
    tūkstančių skyrikliu), grąžina None – tada reikia importuoti Arenas.xlsx iš naujo."""
    v = str(v or "").strip().replace(" ", "")
    if re.fullmatch(r"-?\d+,\d+", v):
        v = v.replace(",", ".")
    try:
        x = float(v)
    except ValueError:
        return None
    return x if -lim <= x <= lim else None


def load_rows(text):
    rows = list(csv.reader(io.StringIO(text)))
    head = [norm(h) for h in rows[0]]

    def ci(*names):
        for n in names:
            for i, h in enumerate(head):
                if h == n:
                    return i
        for n in names:
            for i, h in enumerate(head):
                if h.startswith(n):
                    return i
        return None

    c = {k: ci(*v) for k, v in {
        "league": ("league",), "arena": ("arena",), "aka": ("also known as",), "home": ("home clubs",),
        "main": ("main arena for",), "over": ("main arena override",), "city": ("city",),
        "lat": ("latitude",), "lon": ("longitude",), "cap": ("capacity",)}.items()}
    out = []
    for r in rows[1:]:
        g = lambda k: (r[c[k]].strip() if c[k] is not None and c[k] < len(r) else "")  # noqa: E731
        if not g("league") or not g("arena"):
            continue
        lat, lon = _coord(g("lat"), 90), _coord(g("lon"), 180)
        out.append({"league": g("league"), "arena": fix_text(g("arena")), "city": fix_text(g("city")),
                    "aka": [fix_text(a.strip()) for a in g("aka").split(";") if a.strip()],
                    "home": _names(g("home")), "main": [n for n, _ in _names(g("main"))],
                    "over": [n for n, _ in _names(g("over"))], "lat": lat, "lon": lon,
                    "capacity": to_int(g("cap"))})
    return out


def find_row(rows, league, arena):
    n = norm(arena)
    if not n or n in UNKNOWN:
        return None
    L = norm(league)
    for pool in ([r for r in rows if norm(r["league"]) == L], rows):
        for r in pool:
            if norm(r["arena"]) == n or any(norm(a) == n for a in r["aka"]):
                return r
    return None


def pick(rec):
    return {"arena": rec["arena"], "city": rec["city"], "lat": rec["lat"], "lon": rec["lon"],
            "capacity": rec["capacity"], "league": rec["league"]}


def build(text, root=".", season="2026-27", now=""):
    rows = load_rows(text)
    out, unmatched = {}, []
    # 3) lapas – visiems
    for r in rows:
        for club in r["main"]:
            out[club] = {**pick(r), "source": "sheet"}
    # 2) tikri šio sezono duomenys
    for d in LEAGUE_DIRS:
        ddir = Path(root) / d
        try:
            league = json.loads((ddir / "attendance_averages.json").read_text(encoding="utf-8")).get("league", "")
        except Exception:  # noqa: BLE001
            continue
        matches = read_matches(ddir / "matches.csv")
        try:
            extra = json.loads((ddir / "game_arenas.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            extra = {}
        skip = playoff_ids(league, matches, season)
        games = defaultdict(list)                      # lygos klubas -> [(data, arenos eilutė)]
        for m in matches:
            if m.get("season") != season or str(m.get("match_id")) in skip:
                continue
            arena = (m.get("arena") or "").strip()
            ex = extra.get(m.get("match_id", ""))
            if not arena and isinstance(ex, str):
                arena = ex
            rec = find_row(rows, league, arena)
            if rec:
                games[(m.get("home") or "").strip()].append(((m.get("date") or "")[:10], str(m.get("match_id")), rec))
        L = norm(league)
        league_rows = [r for r in rows if norm(r["league"]) == L]
        pool = {n for r in league_rows for n, k in r["home"]} | {n for r in league_rows for n in r["main"]}
        for club, gl in games.items():
            cnt = Counter(id(rec) for _, _, rec in gl)
            last = {id(rec): (dt, mid) for dt, mid, rec in sorted(gl, key=lambda x: (x[0], x[1]))}
            best_id = max(cnt, key=lambda k: (cnt[k], last[k]))
            best = next(rec for _, _, rec in gl if id(rec) == best_id)
            # lygos pavadinimas -> Budgets pavadinimas
            budget = None
            mains = [n for n, k in best["home"] if k == "main"]
            w = _words(club)
            for names in (mains, pool):
                cands = sorted(((len(w & _words(n)), n) for n in names if w & _words(n)), reverse=True)
                if cands and (len(cands) == 1 or cands[0][0] > cands[1][0]):
                    budget = cands[0][1]
                    break
            if not budget and len(mains) == 1:
                budget = mains[0]
            if not budget:
                unmatched.append({"league": league, "club": club, "arena": best["arena"]})
                continue
            out[budget] = {**pick(best), "source": "games", "games": cnt[best_id], "of_games": len(gl),
                           "league_name": club}
    # 1) ranka įrašytos išimtys
    for r in rows:
        for club in r["over"]:
            out[club] = {**pick(r), "source": "override"}
    res = {"season": season, "updated": now, "clubs": dict(sorted(out.items())), "unmatched": unmatched}
    p = Path(root) / "data" / "main_arenas.json"
    try:
        old = json.loads(p.read_text(encoding="utf-8"))
        old["updated"] = now
        same = old == res
    except Exception:  # noqa: BLE001
        same = False
    if not same:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    n_games = sum(1 for v in out.values() if v["source"] == "games")
    print(f"Pagrindinės arenos: {len(out)} klubų (pagal šio sezono rungtynes {n_games})"
          + (f"; nesusieti: {', '.join(u['club'] for u in unmatched)}" if unmatched else ""))
    return res
