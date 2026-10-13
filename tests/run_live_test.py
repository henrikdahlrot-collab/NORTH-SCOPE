#!/usr/bin/env python3
"""Provar server/live_collector.py mot låtsasservern och en riktig PostGIS-databas.

Kräver en databas med server/schema.sql inläst. Anges med TEST_DATABASE_URL.
Rensar tabellerna före varje körning. Använd aldrig mot en databas med riktig data.

    TEST_DATABASE_URL=postgresql://north:north@localhost:5544/northscope \
        python3 tests/run_live_test.py [--outage "kommando som startar om databasen"]
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
URL = os.environ.get("TEST_DATABASE_URL", "postgresql://north:north@localhost:5544/northscope")
failures = []


def check(ok, text):
    print(("  ok   " if ok else "  FEL  ") + text)
    if not ok:
        failures.append(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outage", default=None, help="kommando som startar om databasen under körningen")
    ap.add_argument("--seconds", type=float, default=14)
    args = ap.parse_args()

    with psycopg.connect(URL, autocommit=True) as db:
        db.execute("TRUNCATE positions, vessels, excluded_vessels, collector_runs RESTART IDENTITY")

    facit_path = Path(tempfile.mkdtemp()) / "facit.json"
    fake = subprocess.Popen([sys.executable, str(ROOT / "tests" / "fake_live_aisstream.py"),
                             "--facit", str(facit_path)], stdout=subprocess.PIPE, text=True)
    fake.stdout.readline()
    time.sleep(0.5)

    cmd = [sys.executable, str(ROOT / "server" / "live_collector.py"),
           "--url", "ws://localhost:8766", "--api-key", "test-key", "--database-url", URL,
           "--seconds", str(args.seconds), "--flush-s", "1", "--silence-s", "5",
           "--min-gap-s", "0.3", "--moving-max-s", "1.5", "--still-max-s", "4"]
    collector = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if args.outage:
        time.sleep(7)
        print("Startar om databasen under körningen ...")
        subprocess.run(args.outage, shell=True, check=True, timeout=60, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    output, _ = collector.communicate(timeout=args.seconds + 60)
    time.sleep(0.5)
    fake.terminate()
    facit = json.loads(facit_path.read_text())

    print("\nUtskrift från insamlingen (sista raderna):")
    print("\n".join("  | " + line for line in output.strip().splitlines()[-6:]))
    print(f"\nExitkod: {collector.returncode}")
    check(collector.returncode == 0, "insamlingen avslutades utan fel")
    check("test-key" not in output, "API-nyckeln skrivs inte ut")

    with psycopg.connect(URL, autocommit=True) as db:
        q = lambda sql, *a: db.execute(sql, a).fetchall()
        vessels = {r[0]: r for r in q("SELECT mmsi, flag, ship_type, length_m, destination, eta FROM vessels")}
        pos = dict(q("SELECT mmsi, count(*) FROM positions GROUP BY mmsi"))
        excluded = dict(q("SELECT mmsi, reason FROM excluded_vessels"))
        run = q("SELECT received, stored_positions, thinned_out, excluded, rejected, reconnects, ended "
                "FROM collector_runs ORDER BY id DESC LIMIT 1")[0]
        span = q("SELECT extract(epoch FROM max(ts) - min(ts)) FROM positions")[0][0]

    print("\nFartyg som ska sparas:")
    wrong = [v for v in facit["normal"]
             if v["mmsi"] not in vessels or vessels[v["mmsi"]][1] != v["flag"]
             or vessels[v["mmsi"]][2] != v["type"] or vessels[v["mmsi"]][3] != v["length"]]
    check(not wrong, f"alla {len(facit['normal'])} fartyg finns med rätt flagga, typ och längd")
    flags = {vessels[v["mmsi"]][1] for v in facit["normal"] if v["mmsi"] in vessels}
    check(len(flags) >= 10, f"{len(flags)} olika flaggstater sparades (alla länder, inte bara nordiska)")
    check(all(vessels[v["mmsi"]][4] == "GOTEBORG" for v in facit["normal"]
              if v["mmsi"] in vessels and v["mmsi"] in vessels and vessels[v["mmsi"]][4]),
          "destination sparades där den fanns")

    print("\nUtgleshningen:")
    sent = facit["sent_positions"]
    moving = [v for v in facit["normal"] if v["moving"]]
    still = [v for v in facit["normal"] if not v["moving"]]
    for label, group, lo, hi in (("fartyg i fart", moving, 4, 30), ("fartyg som ligger stilla", still, 2, 8)):
        counts = [pos.get(v["mmsi"], 0) for v in group]
        check(all(lo <= c <= hi for c in counts), f"{label}: {min(counts)}–{max(counts)} sparade per fartyg (väntat {lo}–{hi})")
    total_sent = sum(sent.get(v["mmsi"], 0) for v in facit["normal"])
    total_kept = sum(pos.get(v["mmsi"], 0) for v in facit["normal"])
    check(total_kept < total_sent / 4, f"{total_kept} av {total_sent} skickade positioner sparades (mindre än en fjärdedel)")

    print("\nFartyg som aldrig ska sparas:")
    leak = [m for m in facit["excluded"] if m in vessels or pos.get(m)]
    check(not leak, "inga uppgifter eller positioner finns kvar för räddnings- och militära fartyg")
    check(set(facit["excluded"]) <= set(excluded), "alla uteslutna fartyg ligger i excluded_vessels")
    check({excluded[m] for m in facit["excluded"] if m in excluded} == {"ais_type:35", "ais_type:51", "ais_type:55", "ais_type:58"},
          f"orsakerna är rätt typkoder ({sorted(set(excluded.values()))})")
    late = [m for m in facit["excluded"] if sent.get(m, 0) > 0]
    check(len(late) == len(facit["excluded"]), "positioner skickades för alla uteslutna, även dem vars typ kom sent")
    junk = [m for m in facit["noise"] + facit["bad"] if m in vessels or pos.get(m)]
    check(not junk, "kuststation, sjömärke, flygplan, okänd landskod och ogiltiga koordinater sparades inte")

    print("\nÅteranslutning och drift:")
    check(facit["connections"] >= 2, f"anslutningen bröts och återupptogs ({facit['connections']} anslutningar)")
    check(run[5] >= 1, f"återanslutningar räknades i collector_runs ({run[5]})")
    check(span is not None and span > args.seconds - 6, f"positioner sparades över hela körningen ({span:.0f} s)")
    check(run[6] is not None, "körningen markerades som avslutad")
    check(run[3] > 0 and run[4] > 0, f"uteslutna ({run[3]}) och ogiltiga ({run[4]}) meddelanden räknades")
    check(run[1] == sum(pos.values()), f"stored_positions ({run[1]}) stämmer med antalet rader ({sum(pos.values())})")

    print(f"\n{'ALLT GODKÄNT' if not failures else str(len(failures)) + ' FEL'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
