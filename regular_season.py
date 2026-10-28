"""
Tik reguliarusis sezonas – kaip vartotojo Excel failuose (BSL, LEBL, PLK, ABA, Korisliiga, NB I/A):
atkrintamosios (playoff), play-in, finalai ir pan. į vidurkius, talpą ir užpildymą NEįskaičiuojami.

Lygos, kurių svetainė pati nurodo etapą, filtruojamos rinkiklyje (official_attendance.py):
  NBL – season_type „regular“ (be Blitz, be finalų)      LBA – čempionato ID 602 = Regular Season
  BBL – stage „MAIN_ROUND“                               PLK – round „Runda Zasadnicza“
  ABA – Stage 1 + Top8 / Play-out (be Play-in ir PO)     GBL – esake.gr „Α Φάση – Κανονική Περίοδος“
  Korisliiga – runkosarja + jatkosarja (be pudotuspelit, be karsinnat)

Lygoms, kurių duomenyse etapo nėra, taisyklė čia:
  home_games: N – kiekvieno klubo pirmosios N namų rungtynių (pagal datą, jei datos nėra – pagal ID);
              "auto" = komandų skaičius − 1 (dvigubas ratų turnyras)
  end:        rungtynės po šios datos – nebe reguliarusis sezonas
Sezonui pasikeitus – peržiūrėkite (pvz. LKL reguliariojo sezono pabaigos data).
"""
from __future__ import annotations

import re

RULES = {
    "2026-27": {
        "ACB": {"home_games": 17},           # 18 komandų, 34 turai (ACB rinkiklis datų nefiksuoja – eilė pagal ID)
        "LKL": {"end": "2027-05-16", "home_games": 18},  # 10 komandų, 36 turai (18 namų); paskutinė reguliariojo sezono diena lkl.lt
                                             # tvarkaraštyje (ten pat rodomos ir KMT rungtynės – jos į LKL duomenis
                                             # nepatenka, nes lankomumas imamas iš Flashscore LKL puslapio)
        "BBL": {"home_games": "auto"},       # 18 komandų → 17 (Flashscore duomenims; oficialūs – pagal stage)
        "LEBL": {"home_games": "auto"},      # komandų − 1 (2025/26: 14 komandų → 13, kaip Excel faile)
        "BSL": {"home_games": "auto"},       # 16 komandų → 15 (kaip BSL_attendance_2023-26_correct.xlsx)
    },
}


def _order(m: dict):
    d = (m.get("date") or "")[:10]
    d = d if re.match(r"20\d\d-\d\d-\d\d$", d) else ""
    mid = str(m.get("match_id") or "")
    return (d or "9999", int(mid) if mid.isdigit() else 0, mid)


def playoff_ids(league: str, matches, season: str) -> set[str]:
    """Rungtynių ID, kurios NĖRA reguliariojo sezono (neįskaičiuojamos)."""
    rule = RULES.get(season, {}).get(str(league).upper()) or RULES.get(season, {}).get(league)
    if not rule:
        return set()
    rows = [m for m in (matches.values() if isinstance(matches, dict) else matches)
            if m.get("season") == season and (m.get("home") or "").strip()]
    out: set[str] = set()
    end = rule.get("end")
    if end:
        for m in rows:
            d = (m.get("date") or "")[:10]
            if re.match(r"20\d\d-\d\d-\d\d$", d) and d > end:
                out.add(str(m.get("match_id")))
    cap = rule.get("home_games")
    if cap == "auto":
        teams = {(m.get("home") or "").strip() for m in rows} | {(m.get("away") or "").strip() for m in rows}
        teams.discard("")
        cap = len(teams) - 1 if len(teams) >= 4 else None
    if cap:
        seen: dict[str, int] = {}
        for m in sorted(rows, key=_order):
            h = m["home"].strip()
            seen[h] = seen.get(h, 0) + 1
            if seen[h] > cap:
                out.add(str(m.get("match_id")))
    return out
