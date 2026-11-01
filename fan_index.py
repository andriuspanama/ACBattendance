#!/usr/bin/env python3
"""
Sirgalių traukos indeksai ir profiliai einamajam sezonui (kaip Fan_attraction_index_combined.xlsx).
Rezultatas: data/fan_index.json – jį skaito pagrindinis puslapis (krepsiniofinansai).

Duomenys:
  * kiekvienos lygos data/<aplankas>/matches.csv – sužaistos namų rungtynės ir jų lankomumas
    (tik reguliarusis sezonas, žr. regular_season.py);
  * Google Sheets lapas „Populations“ – klubo rinkos dydis (Effective population) ir urbanistinė zona;
  * fan_index_model.json – 2025/26 Excel modelio skaičiai (Europos linija ir rinkos elastingumas b
    įšaldyti; lygų 2025/26 bazės – tik lygoms, kuriose trūksta duomenų).

Taisyklės (sutarta 2026-11-01):
  * Klubo duomenys naudojami, jei lankomumas paskelbtas bent 50 % jo sužaistų namų rungtynių (ir bent 1).
  * Europos indeksas = vidurkis ÷ (intercept + slope × ln(populiacija)) – 2025/26 linija.
  * Savos lygos indeksas = vidurkis ÷ [lygos tipinis lankomumas × (populiacija ÷ lygos tipinė populiacija)^b]:
      - lygoje tinkamų klubų ≥ 85 % → tipinis lankomumas ir populiacija skaičiuojami iš ŠIO sezono
        (geometrinis vidurkis, to paties miesto klubai turi bendrą svorį 1 ÷ klubų skaičius) – kaip Excel;
      - < 85 %, bet lyga buvo 2025/26 modelyje (pvz. Italija) → 2025/26 bazė × vidutinis klubų pokytis
        (klubai, kurių duomenys yra abiem sezonais);
      - < 85 % ir lygos 2025/26 modelyje nebuvo (pvz. Graikija) → tik Europos indeksas.
  * Profilis – tik kai yra abu indeksai; ribos 0,9 / 1,1 (kaip Excel Method lape).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from capacities import league_dirs, read_matches
from regular_season import playoff_ids

# ACBattendance lygos → 2025/26 modelio (Excel) lyga
MODEL_LEAGUE = {"ACB": "Spain", "BBL": "Germany", "LBA": "Italy", "BSL": "Turkey", "ABA": "ABA League",
                "LKL": "Lithuania", "PLK": "Poland", "LEBL": "LEBL", "Korisliiga": "Finland", "NBL": "Australia"}
PROFILE = {  # (savos lygos lygis, Europos lygis) -> tekstas, kaip Excel
    ("hi", "hi"): "Strong in own league and in Europe", ("hi", "mid"): "Strong in own league, in line with Europe",
    ("hi", "lo"): "Strong in own league, below Europe", ("mid", "hi"): "In line with own league, above Europe",
    ("mid", "mid"): "In line with own league and Europe", ("mid", "lo"): "In line with own league, below Europe",
    ("lo", "hi"): "Below own league, above Europe", ("lo", "mid"): "Below own league, in line with Europe",
    ("lo", "lo"): "Below own league and Europe"}
EUROPE_READING = {"hi": "Above Europe", "mid": "In line with Europe", "lo": "Below Europe"}


def nz(s) -> str:
    s = unicodedata.normalize("NFD", str(s or "").lower().replace("ł", "l").replace("ı", "i").replace("đ", "d"))
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)))


def num(v):
    try:
        x = float(str(v).replace(",", ".").replace(" ", "").replace(" ", ""))
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def sheet_csv(sheet_id: str, sheet: str) -> str:
    """Lapo reikšmės be formatavimo (gviz JSON), kaip main_arenas.py."""
    import urllib.parse
    import requests
    url = (f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:json&headers=1"
           f"&sheet={urllib.parse.quote(sheet)}")
    t = requests.get(url, timeout=30).content.decode("utf-8")
    j = json.loads(t[t.index("(") + 1: t.rindex(")")])
    rows = [[c.get("label") or "" for c in j["table"]["cols"]]]
    for r in j["table"]["rows"]:
        vals = []
        for c in r.get("c") or []:
            v = None if c is None else c.get("v")
            if isinstance(v, float):
                v = repr(v) if not v.is_integer() else str(int(v))
            vals.append("" if v is None else str(v))
        rows.append(vals)
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue()


def load_populations(text: str):
    rows = list(csv.reader(io.StringIO(text)))
    head = [nz(h) for h in rows[0]]

    def col(*keys):
        for k in keys:
            for i, h in enumerate(head):
                if h.startswith(k):
                    return i
        return None
    ic, ia, iu, ie = col("club"), col("nameonacbattendance"), col("urbanarea"), col("effectivepopulation")
    if ic is None or ie is None:
        raise SystemExit("Populations: nerasti stulpeliai Club / Effective population")
    out = {}
    for r in rows[1:]:
        g = lambda i: (r[i].strip() if i is not None and i < len(r) else "")  # noqa: E731
        club, eff = g(ic), num(g(ie))
        if not club or not eff or eff <= 0:
            continue
        rec = {"club": club, "area": g(iu), "pop": eff}
        out[nz(club)] = rec
        if g(ia):
            out.setdefault(nz(g(ia)), rec)
    return out


def level(idx, lo, hi):
    return "hi" if idx >= hi else "mid" if idx > lo else "lo"


def city_weights(clubs):
    """To paties miesto (tos pačios populiacijos) klubai lygoje skaičiuojami kaip vienas: svoris 1 ÷ jų skaičius."""
    cnt = defaultdict(int)
    for c in clubs:
        cnt[round(c["population"])] += 1
    return {id(c): 1 / cnt[round(c["population"])] for c in clubs}


def build(pop_text: str, model: dict, root: str = ".", season: str = "2026-27", today: str | None = None):
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pops = load_populations(pop_text)
    E, T = model["europe"], model["thresholds"]
    b, lo, hi = E["elasticity"], T["band_low"], T["band_high"]
    try:
        names = json.loads((Path(root) / "data" / "main_arenas.json").read_text(encoding="utf-8")).get("names", {})
    except Exception:  # noqa: BLE001
        names = {}
    out_clubs, out_leagues, unmatched = [], {}, []
    for d in league_dirs(root):
        ddir = Path(root) / d
        try:
            league = json.loads((ddir / "attendance_averages.json").read_text(encoding="utf-8")).get("league", "")
        except Exception:  # noqa: BLE001
            continue
        matches = [m for m in read_matches(ddir / "matches.csv") if m.get("season") == season]
        if not matches:
            continue
        skip = playoff_ids(league, matches, season)
        try:
            caps = {c["club"]: c for c in json.loads((ddir / "capacity.json").read_text(encoding="utf-8")).get("clubs", [])}
        except Exception:  # noqa: BLE001
            caps = {}
        lnames = names.get(league, {})
        teams, played, reported = set(), defaultdict(int), defaultdict(list)
        for m in matches:
            if str(m.get("match_id")) in skip:
                continue
            h, a = (m.get("home") or "").strip(), (m.get("away") or "").strip()
            teams.update(x for x in (h, a) if x)
            if not h or str(m.get("attempts") or "") == "99":      # uždarytos durys – nei sužaistos, nei paskelbtos
                continue
            att = num(m.get("attendance"))
            date = (m.get("date") or "")[:10]
            past = bool(re.match(r"20\d\d-\d\d-\d\d$", date)) and date < today
            if att and att > 0:
                played[h] += 1
                reported[h].append(att)
            elif past:                                               # sužaista, bet lankomumas nepaskelbtas
                played[h] += 1
        clubs = []
        for t in sorted(teams):
            sheet = lnames.get(t, t)
            p = pops.get(nz(sheet)) or pops.get(nz(t))
            g, rep = played.get(t, 0), reported.get(t, [])
            rec = {"league": league, "league_name": t, "club": p["club"] if p else sheet,
                   "population": p["pop"] if p else None, "area": p["area"] if p else "",
                   "home_games": g, "reported_games": len(rep),
                   "average": round(sum(rep) / len(rep)) if rep else None}
            rec["eligible"] = bool(p and rep and len(rep) >= 0.5 * g)
            if not p:
                unmatched.append({"league": league, "club": t, "sheet_name": sheet})
            cp = caps.get(t) or {}
            if cp.get("filling_rate") is not None:
                rec["filling_rate"] = cp["filling_rate"]
                rec["arena_full"] = cp["filling_rate"] >= T["full_arena"]
            clubs.append(rec)
        elig = [c for c in clubs if c["eligible"]]
        cov = len(elig) / len(clubs) if clubs else 0
        info = {"clubs": len(clubs), "eligible": len(elig), "coverage": round(cov, 3)}
        # --- savos lygos bazė ---
        N = M = None
        if elig and cov >= model.get("coverage_threshold", 0.85):
            w = city_weights(elig)
            sw = sum(w.values())
            N = math.exp(sum(w[id(c)] * math.log(c["average"]) for c in elig) / sw)
            M = sum(w[id(c)] * math.log(c["population"]) for c in elig) / sw
            info["method"] = "this season"
        elif MODEL_LEAGUE.get(league) in model["leagues"]:
            base = model["leagues"][MODEL_LEAGUE[league]]
            prev = {nz(k): v["attendance"] for k, v in base["clubs"].items()}
            both = [c for c in elig if nz(c["club"]) in prev]
            if len(both) >= 3:
                w = city_weights(both)
                sw = sum(w.values())
                factor = math.exp(sum(w[id(c)] * math.log(c["average"] / prev[nz(c["club"])]) for c in both) / sw)
                N, M = base["typical_attendance"] * factor, base["mean_ln_pop"]
                info.update(method="2025/26 baseline × change of clubs reporting in both seasons",
                            change_factor=round(factor, 4), clubs_in_change=len(both))
            else:
                info["method"] = "Europe index only (too few clubs reporting)"
        else:
            info["method"] = "Europe index only (league not in the 2025/26 model, coverage below threshold)"
        if N:
            info.update(typical_attendance=round(N), typical_population=round(math.exp(M)))
        out_leagues[league] = info
        # --- indeksai ---
        for c in clubs:
            if not c["eligible"]:
                c["note"] = ("no population in Populations tab" if c["population"] is None else
                             "no published attendance yet" if not c["reported_games"] else
                             "attendance published for under 50% of home games")
                out_clubs.append(c)
                continue
            lnp = math.log(c["population"])
            expE = E["intercept"] + E["slope"] * lnp
            if expE > 0:
                c["expected_europe"] = round(expE)
                c["index_europe"] = round(c["average"] / expE, 3)
            if N:
                expL = N * math.exp(b * (lnp - M))
                c["expected_league"] = round(expL)
                c["index_league"] = round(c["average"] / expL, 3)
            if "index_europe" in c and "index_league" in c:
                c["profile"] = PROFILE[(level(c["index_league"], lo, hi), level(c["index_europe"], lo, hi))]
            elif "index_europe" in c:
                c["europe_reading"] = EUROPE_READING[level(c["index_europe"], lo, hi)]
            out_clubs.append(c)
    return {"season": season, "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": {"source": model.get("source"), "europe": E, "thresholds": T,
                      "coverage_threshold": model.get("coverage_threshold", 0.85), "min_reported_share": 0.5},
            "leagues": out_leagues, "clubs": out_clubs, "unmatched": unmatched}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sheet-id")
    ap.add_argument("--sheet", default="Populations")
    ap.add_argument("--populations-csv", help="vietoj Google Sheets (bandymams)")
    ap.add_argument("--model", default="fan_index_model.json")
    ap.add_argument("--season", default="2026-27")
    ap.add_argument("--root", default=".")
    ap.add_argument("--today")
    args = ap.parse_args()
    text = Path(args.populations_csv).read_text(encoding="utf-8") if args.populations_csv else sheet_csv(args.sheet_id, args.sheet)
    model = json.loads(Path(args.model).read_text(encoding="utf-8"))
    res = build(text, model, args.root, args.season, args.today)
    p = Path(args.root) / "data" / "fan_index.json"
    try:
        old = json.loads(p.read_text(encoding="utf-8"))
        old["updated"] = res["updated"]
        same = old == res
    except Exception:  # noqa: BLE001
        same = False
    if not same:
        p.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    for lg, i in res["leagues"].items():
        print(f"{lg:14} {i['eligible']:2}/{i['clubs']:2} klubų ({i['coverage']:.0%}) – {i['method']}")
    if res["unmatched"]:
        print("! be populiacijos:", ", ".join(u["sheet_name"] for u in res["unmatched"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
