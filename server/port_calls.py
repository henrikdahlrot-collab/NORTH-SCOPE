#!/usr/bin/env python3
"""NORTH SCOPE: anlöpsdetektor (steg 4).

Läser positionerna och skriver anlöp till tabellen port_calls. Förbindelserna mellan hamnar
(vyn port_legs) följer av ordningen mellan ett fartygs anlöp.

Ett anlöp registreras när ett fartyg
  - har positioner inne i en hamns avgränsning (ports.area),
  - har varit där minst --min-dwell-min minuter, och
  - har gått långsamt (högst --slow-kn knop, och inte angett att det ligger för ankar)
    någon gång under vistelsen.
Fartyg som bara passerar i fart räknas därför inte.

    python3 port_calls.py --once          en körning
    python3 port_calls.py --every 300     körs om var femte minut

Skriptet räknar om de senaste --window-h timmarna vid varje körning, och en omräkning ger
samma resultat som förra gången (anlöpen identifieras på fartyg, hamn och ankomsttid).

Kända begränsningar:
  - Avgränsningarna är först cirklar (se ports.py). Ett fartyg på redden eller på väg förbi
    nära en hamn kan räknas som anlöp, och kajplatser utanför cirkeln missas.
  - Positionerna glesas ut (se live_collector.py). Mycket korta stopp, under cirka 20 minuter,
    kan därför missas. Det gäller främst tätt trafikerade färjelinjer.
  - Ett fartyg som stänger av AIS eller lämnar täckningen i hamnen får inget känt avgångsdatum.
"""
import argparse
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

DEFAULT_DB = "postgresql://north:north@db:5432/northscope"
BATCH = 200


@dataclass
class Params:
    window_h: float = 72.0
    min_dwell_s: float = 1200.0
    slow_kn: float = 2.0
    silence_h: float = 24.0
    max_leave_gap_s: float = 21600.0   # längre lucka vid avgång = avgångstiden är okänd
    merge_gap_s: float = 3600.0        # återkomst till samma hamn inom så här lång tid = samma anlöp


