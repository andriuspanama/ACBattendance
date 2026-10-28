"""
Rinkiklio būsena – data/<aplankas>/run_status.json. Puslapis pagal jį rodo įspėjimą,
jei paskutinis nuskaitymas nepavyko (pvz. svetainė ėmė blokuoti) arba duomenys seniai neatnaujinti.

  ok                   – ar paskutinis paleidimas pavyko
  error                – kas negerai (jei ok = false)
  notes                – švelnesni įspėjimai (pvz. patikrinta rungtynių, bet lankomumo nerasta)
  last_ok              – kada paskutinį kartą pavyko (atnaujinama ne dažniau kaip kas 12 val.,
                         kad nebūtų commit'o po kiekvieno paleidimo)
  last_new_attendance  – kada paskutinį kartą rasta naujo lankomumo
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse(s):
    try:
        return datetime.fromisoformat(str(s))
    except Exception:  # noqa: BLE001
        return None


def write_status(data_dir, league: str, source: str, ok: bool, error: str = "",
                 notes: list[str] | None = None, new_attendance: int = 0) -> None:
    p = Path(data_dir) / "run_status.json"
    try:
        prev = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        prev = {}
    now = _now()
    last_ok = prev.get("last_ok")
    if ok:
        lo = _parse(last_ok)
        if not lo or now - lo > timedelta(hours=12):
            last_ok = now.isoformat()
    last_new = now.isoformat() if new_attendance else prev.get("last_new_attendance")
    out = {"league": league, "source": source, "ok": bool(ok), "error": error or "",
           "notes": notes or [], "last_ok": last_ok, "last_new_attendance": last_new,
           "run_at": prev.get("run_at")}
    # run_at keičiame tik pasikeitus būsenai arba kartu su last_ok – kad nebūtų tuščių commit'ų
    keys = ("ok", "error", "notes", "last_ok", "last_new_attendance", "source")
    if any(out[k] != prev.get(k) for k in keys) or not prev:
        out["run_at"] = now.isoformat()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    if not ok:
        print(f"!!! {league}: {error}")
    for n in notes or []:
        print(f"! {league}: {n}")
