#!/usr/bin/env python3
"""NORTH SCOPE: hamnarna (steg 3). Läser in hamnlistan och tar fram avgränsningar.

    python3 ports.py load --download        hamnar ur UN/LOCODE, cirkel runt varje koordinat
    python3 ports.py suggest                polygonförslag ur stillastående fartyg
    python3 ports.py apply --all            godkänn förslag som har tillräckligt underlag
    python3 ports.py apply SEGOT DKAAL      godkänn förslag för bestämda hamnar
    python3 ports.py geojson [KOD ...]      skriv förslag och nuvarande avgränsning för granskning på karta
    python3 ports.py stats                  översikt

Databasadressen läses ur DATABASE_URL (eller --database-url).

OBS: koordinaterna i UN/LOCODE anges i hela bågminuter (1-2 km) och är mitt i staden,
inte i hamnbassängen. Cirklarna är därför ett första grovt mått. Många stora hamnar saknar
koordinater, se data/port_extra.csv. Polygonerna ur verkliga positioner (suggest) är bättre
men måste granskas innan de godkänns.
"""
import argparse
import csv
import os
import re
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCODE_URL = "https://raw.githubusercontent.com/datasets/un-locode/main/data/code-list.csv"
LOCODE_CACHE = HERE / "data" / "unlocode_code-list.csv"
EXTRA_FILE = HERE / "data" / "port_extra.csv"
DEFAULT_DB = "postgresql://north:north@db:5432/northscope"
NORDIC = ("SE", "NO", "DK", "FI", "IS")
COORD = re.compile(r"^(\d{2})(\d{2})([NS]) (\d{3})(\d{2})([EW])$")


def parse_coordinates(text: str):
    """'5743N 01158E' -> (57.7167, 11.9667), eller None."""
    m = COORD.match((text or "").strip())
    if not m:
        return None
    lat = int(m.group(1)) + int(m.group(2)) / 60
    lon = int(m.group(4)) + int(m.group(5)) / 60
    if m.group(3) == "S":
        lat = -lat
    if m.group(6) == "W":
        lon = -lon
    if lat > 90 or lon > 180 or int(m.group(2)) > 59 or int(m.group(5)) > 59:
        return None
    return round(lat, 5), round(lon, 5)


def read_locode(path: Path, default_radius: int):
    """(hamnar med koordinater, hamnar utan koordinater) ur UN/LOCODE-filen."""
    ports, missing = {}, {}
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if "1" not in (r.get("Function") or "") or r.get("Change") == "X":
                continue
            code = (r["Country"] + r["Location"]).strip()
            if len(code) != 5:
                continue
            name = (r.get("Name") or r.get("NameWoDiacritics") or code).strip()
            xy = parse_coordinates(r.get("Coordinates"))
            if xy:
                ports[code] = {"locode": code, "name": name, "country": r["Country"],
                               "lat": xy[0], "lon": xy[1], "radius_m": default_radius, "src": "unlocode"}
            else:
                missing[code] = name
    return ports, missing


def read_extra(path: Path, default_radius: int) -> dict:
    out = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            out[r["locode"].strip()] = {
                "locode": r["locode"].strip(), "name": r["name"].strip(), "country": r["country"].strip(),
                "lat": float(r["lat"]), "lon": float(r["lon"]),
                "radius_m": int(r.get("radius_m") or default_radius), "src": "manual"}
    return out


UPSERT = """
INSERT INTO ports (locode, name, country, geom, radius_m, area, coord_source, area_source)
VALUES (%(locode)s, %(name)s, %(country)s,
        ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), %(radius_m)s,
        ST_Buffer(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography, %(radius_m)s)::geometry,
        %(src)s, 'circle')
ON CONFLICT (locode) DO UPDATE SET
    name = EXCLUDED.name, country = EXCLUDED.country,
    geom = CASE WHEN ports.area_source = 'polygon' THEN ports.geom ELSE EXCLUDED.geom END,
    radius_m = CASE WHEN ports.area_source = 'polygon' THEN ports.radius_m ELSE EXCLUDED.radius_m END,
    area = CASE WHEN ports.area_source = 'polygon' THEN ports.area ELSE EXCLUDED.area END,
    coord_source = CASE WHEN ports.area_source = 'polygon' THEN ports.coord_source ELSE EXCLUDED.coord_source END
"""

