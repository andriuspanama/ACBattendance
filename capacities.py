#!/usr/bin/env python3
"""
Arenos talpa, svertinė talpa ir užpildymas kiekvienam klubui (visoms lygoms).

Talpų sąrašas – Google Sheets lape „Arenas“ (redaguojamas ranka):
    League | Club | Arena | Capacity | Also known as | Source
  League        – lygos pavadinimas kaip attendance_averages.json faile (ACB, LKL, BBL, ABA, LBA, PLK,
                  LEBL, Korisliiga, NBL, BSL)
  Club          – neprivaloma. Jei įrašyta – tai klubo pagrindinė arena: naudojama rungtynėms, kurių
                  arena nežinoma (ACB, LKL rinkikliai arenos nefiksuoja)
  Arena         – pavadinimas kaip lygos svetainėje
  Capacity      – vietų skaičius krepšiniui
  Also known as – kiti to paties pastato pavadinimai, atskirti „;“

Kiekvienoms rungtynėms:  talpa = lapo talpa (jei yra) → kitaip lygos pateikta talpa (BBL) → kitaip klubo
pagrindinė arena (tik jei rungtynių arena nežinoma).  Efektyvi talpa = max(talpa, lankomumas).
Klubui:  talpa                = dažniausiai naudotos arenos talpa
         svertinė talpa       = efektyvių talpų vidurkis (tik rungtynės su lankomumu ir žinoma talpa)
         užpildymas           = lankomumo suma / efektyvių talpų suma

Rezultatas: data/<aplankas>/capacity.json (atskiras failas – rinkikliai jo neliečia).
Arenos, kurių nėra lape, surašomos į „unknown_arenas“ – jas verta įrašyti į lapą.

Naudojimas:
  python capacities.py --sheet-id <ID> [--sheet Arenas] [--season 2026-27]
  python capacities.py --arenas-csv arenas.csv      (bandymui be Google Sheets)
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import unicodedata
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from regular_season import playoff_ids

# (aplankas, lyga) – visos lygos, kurių lankomumą renka šis repo
LEAGUE_DIRS = ["data", "data/lkl", "data/bbl", "data/nbl", "data/fin", "data/lvee",
               "data/aba", "data/ita", "data/pol", "data/bsl", "data/gre", "data/isr"]

UNKNOWN = {"tbc", "tba", "atanmadi", "koht tapsustamisel", "nezinoma", "unknown", "null", "none", "?"}


GR = {"α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i", "θ": "th", "ι": "i", "κ": "k",
      "λ": "l", "μ": "m", "ν": "n", "ξ": "x", "ο": "o", "π": "p", "ρ": "r", "σ": "s", "ς": "s", "τ": "t",
      "υ": "y", "φ": "f", "χ": "ch", "ψ": "ps", "ω": "o"}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", str(s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(GR.get(c, c) for c in s)          # graikiški pavadinimai (esake.gr)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("ł", "l").replace("Ł", "L").replace("ı", "i").replace("đ", "d").replace("Đ", "D").replace("ß", "ss")
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def fix_text(s: str) -> str:
    """Pataiso „sugadintą“ koduotę, pvz. „BMW Park MĆ¼nchen“ -> „BMW Park München“
    (UTF-8 failas atidarytas Excel'yje kaip Windows-1257 / 1250 / 1252 ir išsaugotas)."""
    s = str(s or "")
    if s.isascii():
        return s
    for enc in ("cp1257", "cp1250", "cp1252", "latin-1"):
        try:
            t = s.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if t != s:
            return t
    return s


def toks(s: str) -> set[str]:
    return {t for t in norm(s).split() if len(t) >= 2}


def to_int(v):
    try:
        n = int(float(re.sub(r"[^\d.]", "", str(v)) or "x"))
        return n if n > 0 else None
    except ValueError:
        return None


def load_arenas_csv(text: str) -> list[dict]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    head = [norm(h) for h in rows[0]]

    def ci(*names):
        for n in names:
            for i, h in enumerate(head):
                if h == n or h.startswith(n):
                    return i
        return None

    c = {"league": ci("league", "lyga"), "club": ci("club", "klubas"), "arena": ci("arena", "venue"),
         "cap": ci("capacity", "talpa"), "aka": ci("also known", "aka", "alias"), "src": ci("source", "saltinis")}
    out = []
    for r in rows[1:]:
        g = lambda k: (r[c[k]].strip() if c[k] is not None and c[k] < len(r) else "")  # noqa: E731
        if not g("league") or not (g("arena") or g("club")):
            continue
        out.append({"league": g("league"), "club": fix_text(g("club")), "arena": fix_text(g("arena")),
                    "capacity": to_int(g("cap")),
                    "aka": [fix_text(a.strip()) for a in g("aka").split(";") if a.strip()], "source": g("src")})
    return out


class Table:
    def __init__(self, rows: list[dict], league: str):
        L = norm(league)
        self.rows = [r for r in rows if norm(r["league"]) == L]

    def arena(self, name: str) -> dict | None:
        n = norm(name)
        if not n or n in UNKNOWN:
            return None
        for r in self.rows:
            if r["arena"] and (norm(r["arena"]) == n or any(norm(a) == n for a in r["aka"])):
                return r
        # visi trumpesnio pavadinimo žodžiai yra ilgesniame (pvz. „Ankara Spor Salonu“ ⊂ „Ankara Arena (Ankara Spor Salonu)“)
        t = toks(name)
        hits = []
        for r in self.rows:
            for cand in [r["arena"], *r["aka"]]:
                u = toks(cand)
                if u and t and (t <= u or u <= t) and min(len(t), len(u)) >= 2:
                    hits.append(r)
                    break
        hits = list({id(h): h for h in hits}.values())
        return hits[0] if len(hits) == 1 else None

    def club_default(self, club: str) -> dict | None:
        n, t = norm(club), toks(club)
        cands = [r for r in self.rows if r["club"]]
        for r in cands:
            if norm(r["club"]) == n:
                return r
        hits = [r for r in cands if toks(r["club"]) and (toks(r["club"]) <= t or t <= toks(r["club"]))]
        return hits[0] if len(hits) == 1 else None


def club_capacity(rows_tbl: Table, matches: list[dict], season: str, league: str = ""):
    not_regular = playoff_ids(league, matches, season)   # tik reguliarusis sezonas
    per = defaultdict(list)
    unknown = Counter()
    unknown_clubs = defaultdict(set)
    games_out = {}
    for m in matches:
        if m.get("season") != season or str(m.get("match_id")) in not_regular:
            continue
        club = (m.get("home") or "").strip()
        att = to_int(m.get("attendance"))
        arena = (m.get("arena") or "").strip()
        rec, cap, used = None, None, arena
        if arena:
            rec = rows_tbl.arena(arena)
            cap = rec["capacity"] if rec and rec["capacity"] else to_int(m.get("capacity"))
        if not cap and (not arena or norm(arena) in UNKNOWN):
            d = rows_tbl.club_default(club)
            if d and d["capacity"]:
                cap, used = d["capacity"], d["arena"] or arena
        # Įspėjimas – tik sužaistoms rungtynėms su lankomumu, kurioms talpos nėra
        if att and not cap:
            key = arena if arena and norm(arena) not in UNKNOWN else "(arena nenurodyta)"
            unknown[key] += 1
            unknown_clubs[key].add(club)
        if rec and rec["arena"]:
            used = rec["arena"]
        games_out[m.get("match_id", "")] = {"arena": used, "capacity": cap}
        if att:
            per[club].append((used, cap, att))

    clubs = []
    for club, games in per.items():
        with_cap = [(a, c, att) for a, c, att in games if c]
        arenas = Counter(a for a, _, _ in games if a)
        caps = {a: c for a, c, _ in with_cap}
        main = arenas.most_common(1)[0][0] if arenas else ""
        row = {"club": club, "home_games_counted": len(games), "games_with_capacity": len(with_cap),
               "capacity": caps.get(main) or (with_cap[0][1] if with_cap else None),
               "weighted_capacity": None, "filling_rate": None,
               "arenas": [{"arena": a, "games": n, "capacity": caps.get(a)} for a, n in arenas.most_common()]}
        if with_cap:
            eff = [max(c, att) for _, c, att in with_cap]
            row["weighted_capacity"] = round(sum(eff) / len(eff))
            row["filling_rate"] = round(sum(att for _, _, att in with_cap) / sum(eff), 4)
        clubs.append(row)
    clubs.sort(key=lambda r: r["club"])
    unk = [{"arena": a, "games": n, "clubs": sorted(unknown_clubs[a])} for a, n in unknown.most_common()]
    return clubs, unk, games_out


def read_matches(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sheet-id")
    ap.add_argument("--sheet", default="Arenas")
    ap.add_argument("--arenas-csv")
    ap.add_argument("--season", default="2026-27")
    ap.add_argument("--root", default=".")
    args = ap.parse_args()

    if args.arenas_csv:
        text = Path(args.arenas_csv).read_text(encoding="utf-8")
    elif args.sheet_id:
        import requests
        url = (f"https://docs.google.com/spreadsheets/d/{args.sheet_id}/gviz/tq?tqx=out:csv&headers=1"
               f"&sheet={urllib.parse.quote(args.sheet)}")
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        text = r.content.decode("utf-8")
    else:
        print("Reikia --sheet-id arba --arenas-csv", file=sys.stderr)
        return 2
    table = load_arenas_csv(text)
    if not table:
        print("Arenų lapas tuščias arba neteisingos antraštės (League | Club | Arena | Capacity | …)", file=sys.stderr)
        return 1
    print(f"Arenų sąraše eilučių: {len(table)}")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for d in LEAGUE_DIRS:
        ddir = Path(args.root) / d
        avg = ddir / "attendance_averages.json"
        if not avg.exists():
            continue
        league = json.loads(avg.read_text(encoding="utf-8")).get("league", "")
        matches = read_matches(ddir / "matches.csv")
        # ACB/LKL: arenos iš game_arenas.py (atskiras failas, rinkikliai jo neliečia)
        try:
            extra = json.loads((ddir / "game_arenas.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            extra = {}
        for m in matches:
            if not (m.get("arena") or "").strip() and extra.get(m.get("match_id", "")):
                m["arena"] = extra[m["match_id"]]
        clubs, unknown, games = club_capacity(Table(table, league), matches, args.season, league)
        # paskutinės rungtynės su lankomumu – puslapis įspėja, jei naujo lankomumo seniai nėra
        # ACB rinkiklis datų nefiksuoja (0000-00-00) – tada imama, kada lankomumas rastas (checked_at)
        def game_day(m):
            d = (m.get("date") or "")[:10]
            return d if re.match(r"20\d\d-\d\d-\d\d", d) else (m.get("checked_at") or "")[:10]
        last_att = max((game_day(m) for m in matches
                        if m.get("season") == args.season and to_int(m.get("attendance"))), default="")
        out = {"league": league, "season": args.season, "updated": now, "clubs": clubs,
               "unknown_arenas": unknown, "last_attendance_game": last_att, "games": games}
        cp = ddir / "capacity.json"
        try:
            old = json.loads(cp.read_text(encoding="utf-8"))
            old["updated"] = now
            same = old == out
        except Exception:  # noqa: BLE001
            same = False
        if not same:   # rašome tik pasikeitus (kad nebūtų tuščių commit'ų kas valandą)
            cp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        done = sum(1 for c in clubs if c["weighted_capacity"])
        print(f"{league:<11} klubų {len(clubs):>3}, su talpa {done:>3}"
              + (f"; nežinomos arenos: {', '.join(u['arena'] for u in unknown)}" if unknown else ""))
    # kiekvieno klubo pagrindinė arena žemėlapiui (data/main_arenas.json) – klaida čia talpų nesugadina
    try:
        from main_arenas import build, sheet_raw_csv
        raw = sheet_raw_csv(args.sheet_id, args.sheet) if args.sheet_id and not args.arenas_csv else None
        build(raw or text, args.root, args.season, now)
    except Exception as e:  # noqa: BLE001
        print(f"! main_arenas.json neatnaujintas: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
