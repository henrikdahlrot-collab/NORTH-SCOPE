#!/usr/bin/env python3
"""Databasprov för hamnar, anlöp och förbindelser (server/ports.py och server/port_calls.py).

Lägger in påhittade hamnar och fartygsrörelser och jämför med facit som räknats ut i förväg.
Rensar tabellerna positions, ports, port_calls. Använd aldrig mot en databas med riktig data.

    TEST_DATABASE_URL=postgresql://north:north@localhost:5544/northscope python3 tests/run_port_test.py
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import port_calls as pc  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL", "postgresql://north:north@localhost:5544/northscope")
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
failures = []


def check(ok, text):
    print(("  ok   " if ok else "  FEL  ") + text)
    if not ok:
        failures.append(text)


def at(h):
    return NOW + timedelta(hours=h)


def main():
    db = psycopg.connect(URL, autocommit=True)
    db.execute("TRUNCATE positions, port_calls, ports CASCADE")
    # Hamnarna ligger på latitud 57, så 0,01 grader longitud är cirka 0,6 km.
    for code, lat, lon in (("XXAAA", 57.0, 11.0), ("XXBBB", 57.0, 11.2), ("XXCCC", 56.98, 10.99)):
        db.execute("INSERT INTO ports (locode, name, country, geom, radius_m, area) VALUES "
                   "(%s, %s, 'XX', ST_SetSRID(ST_MakePoint(%s, %s), 4326), 3000, "
                   "ST_Buffer(ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, 3000)::geometry)",
                   (code, code, lon, lat, lon, lat))

    pos = []

    def add(mmsi, h, lon, sog, nav=0, lat=57.0):
        pos.append((mmsi, at(h), lon, lat, sog, 0.0, nav))

    def track(mmsi, points):
        for h, lon, sog in points:
            add(mmsi, h, lon, sog)

    OUT, EDGE, IN = 11.10, 11.04, 11.0  # utanför / inne vid kanten / mitt i hamn A (longitud)
    # 1. Vanligt anlöp i A: in -9,9 h, långsamt två timmar, ut -7,4 h.
    track("200000001", [(-10, OUT, 10), (-9.9, EDGE, 5), (-9.5, IN, .2), (-9, IN, .2), (-8.5, IN, .2),
                        (-8, IN, .2), (-7.6, IN, .2), (-7.5, EDGE, 6), (-7.4, OUT, 10)])
    # 2. Passerar A i 12 knop.
    track("200000002", [(-6, OUT, 12), (-5.97, EDGE, 12), (-5.95, 10.98, 12), (-5.93, 10.95, 12)])
    # 3. Stannar i A bara sex minuter.
    track("200000003", [(-6, OUT, 10), (-5.95, EDGE, 3), (-5.9, IN, .2), (-5.85, EDGE, 5), (-5.8, OUT, 10)])
    # 4. Ligger för ankar inne i A i fem timmar (status 1).
    for h in (-8, -6, -4, -2, -1):
        add("200000004", h, IN, 0.1, nav=1)
    add("200000004", -0.5, OUT, 8)
    # 5. A, sedan B, sedan A igen nästa dygn.
    track("200000005", [(-20.5, OUT, 10), (-20, EDGE, 3), (-19.9, IN, .3), (-19.5, IN, .3), (-19, IN, .3),
                        (-18.9, EDGE, 5), (-18.8, OUT, 10), (-18.5, 11.17, 3), (-18, 11.2, .2),
                        (-17, 11.2, .2), (-16, 11.2, .2), (-15.9, 11.17, 5), (-15.8, OUT, 10),
                        (-6, EDGE, 3), (-5.5, IN, .2), (-5, IN, .2), (-4.5, IN, .2), (-4.4, EDGE, 4),
                        (-4.3, OUT, 10)])
    # 6. Ligger redan i A när vår första position kommer in.
    track("200000006", [(-5, IN, .2), (-4, IN, .2), (-3, IN, .2), (-2.9, OUT, 8)])
    # 7. Långt anlöp: ankom -91,9 h och ligger kvar. Körs i två omgångar (se nedan).
    track("200000007", [(-92, OUT, 10), (-91.9, EDGE, 3)] + [(-91.5 + k, IN, .2) for k in range(0, 92)])
    # 8. Försvinner ur täckningen i hamnen: sista position -30 h.
    track("200000008", [(-40.5, OUT, 10), (-40.4, EDGE, 3), (-40, IN, .2), (-39.5, IN, .2),
                        (-39, IN, .2), (-38.5, IN, .2), (-30, IN, .2)])
    # 9. Hoppar ut och in vid cirkelns kant (tio minuter utanför): ska bli ett anlöp.
    track("200000009", [(-12, OUT, 10), (-11.9, EDGE, 3), (-11.5, IN, .2), (-11, IN, .2), (-10.5, IN, .2),
                        (-10.4, 11.05, 1), (-10.3, 11.05, 1), (-10.2, EDGE, .5), (-9.5, IN, .2),
                        (-9, IN, .2), (-8.9, EDGE, 5), (-8.8, OUT, 10)])
    # 10. Ligger där A:s och C:s cirklar överlappar, närmast C: ska bli ett anlöp i C, inte i A.
    for h, sog in ((-5, 3), (-4.5, .2), (-4, .2), (-3.5, .2), (-3, .2), (-2.9, 6)):
        add("200000010", h, 10.992, sog, lat=56.985)
    add("200000010", -2.8, OUT, 10)

    db.cursor().executemany(
        "INSERT INTO positions (mmsi, ts, geom, sog_kn, cog_deg, nav_status) "
        "VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s)", pos)

    p = pc.Params()
    print(f"{len(pos)} påhittade positioner inlagda.\n")
    # Fartyg 7 körs först, med ett tidigare 'nu', så att anlöpet redan finns när fönstret flyttas.
    with psycopg.connect(URL) as conn:
        s1 = pc.run_once(conn, p, now=at(-40))
    q = lambda sql, *a: db.execute(sql, a).fetchall()
    first = q("SELECT arrived, departed FROM port_calls WHERE mmsi = '200000007'")
    check(len(first) == 1 and first[0][1] is None and first[0][0] == at(-91.9),
          "omgång 1: långt anlöp har ankomst -91,9 h och är pågående")
    with psycopg.connect(URL) as conn:
        s2 = pc.run_once(conn, p, now=NOW)

    calls = {}
    for r in q("SELECT mmsi, locode, arrived, departed, last_inside, arrival_known, departure_known "
               "FROM port_calls ORDER BY mmsi, arrived"):
        calls.setdefault(r[0], []).append(r)

    print("Anlöp:")
    c = calls.get("200000001", [])
    check(len(c) == 1 and c[0][1] == "XXAAA" and c[0][2] == at(-9.9) and c[0][3] == at(-7.4)
          and c[0][5] and c[0][6], "1: ett anlöp i A, ankomst -9,9 h, avgång -7,4 h")
    check("200000002" not in calls, "2: fartyg som passerar i fart räknas inte")
    check("200000003" not in calls, "3: stopp på sex minuter räknas inte")
    check("200000004" not in calls, "4: fartyg som ligger för ankar räknas inte")
    c = calls.get("200000005", [])
    check([(x[1], x[2], x[3]) for x in c] == [("XXAAA", at(-20), at(-18.8)), ("XXBBB", at(-18.5), at(-15.8)),
                                              ("XXAAA", at(-6), at(-4.3))],
          "5: tre anlöp i ordningen A, B, A med rätt tider")
    c = calls.get("200000006", [])
    check(len(c) == 1 and c[0][5] is False and c[0][2] == at(-5) and c[0][3] == at(-2.9),
          "6: fartyg som redan låg i hamnen får okänd ankomst")
    c = calls.get("200000007", [])
    check(len(c) == 1 and c[0][2] == at(-91.9) and c[0][3] is None and c[0][4] == at(-0.5)
          and c[0][5], "7: långt anlöp fortsätter på samma rad när fönstret flyttats (ankomst bevaras)")
    c = calls.get("200000008", [])
    check(len(c) == 1 and c[0][2] == at(-40.4) and c[0][3] == at(-30) and c[0][6] is False,
          "8: fartyg som försvinner stängs efter 24 h tystnad, med okänd avgång")
    c = calls.get("200000009", [])
    check(len(c) == 1 and c[0][2] == at(-11.9) and c[0][3] == at(-8.8),
          "9: utgång och återkomst vid kanten blir ett enda anlöp")
    c = calls.get("200000010", [])
    check([x[1] for x in c] == ["XXCCC"], "10: fartyg i överlappet hör till närmaste hamn (C)")
    check(sum(len(v) for v in calls.values()) == 9, f"totalt 9 anlöp ({sum(len(v) for v in calls.values())})")

    print("\nFörbindelser:")
    legs = q("SELECT from_locode, to_locode FROM port_legs WHERE mmsi = '200000005' ORDER BY arrived")
    check(legs == [(None, "XXAAA"), ("XXAAA", "XXBBB"), ("XXBBB", "XXAAA")],
          f"fartyg 5: föregående och nästa hamn stämmer ({legs})")

    print("\nOmräkning:")
    before = q("SELECT * FROM port_calls ORDER BY id")
    with psycopg.connect(URL) as conn:
        pc.run_once(conn, p, now=NOW)
    after = q("SELECT * FROM port_calls ORDER BY id")
    check(before == after, "en ny körning ger exakt samma rader (inga dubbletter)")
    check(s2["skipped_truncated"] >= 0, f"körning 2: {s2}")

    # ------------------------------------------------------------ polygonförslag
    print("\nPolygonförslag och godkännande:")
    db.execute("TRUNCATE positions, port_calls, ports CASCADE")
    db.execute("INSERT INTO ports (locode, name, country, geom, radius_m, area) VALUES "
               "('XXDDD', 'Hamn D', 'XX', ST_SetSRID(ST_MakePoint(11.505, 58.002), 4326), 3000, "
               "ST_Buffer(ST_SetSRID(ST_MakePoint(11.505, 58.002), 4326)::geography, 3000)::geometry)")
    real_now = datetime.now(timezone.utc)
    pos = []
    # Åtta fartyg vid kajer längs en sträcka (longitud 11,50-11,51), sex positioner var.
    for v in range(8):
        for k in range(6):
            pos.append((f"20000010{v}", real_now - timedelta(hours=2 + k), 11.50 + 0.0015 * v,
                        58.0 + 0.0002 * k, 0.1, 0.0, 5))
    # Brus som inte ska påverka: fartyg för ankar 2 km bort, fartyg i fart, fartyg utanför sökområdet.
    for k in range(40):
        pos.append(("200000200", real_now - timedelta(hours=1, minutes=k), 11.505, 58.02, 0.1, 0.0, 1))
        pos.append(("200000201", real_now - timedelta(hours=1, minutes=k), 11.50 + 0.0003 * k, 58.0, 12.0, 0.0, 0))
        pos.append(("200000202", real_now - timedelta(hours=1, minutes=k), 12.5, 58.0, 0.1, 0.0, 5))
    db.cursor().executemany(
        "INSERT INTO positions (mmsi, ts, geom, sog_kn, cog_deg, nav_status) "
        "VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s)", pos)

    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "server" / "ports.py"),
                                     "--database-url", URL, *a], capture_output=True, text=True)
    r = run("suggest")
    check(r.returncode == 0 and "XXDDD" in r.stdout, "suggest föreslår en polygon för hamn D")
    row = q("SELECT suggested_support, suggested_vessels, "
            "ST_Area(area_suggested::geography)/1e6, ST_Area(area::geography)/1e6, "
            "ST_Contains(area_suggested, ST_SetSRID(ST_MakePoint(11.5105, 58.0005), 4326)), "
            "ST_Contains(area_suggested, ST_SetSRID(ST_MakePoint(11.505, 58.02), 4326)) "
            "FROM ports WHERE locode = 'XXDDD'")[0]
    check(row[0] == 48 and row[1] == 8, f"underlaget är 48 stillastående positioner från 8 fartyg ({row[0]}, {row[1]})")
    check(row[2] < row[3] / 5, f"förslaget ({row[2]:.2f} km²) är mycket mindre än cirkeln ({row[3]:.1f} km²)")
    check(row[4] is True, "förslaget täcker kajerna")
    check(row[5] is False, "förslaget täcker inte ankarplatsen 2 km bort")
    check(q("SELECT area_source FROM ports WHERE locode = 'XXDDD'")[0][0] == "circle",
          "ingenting godkänns av sig självt: avgränsningen är fortfarande cirkeln")
    import json, tempfile as _tf
    gj = Path(_tf.mkdtemp()) / "h.geojson"
    r = run("geojson", "--out", str(gj))
    feats = json.loads(gj.read_text())["features"] if r.returncode == 0 else []
    check(sorted(f["properties"]["kind"] for f in feats) == ["förslag", "nuvarande circle"],
          "geojson ger nuvarande avgränsning och förslag för granskning på karta")
    r = run("apply", "--all")
    check(r.returncode == 0 and "XXDDD" not in r.stdout,
          "apply --all godkänner inte ett förslag med för litet underlag (48 av 50 positioner)")
    r = run("apply", "--all", "--min-positions", "40")
    check(r.returncode == 0 and "XXDDD" in r.stdout, "apply --all godkänner förslaget när underlaget räcker")
    check(q("SELECT area_source FROM ports WHERE locode = 'XXDDD'")[0][0] == "polygon", "hamnen har nu polygon")
    # Efter godkännandet ska ett fartyg som ligger still på redden (inne i gamla cirkeln, utanför polygonen) inte ge anlöp.
    pos = [("200000300", real_now - timedelta(hours=6 - 0.5 * k), 11.505, 58.02, 0.1, 0.0, 0) for k in range(10)]
    db.cursor().executemany(
        "INSERT INTO positions (mmsi, ts, geom, sog_kn, cog_deg, nav_status) "
        "VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s)", pos)
    with psycopg.connect(URL) as conn:
        pc.run_once(conn, pc.Params(), now=real_now)
    check(q("SELECT count(*) FROM port_calls WHERE mmsi = '200000300'")[0][0] == 0,
          "fartyg på redden utanför polygonen ger inget anlöp efter godkännandet")
    check(q("SELECT count(*) FROM port_calls WHERE mmsi LIKE '20000010_'")[0][0] == 8,
          "de åtta fartygen vid kaj ger ett anlöp var")
    r = run("stats")
    check(r.returncode == 0 and "polygon" in r.stdout, "stats fungerar")
    # En ny inläsning av hamnlistan får inte skriva över en godkänd polygon, men får uppdatera namnet.
    import tempfile
    tiny = Path(tempfile.mkdtemp()) / "tiny.csv"
    tiny.write_text("Change,Country,Location,Name,NameWoDiacritics,Subdivision,Status,Function,Date,IATA,Coordinates,Remarks\n"
                    ",XX,DDD,Hamn D nytt namn,Hamn D nytt namn,,AA,1-------,,,5900N 01200E,\n"
                    ",XX,FFF,Hamn F,Hamn F,,AA,1-------,,,5700N 01000E,\n"
                    ",XX,GGG,Utan hamnfunktion,Utan hamnfunktion,,AA,--3-----,,,5700N 01000E,\n"
                    ",XX,HHH,Utan koordinater,Utan koordinater,,AA,1-------,,,,\n", encoding="utf-8")
    before = q("SELECT ST_AsText(area), ST_AsText(geom) FROM ports WHERE locode = 'XXDDD'")[0]
    r = run("load", "--file", str(tiny), "--extra", str(tiny.parent / "finns_inte.csv"))
    after = q("SELECT ST_AsText(area), ST_AsText(geom), name FROM ports WHERE locode = 'XXDDD'")[0]
    check(r.returncode == 0 and before == after[:2], "ny inläsning rör inte polygonen eller koordinaten för en godkänd hamn")
    check(after[2] == "Hamn D nytt namn", "men namnet uppdateras")
    check(q("SELECT area_source FROM ports WHERE locode = 'XXFFF'")[0][0] == "circle"
          and q("SELECT count(*) FROM ports WHERE locode IN ('XXGGG', 'XXHHH')")[0][0] == 0,
          "ny hamn får cirkel, och poster utan hamnfunktion eller koordinater hoppas över")
    check("1 hamnar saknar koordinater" in r.stdout or "saknar koordinater" in r.stdout, "hamnar utan koordinater redovisas")

    print(f"\n{'ALLT GODKÄNT' if not failures else str(len(failures)) + ' FEL'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