def log(text: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z  {text}", flush=True)


# ---------------------------------------------------------------- ren logik (utan databas)

def detect_calls(rows, known_before: bool, p: Params) -> list[dict]:
    """Besök i hamnar ur ett fartygs positioner.

    rows: [(ts, sog, nav_status, locode)] i tidsordning, locode = None utanför alla hamnar.
    known_before: fanns det positioner för fartyget före den första raden?
    Returnerar alla besök, även sådana som inte uppfyller kraven. Se qualifies().
    """
    visits, cur = [], None

    def finish(v, departed, departure_known):
        v["departed"], v["departure_known"] = departed, departure_known
        v["dwell_s"] = (v["last_inside"] - v["arrived"]).total_seconds()
        visits.append(v)

    for i, (ts, sog, nav, loc) in enumerate(rows):
        slow = sog is not None and sog <= p.slow_kn and nav != 1
        if cur is not None and loc == cur["locode"]:
            cur["last_inside"] = ts
            cur["slow"] = cur["slow"] or slow
            continue
        if cur is not None:  # fartyget har lämnat hamnen
            gap = (ts - cur["last_inside"]).total_seconds()
            if gap <= p.max_leave_gap_s:
                finish(cur, ts, True)
            else:
                finish(cur, cur["last_inside"], False)
            cur = None
        if loc is not None:
            cur = {"locode": loc, "arrived": ts, "last_inside": ts, "slow": slow,
                   "truncated_start": i == 0 and known_before,
                   "arrival_known": not (i == 0 and not known_before)}
    if cur is not None:  # ligger kvar
        finish(cur, None, True)
        visits[-1]["open"] = True

    # Slå ihop besök där fartyget gick ut och in igen vid gränsen.
    merged = []
    for v in visits:
        prev = merged[-1] if merged else None
        if (prev and prev["locode"] == v["locode"] and prev["departed"] is not None
                and prev["departure_known"]
                and (v["arrived"] - prev["departed"]).total_seconds() <= p.merge_gap_s):
            prev.update(departed=v["departed"], departure_known=v["departure_known"],
                        last_inside=v["last_inside"], slow=prev["slow"] or v["slow"],
                        open=v.get("open", False))
            prev["dwell_s"] = (prev["last_inside"] - prev["arrived"]).total_seconds()
        else:
            merged.append(v)
    for v in merged:
        v.setdefault("open", False)
    return merged


def qualifies(v: dict, p: Params) -> bool:
    return bool(v["slow"] and v["dwell_s"] >= p.min_dwell_s)


# ---------------------------------------------------------------- databas

CANDIDATES = """
SELECT DISTINCT p.mmsi FROM positions p
JOIN ports po ON ST_Contains(po.area, p.geom)
WHERE p.ts >= %(start)s AND p.ts <= %(end)s
UNION
SELECT mmsi FROM port_calls WHERE departed IS NULL
"""
TRACKS = """
SELECT p.mmsi, p.ts, p.sog_kn, p.nav_status, pl.locode
FROM positions p
LEFT JOIN LATERAL (
    SELECT po.locode FROM ports po WHERE ST_Contains(po.area, p.geom)
    ORDER BY po.geom <-> p.geom LIMIT 1
) pl ON true
WHERE p.mmsi = ANY(%(mmsis)s) AND p.ts >= %(start)s AND p.ts <= %(end)s
ORDER BY p.mmsi, p.ts
"""
FIRST_SEEN = "SELECT mmsi, min(ts) FROM positions WHERE mmsi = ANY(%s) GROUP BY mmsi"
OPEN_CALL = ("SELECT arrived FROM port_calls WHERE mmsi = %s AND locode = %s AND departed IS NULL "
             "ORDER BY arrived DESC LIMIT 1")
UPSERT = """
INSERT INTO port_calls (mmsi, locode, arrived, departed, last_inside, arrival_known, departure_known)
VALUES (%(mmsi)s, %(locode)s, %(arrived)s, %(departed)s, %(last_inside)s, %(arrival_known)s, %(departure_known)s)
ON CONFLICT (mmsi, locode, arrived) DO UPDATE SET
    departed = EXCLUDED.departed,
    last_inside = GREATEST(port_calls.last_inside, EXCLUDED.last_inside),
    departure_known = EXCLUDED.departure_known
"""
CLOSE_SILENT = """
UPDATE port_calls pc SET departed = COALESCE(pc.last_inside, pc.arrived), departure_known = false
WHERE pc.departed IS NULL
  AND NOT EXISTS (SELECT 1 FROM positions p WHERE p.mmsi = pc.mmsi
                  AND p.ts > %(now)s - %(silence_h)s * interval '1 hour')
"""


def run_once(conn, p: Params, now: datetime | None = None) -> dict:
    """En körning. Returnerar en sammanfattning."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(hours=p.window_h)
    stats = {"vessels": 0, "calls": 0, "skipped_truncated": 0, "closed_silent": 0}
    with conn.cursor() as cur:
        cur.execute(CANDIDATES, {"start": start, "end": now})
        mmsis = sorted(r[0] for r in cur.fetchall())
        stats["vessels"] = len(mmsis)
        for i in range(0, len(mmsis), BATCH):
            batch = mmsis[i:i + BATCH]
            cur.execute(FIRST_SEEN, (batch,))
            first_seen = dict(cur.fetchall())
            cur.execute(TRACKS, {"mmsis": batch, "start": start, "end": now})
            tracks: dict[str, list] = {}
            for mmsi, ts, sog, nav, loc in cur.fetchall():
                tracks.setdefault(mmsi, []).append((ts, sog, nav, loc))
            for mmsi, rows in tracks.items():
                known_before = first_seen.get(mmsi, now) < start
                for v in detect_calls(rows, known_before, p):
                    arrived = v["arrived"]
                    if v["truncated_start"]:
                        # Besöket började före fönstret. Det räknas bara om det redan finns ett
                        # pågående anlöp att fortsätta på, annars vet vi inte när det började.
                        cur.execute(OPEN_CALL, (mmsi, v["locode"]))
                        found = cur.fetchone()
                        if not found:
                            stats["skipped_truncated"] += 1
                            continue
                        arrived = found[0]
                    elif not qualifies(v, p):
                        continue
                    cur.execute(UPSERT, {
                        "mmsi": mmsi, "locode": v["locode"], "arrived": arrived,
                        "departed": v["departed"], "last_inside": v["last_inside"],
                        "arrival_known": v["arrival_known"] if not v["truncated_start"] else True,
                        "departure_known": v["departure_known"]})
                    stats["calls"] += 1
        cur.execute(CLOSE_SILENT, {"now": now, "silence_h": p.silence_h})
        stats["closed_silent"] = cur.rowcount
    conn.commit()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="NORTH SCOPE: anlöpsdetektor.")
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL", DEFAULT_DB))
    ap.add_argument("--once", action="store_true", help="kör en gång och avsluta")
    ap.add_argument("--every", type=float, default=300, help="sekunder mellan körningar")
    ap.add_argument("--window-h", type=float, default=72)
    ap.add_argument("--min-dwell-min", type=float, default=20)
    ap.add_argument("--slow-kn", type=float, default=2.0)
    ap.add_argument("--silence-h", type=float, default=24)
    ap.add_argument("--merge-gap-min", type=float, default=60)
    args = ap.parse_args()
    p = Params(window_h=args.window_h, min_dwell_s=args.min_dwell_min * 60, slow_kn=args.slow_kn,
               silence_h=args.silence_h, merge_gap_s=args.merge_gap_min * 60)

    import psycopg
    while True:
        try:
            with psycopg.connect(args.database_url) as conn:
                s = run_once(conn, p)
            log(f"{s['vessels']} fartyg lästa, {s['calls']} anlöp skrivna, "
                f"{s['skipped_truncated']} besök utan känd ankomst hoppades över, "
                f"{s['closed_silent']} anlöp stängdes efter tystnad")
        except Exception as exc:  # databasen kan vara nere en stund
            if args.once:
                raise
            log(f"Körningen misslyckades ({type(exc).__name__}). Försöker igen.")
        if args.once:
            return 0
        time.sleep(args.every)


if __name__ == "__main__":
    sys.exit(main())