# Stillastående positioner nära varje hamn (varje position hör till närmaste hamn).
# Fartyg som anger att de ligger för ankar (status 1) räknas inte: ankarplatser är inte kajplatser.
SUGGEST = """
WITH still AS (
    SELECT DISTINCT ON (p.mmsi, p.ts) po.locode, p.mmsi, p.geom
    FROM ports po
    JOIN positions p ON ST_DWithin(p.geom, po.geom, %(deg)s)
    WHERE po.area_source = 'circle'
      AND p.ts >= now() - %(days)s * interval '1 day'
      AND p.sog_kn < 0.5 AND p.nav_status IS DISTINCT FROM 1
      AND ST_DWithin(p.geom::geography, po.geom::geography, %(search_m)s)
    ORDER BY p.mmsi, p.ts, ST_Distance(p.geom, po.geom)
), agg AS (
    SELECT locode, count(*) AS n, count(DISTINCT mmsi) AS vessels,
           ST_ConvexHull(ST_Collect(geom)) AS hull
    FROM still GROUP BY locode
    HAVING count(*) >= %(min_pos)s AND count(DISTINCT mmsi) >= %(min_vessels)s
)
UPDATE ports SET
    area_suggested = ST_Buffer(agg.hull::geography, %(buffer_m)s)::geometry,
    suggested_support = agg.n, suggested_vessels = agg.vessels, suggested_at = now()
FROM agg WHERE ports.locode = agg.locode
RETURNING ports.locode, ports.name, agg.n, agg.vessels,
          round((ST_Area(ports.area::geography) / 1e6)::numeric, 2),
          round((ST_Area(ports.area_suggested::geography) / 1e6)::numeric, 2)
"""

APPLY = """
UPDATE ports SET area = area_suggested, area_source = 'polygon'
WHERE area_suggested IS NOT NULL AND area_source = 'circle'
  AND suggested_vessels >= %(min_vessels)s AND suggested_support >= %(min_pos)s
  AND ST_Area(area_suggested::geography) BETWEEN %(min_km2)s * 1e6 AND %(max_km2)s * 1e6
  AND (%(all)s OR locode = ANY(%(codes)s))
RETURNING locode, name, suggested_support, suggested_vessels,
          round((ST_Area(area::geography) / 1e6)::numeric, 2)
"""


def connect(url):
    import psycopg
    return psycopg.connect(url, autocommit=False)


def cmd_load(args) -> int:
    path = Path(args.file) if args.file else LOCODE_CACHE
    if args.download:
        print(f"Hämtar {LOCODE_URL} ...")
        with urllib.request.urlopen(LOCODE_URL, timeout=60) as r:
            LOCODE_CACHE.write_bytes(r.read())
        path = LOCODE_CACHE
    if not path.exists():
        print(f"Hittar inte {path}. Kör med --download eller ange --file.", file=sys.stderr)
        return 1
    ports, missing = read_locode(path, args.radius)
    extra = read_extra(Path(args.extra), args.radius)
    ports.update(extra)
    still_missing = {c: n for c, n in missing.items() if c not in ports}
    with connect(args.database_url) as conn, conn.cursor() as cur:
        cur.executemany(UPSERT, list(ports.values()))
    print(f"{len(ports)} hamnar inlästa ({len(extra)} med koordinater ur port_extra.csv).")
    nordic = sorted(f"{c} {n}" for c, n in still_missing.items() if c[:2] in NORDIC)
    print(f"{len(still_missing)} hamnar saknar koordinater och är inte med, varav {len(nordic)} nordiska.")
    if nordic:
        print("Nordiska hamnar utan koordinater (lägg de viktiga i data/port_extra.csv):")
        for i in range(0, min(len(nordic), 60), 4):
            print("  " + " | ".join(nordic[i:i + 4]))
    return 0


def cmd_suggest(args) -> int:
    deg = args.search_m / 111320 / 0.3  # grov förfiltrering som gäller upp till 72 graders latitud
    params = dict(deg=deg, days=args.days, search_m=args.search_m, min_pos=args.min_positions,
                  min_vessels=args.min_vessels, buffer_m=args.buffer_m)
    with connect(args.database_url) as conn, conn.cursor() as cur:
        cur.execute(SUGGEST, params)
        rows = cur.fetchall()
    if not rows:
        print("Inga hamnar har tillräckligt många stillastående positioner än "
              f"(minst {args.min_positions} positioner från {args.min_vessels} fartyg). Vänta på mer data.")
        return 0
    print(f"{'Kod':<7}{'Hamn':<24}{'Positioner':>11}{'Fartyg':>8}{'Cirkel km²':>12}{'Förslag km²':>13}")
    for code, name, n, vessels, circle, suggested in sorted(rows):
        print(f"{code:<7}{name[:23]:<24}{n:>11}{vessels:>8}{circle:>12}{suggested:>13}")
    print("\nGranska förslagen på en karta innan du godkänner dem med: python3 ports.py apply --all")
    return 0


def cmd_apply(args) -> int:
    if not args.all and not args.codes:
        print("Ange --all eller en eller flera hamnkoder.", file=sys.stderr)
        return 1
    params = dict(min_vessels=args.min_vessels, min_pos=args.min_positions, min_km2=args.min_km2,
                  max_km2=args.max_km2, all=args.all, codes=args.codes)
    with connect(args.database_url) as conn, conn.cursor() as cur:
        cur.execute(APPLY, params)
        rows = cur.fetchall()
    for code, name, n, vessels, km2 in sorted(rows):
        print(f"  {code} {name}: polygon godkänd ({km2} km², {n} positioner från {vessels} fartyg)")
    print(f"{len(rows)} hamnar fick polygon.")
    return 0


GEOJSON = """
WITH sel AS (
    SELECT * FROM ports
    WHERE CASE WHEN %(by_codes)s THEN locode = ANY(%(codes)s) ELSE area_suggested IS NOT NULL END
)
SELECT json_build_object('type', 'FeatureCollection', 'features', coalesce(json_agg(f), '[]'::json))
FROM (
    SELECT json_build_object('type', 'Feature', 'geometry', ST_AsGeoJSON(area)::json,
        'properties', json_build_object('locode', locode, 'name', name,
                                        'kind', 'nuvarande ' || area_source, 'stroke', '#2a78d6')) AS f
    FROM sel
    UNION ALL
    SELECT json_build_object('type', 'Feature', 'geometry', ST_AsGeoJSON(area_suggested)::json,
        'properties', json_build_object('locode', locode, 'name', name, 'kind', 'förslag',
            'positioner', suggested_support, 'fartyg', suggested_vessels, 'stroke', '#eb6834'))
    FROM sel WHERE area_suggested IS NOT NULL
) t
"""


def cmd_geojson(args) -> int:
    import json
    params = dict(by_codes=bool(args.codes), codes=args.codes)
    with connect(args.database_url) as conn, conn.cursor() as cur:
        cur.execute(GEOJSON, params)
        data = cur.fetchone()[0]
    Path(args.out).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    n = len(data["features"])
    print(f"Skrev {n} objekt till {args.out}. Öppna filen på https://geojson.io för att granska dem.")
    return 0


def cmd_stats(args) -> int:
    with connect(args.database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT area_source, count(*) FROM ports GROUP BY 1 ORDER BY 1")
        print("Hamnar efter avgränsning:", dict(cur.fetchall()))
        cur.execute("SELECT count(*) FROM ports WHERE area_suggested IS NOT NULL AND area_source = 'circle'")
        print("Polygonförslag som väntar på granskning:", cur.fetchone()[0])
        cur.execute("SELECT count(*), count(*) FILTER (WHERE departed IS NULL), "
                    "count(*) FILTER (WHERE NOT arrival_known) FROM port_calls")
        total, open_, unknown = cur.fetchone()
        print(f"Anlöp: {total} (varav {open_} pågår, {unknown} med okänd ankomst)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="NORTH SCOPE: hamnar och avgränsningar.")
    p.add_argument("--database-url", default=os.environ.get("DATABASE_URL", DEFAULT_DB))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("load", help="läs in hamnar ur UN/LOCODE")
    s.add_argument("--download", action="store_true", help="hämta UN/LOCODE från GitHub först")
    s.add_argument("--file", default=None, help="UN/LOCODE-fil (annars den som hämtades senast)")
    s.add_argument("--extra", default=str(EXTRA_FILE))
    s.add_argument("--radius", type=int, default=3000, help="cirkelns radie i meter")
    s.set_defaults(fn=cmd_load)

    s = sub.add_parser("suggest", help="föreslå polygoner ur stillastående positioner")
    s.add_argument("--days", type=float, default=14)
    s.add_argument("--search-m", type=float, default=6000)
    s.add_argument("--min-positions", type=int, default=30)
    s.add_argument("--min-vessels", type=int, default=5)
    s.add_argument("--buffer-m", type=float, default=250)
    s.set_defaults(fn=cmd_suggest)

    s = sub.add_parser("apply", help="godkänn polygonförslag")
    s.add_argument("codes", nargs="*")
    s.add_argument("--all", action="store_true")
    s.add_argument("--min-positions", type=int, default=50)
    s.add_argument("--min-vessels", type=int, default=8)
    s.add_argument("--min-km2", type=float, default=0.05)
    s.add_argument("--max-km2", type=float, default=60.0)
    s.set_defaults(fn=cmd_apply)

    s = sub.add_parser("geojson", help="skriv nuvarande avgränsning och förslag som GeoJSON för granskning")
    s.add_argument("codes", nargs="*", help="hamnkoder (annars alla hamnar med förslag)")
    s.add_argument("--out", default="hamnar.geojson")
    s.set_defaults(fn=cmd_geojson)

    s = sub.add_parser("stats", help="översikt")
    s.set_defaults(fn=cmd_stats)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
