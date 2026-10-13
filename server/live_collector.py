#!/usr/bin/env python3
"""NORTH SCOPE: insamling som lyssnar hela tiden och sparar i PostgreSQL med PostGIS.

Skriptet läser AIS-strömmen från aisstream.io och sparar
  - fartygens uppgifter (flagga ur MMSI, typ, mått, destination) i tabellen vessels
  - utglesade positioner i tabellen positions
för alla flaggstater och alla fartygstyper, utom räddnings- och militära fartyg som
aldrig sparas (se EXCLUDED_TYPES).

Starta med docker compose (se server/README.md) eller direkt:
    python3 live_collector.py --area nordeuropa

API-nyckeln läses ur AISSTREAM_API_KEY och skrivs aldrig ut.
"""
import argparse
import asyncio
import csv
import json
import math
import os
import random
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_URL = "wss://stream.aisstream.io/v0/stream"
DEFAULT_DB = "postgresql://north:north@db:5432/northscope"

# Områden som två motstående hörn: [[lat, lon], [lat, lon]].
AREAS = {
    "nordeuropa": [[[50.0, -5.0], [66.0, 31.0]]],  # Nordsjön, Skagerrak, Kattegatt, Östersjön
    "nordic": [[[52.0, -12.0], [72.0, 32.0]], [[62.0, -25.0], [67.5, -12.0]]],
    "global": [[[-90.0, -180.0], [90.0, 180.0]]],
}

# AIS-fartygstyper som aldrig sparas: 35 militär, 51 sök och räddning,
# 55 polis och kustbevakning, 58 sjukvårdstransport. Koderna ska kontrolleras mot
# ITU-R M.1371 innan sidan görs öppen.
EXCLUDED_TYPES = frozenset({35, 51, 55, 58})

POSITION_KINDS = ("PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport")
STATIC_KINDS = ("ShipStaticData", "StaticDataReport")
SUBSCRIBED_KINDS = list(POSITION_KINDS + STATIC_KINDS)

MAX_BUFFER = 500_000  # positioner i minnet om databasen inte svarar


class FatalError(RuntimeError):
    """Något som inte går att lösa med nya försök, till exempel fel API-nyckel."""


def log(text: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z  {text}", flush=True)


# ---------------------------------------------------------------- hjälpfunktioner

def ci_get(d, key, default=None):
    """Hämtar nyckel ur en dict, även om skiftläget skiljer sig."""
    if not isinstance(d, dict):
        return default
    if key in d:
        return d[key]
    lowered = key.lower()
    for k, v in d.items():
        if isinstance(k, str) and k.lower() == lowered:
            return v
    return default


def to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def to_float(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def load_mid_map(path: Path) -> dict[str, tuple[str, str]]:
    """mid -> (iso2, namn) ur CSV-filen."""
    with open(path, encoding="utf-8", newline="") as f:
        return {r["mid"]: (r["iso2"], r["name"]) for r in csv.DictReader(f)}


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def angle_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def valid_mmsi(mmsi: str, mids: dict) -> bool:
    """Fartyg har nio siffror och en känd landskod. Kuststationer, flygplan och
    sjömärken har andra former och sorteras bort."""
    return len(mmsi) == 9 and mmsi.isdigit() and mmsi[:3] in mids


# ---------------------------------------------------------------- tolkning av meddelanden

def message_parts(msg):
    """(typ, meta, kropp) ur ett aisstream-meddelande."""
    kind = ci_get(msg, "MessageType")
    meta = ci_get(msg, "MetaData") or ci_get(msg, "Metadata") or {}
    body = ci_get(ci_get(msg, "Message") or {}, kind) if kind else None
    return kind, meta, (body if isinstance(body, dict) else {})


def parse_position(msg, mids: dict) -> dict | None:
    """Position ur ett meddelande, eller None om det är ogiltigt."""
    kind, meta, body = message_parts(msg)
    if kind not in POSITION_KINDS:
        return None
    mmsi = str(to_int(ci_get(body, "UserID") or ci_get(meta, "MMSI")))
    if not valid_mmsi(mmsi, mids):
        return None
    lat = to_float(ci_get(body, "Latitude", ci_get(meta, "latitude")))
    lon = to_float(ci_get(body, "Longitude", ci_get(meta, "longitude")))
    if lat is None or lon is None:
        return None
    # 91 och 181 betyder "saknas" i AIS. (0, 0) är nästan alltid ett fel.
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or lat == 91 or lon == 181:
        return None
    if lat == 0 and lon == 0:
        return None
    sog = to_float(ci_get(body, "Sog"))
    if sog is not None and (sog < 0 or sog >= 102.3):  # 102,3 betyder "saknas"
        sog = None
    cog = to_float(ci_get(body, "Cog"))
    if cog is not None and not (0 <= cog < 360):  # 360 betyder "saknas"
        cog = None
    status = to_int(ci_get(body, "NavigationalStatus"))
    return {"mmsi": mmsi, "lat": lat, "lon": lon, "sog": sog, "cog": cog,
            "nav_status": status if 0 <= status <= 15 else None}


def eta_text(eta) -> str | None:
    month, day = to_int(ci_get(eta, "Month")), to_int(ci_get(eta, "Day"))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    hour, minute = to_int(ci_get(eta, "Hour")), to_int(ci_get(eta, "Minute"))
    if hour > 23 or minute > 59:
        return f"{month:02d}-{day:02d}"
    return f"{month:02d}-{day:02d} {hour:02d}:{minute:02d}"


def positive(value):
    n = to_int(value)
    return n if n > 0 else None


def parse_static(msg, mids: dict) -> dict | None:
    """Fartygets uppgifter ur ShipStaticData (klass A) eller StaticDataReport (klass B)."""
    kind, meta, body = message_parts(msg)
    if kind not in STATIC_KINDS:
        return None
    mmsi = str(to_int(ci_get(body, "UserID") or ci_get(meta, "MMSI")))
    if not valid_mmsi(mmsi, mids):
        return None
    rec = {"mmsi": mmsi, "imo": None, "name": None, "call_sign": None, "ship_type": None,
           "length_m": None, "beam_m": None, "draught_m": None, "destination": None, "eta": None}
    meta_name = str(ci_get(meta, "ShipName") or "").strip() or None

    if kind == "ShipStaticData":
        dim = ci_get(body, "Dimension") or {}
        imo = to_int(ci_get(body, "ImoNumber"))
        draught = to_float(ci_get(body, "MaximumStaticDraught"))
        rec.update({
            "imo": imo if imo > 0 else None,
            "name": str(ci_get(body, "Name") or "").strip() or meta_name,
            "call_sign": str(ci_get(body, "CallSign") or "").strip() or None,
            "ship_type": positive(ci_get(body, "Type")),
            "length_m": positive(to_int(ci_get(dim, "A")) + to_int(ci_get(dim, "B"))),
            "beam_m": positive(to_int(ci_get(dim, "C")) + to_int(ci_get(dim, "D"))),
            "draught_m": draught if draught and draught > 0 else None,
            "destination": str(ci_get(body, "Destination") or "").strip() or None,
            "eta": eta_text(ci_get(body, "Eta")),
        })
    else:  # StaticDataReport: del A har namnet, del B typ och mått
        part_a, part_b = ci_get(body, "ReportA") or {}, ci_get(body, "ReportB") or {}
        dim = ci_get(part_b, "Dimension") or {}
        rec.update({
            "name": str(ci_get(part_a, "Name") or "").strip() or meta_name,
            "call_sign": str(ci_get(part_b, "CallSign") or "").strip() or None,
            "ship_type": positive(ci_get(part_b, "ShipType")),
            "length_m": positive(to_int(ci_get(dim, "A")) + to_int(ci_get(dim, "B"))),
            "beam_m": positive(to_int(ci_get(dim, "C")) + to_int(ci_get(dim, "D"))),
        })
    return rec


# ---------------------------------------------------------------- utglesning

class Thinner:
    """Avgör vilka positioner som sparas. En position sparas när
      - fartyget inte har sparats förut, eller
      - det gått max_age sedan förra sparade (kort tid i fart, lång tid vid kaj), eller
      - kursen ändrats minst turn_deg (och det gått min_gap), eller
      - fartyget flyttat sig minst move_m (och det gått min_gap).
    Allt annat glesas bort. Tiderna räknas på mottagningstiden."""

    def __init__(self, min_gap_s=30.0, moving_max_s=180.0, still_max_s=1800.0,
                 still_kn=0.5, turn_deg=25.0, move_m=1000.0):
        self.min_gap_s, self.moving_max_s, self.still_max_s = min_gap_s, moving_max_s, still_max_s
        self.still_kn, self.turn_deg, self.move_m = still_kn, turn_deg, move_m
        self.last: dict[str, tuple] = {}  # mmsi -> (t, lat, lon, cog)

    def accept(self, mmsi, t, lat, lon, sog, cog) -> bool:
        prev = self.last.get(mmsi)
        keep = prev is None
        if not keep:
            pt, plat, plon, pcog = prev
            age = t - pt
            if age < self.min_gap_s:
                return False
            still = sog is not None and sog < self.still_kn
            if age >= (self.still_max_s if still else self.moving_max_s):
                keep = True
            elif not still and cog is not None and pcog is not None \
                    and angle_diff(cog, pcog) >= self.turn_deg:
                keep = True
            elif haversine_m(plat, plon, lat, lon) >= self.move_m:
                keep = True
        if keep:
            self.last[mmsi] = (t, lat, lon, cog)
        return keep

    def forget(self, mmsi) -> None:
        self.last.pop(mmsi, None)

    def prune(self, t, older_than_s=7200.0) -> None:
        for mmsi in [m for m, v in self.last.items() if t - v[0] > older_than_s]:
            del self.last[mmsi]


# ---------------------------------------------------------------- tillstånd och skrivning

class Stats:
    def __init__(self):
        self.received = self.stored = self.thinned = self.excluded = 0
        self.rejected = self.reconnects = self.dropped_buffer = 0


class State:
    def __init__(self, mids, thinner, excluded_types=EXCLUDED_TYPES):
        self.mids = mids
        self.iso = {m: v[0] for m, v in mids.items()}
        self.thinner = thinner
        self.excluded_types = excluded_types
        self.excluded: set[str] = set()
        self.stats = Stats()
        self.positions: list[tuple] = []            # väntar på att skrivas
        self.vessels: dict[str, dict] = {}           # mmsi -> uppgifter som väntar på att skrivas
        self.new_excluded: dict[str, str] = {}       # mmsi -> orsak, väntar på att skrivas
        self.last_message = time.monotonic()

    def touch_vessel(self, mmsi, now_dt, rec=None):
        cur = self.vessels.get(mmsi)
        if cur is None:
            cur = {"mmsi": mmsi, "last_seen": now_dt, "static_updated": None}
            self.vessels[mmsi] = cur
        cur["last_seen"] = now_dt
        if rec:
            for key, value in rec.items():
                if key != "mmsi" and value is not None:
                    cur[key] = value
            cur["static_updated"] = now_dt

    def exclude(self, mmsi, reason):
        self.excluded.add(mmsi)
        self.new_excluded[mmsi] = reason
        self.vessels.pop(mmsi, None)
        self.thinner.forget(mmsi)
        self.positions = [p for p in self.positions if p[0] != mmsi]

    def handle(self, msg, t: float) -> None:
        """Tar hand om ett meddelande. t = mottagningstid i sekunder (epoch)."""
        kind = ci_get(msg, "MessageType")
        if kind in POSITION_KINDS:
            self.stats.received += 1
            pos = parse_position(msg, self.mids)
            if pos is None:
                self.stats.rejected += 1
                return
            mmsi = pos["mmsi"]
            if mmsi in self.excluded:
                self.stats.excluded += 1
                return
            now_dt = datetime.fromtimestamp(t, timezone.utc)
            self.touch_vessel(mmsi, now_dt)
            if not self.thinner.accept(mmsi, t, pos["lat"], pos["lon"], pos["sog"], pos["cog"]):
                self.stats.thinned += 1
                return
            if len(self.positions) >= MAX_BUFFER:
                self.stats.dropped_buffer += 1
                return
            self.positions.append((mmsi, now_dt, pos["lon"], pos["lat"], pos["sog"],
                                   pos["cog"], pos["nav_status"]))
        elif kind in STATIC_KINDS:
            self.stats.received += 1
            rec = parse_static(msg, self.mids)
            if rec is None:
                self.stats.rejected += 1
                return
            mmsi = rec["mmsi"]
            if mmsi in self.excluded:
                self.stats.excluded += 1
                return
            if rec["ship_type"] in self.excluded_types:
                self.exclude(mmsi, f"ais_type:{rec['ship_type']}")
                self.stats.excluded += 1
                return
            self.touch_vessel(mmsi, datetime.fromtimestamp(t, timezone.utc), rec)


UPSERT_VESSEL = """
INSERT INTO vessels (mmsi, mid, flag, imo, name, call_sign, ship_type, length_m, beam_m,
                     draught_m, destination, eta, first_seen, last_seen, static_updated)
VALUES (%(mmsi)s, %(mid)s, %(flag)s, %(imo)s, %(name)s, %(call_sign)s, %(ship_type)s,
        %(length_m)s, %(beam_m)s, %(draught_m)s, %(destination)s, %(eta)s,
        %(last_seen)s, %(last_seen)s, %(static_updated)s)
ON CONFLICT (mmsi) DO UPDATE SET
    imo = COALESCE(EXCLUDED.imo, vessels.imo),
    name = COALESCE(EXCLUDED.name, vessels.name),
    call_sign = COALESCE(EXCLUDED.call_sign, vessels.call_sign),
    ship_type = COALESCE(EXCLUDED.ship_type, vessels.ship_type),
    length_m = COALESCE(EXCLUDED.length_m, vessels.length_m),
    beam_m = COALESCE(EXCLUDED.beam_m, vessels.beam_m),
    draught_m = COALESCE(EXCLUDED.draught_m, vessels.draught_m),
    destination = COALESCE(EXCLUDED.destination, vessels.destination),
    eta = COALESCE(EXCLUDED.eta, vessels.eta),
    last_seen = GREATEST(vessels.last_seen, EXCLUDED.last_seen),
    static_updated = COALESCE(EXCLUDED.static_updated, vessels.static_updated)
"""
INSERT_POSITION = """
INSERT INTO positions (mmsi, ts, geom, sog_kn, cog_deg, nav_status)
VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s)
ON CONFLICT DO NOTHING
"""
VESSEL_FIELDS = ("imo", "name", "call_sign", "ship_type", "length_m", "beam_m",
                 "draught_m", "destination", "eta", "static_updated")


async def flush(conn, state: State, run_id: int, final=False) -> None:
    """Skriver det som väntar. Vid fel ligger det kvar till nästa försök."""
    positions, vessels, new_excluded = state.positions, state.vessels, state.new_excluded
    state.positions, state.vessels, state.new_excluded = [], {}, {}
    s = state.stats
    deleted = 0
    try:
        async with conn.transaction():
            async with conn.cursor() as cur:
                if new_excluded:
                    await cur.executemany(
                        "INSERT INTO excluded_vessels (mmsi, reason) VALUES (%s, %s) "
                        "ON CONFLICT DO NOTHING", list(new_excluded.items()))
                    ids = list(new_excluded)
                    await cur.execute("DELETE FROM positions WHERE mmsi = ANY(%s)", (ids,))
                    deleted = cur.rowcount  # sparade innan typen avslöjade fartyget
                    await cur.execute("DELETE FROM vessels WHERE mmsi = ANY(%s)", (ids,))
                rows = []
                for mmsi, v in vessels.items():
                    if mmsi in new_excluded or mmsi in state.excluded:
                        continue
                    row = {k: v.get(k) for k in VESSEL_FIELDS}
                    row.update(mmsi=mmsi, mid=mmsi[:3], flag=state.iso[mmsi[:3]],
                               last_seen=v["last_seen"])
                    rows.append(row)
                if rows:
                    await cur.executemany(UPSERT_VESSEL, rows)
                keep = [p for p in positions if p[0] not in state.excluded]
                if keep:
                    await cur.executemany(INSERT_POSITION, keep)
                await cur.execute(
                    "UPDATE collector_runs SET last_heartbeat = now(), received = %s, "
                    "stored_positions = %s, thinned_out = %s, excluded = %s, rejected = %s, "
                    "reconnects = %s, ended = CASE WHEN %s THEN now() ELSE ended END WHERE id = %s",
                    (s.received, s.stored - deleted + len(keep), s.thinned, s.excluded, s.rejected,
                     s.reconnects, final, run_id))
        s.stored += len(keep) - deleted
    except Exception:
        # Lägg tillbaka det som inte skrevs, före det som kommit in under tiden.
        state.positions = positions + state.positions
        for mmsi, v in vessels.items():
            cur_v = state.vessels.get(mmsi)
            if cur_v is None:
                state.vessels[mmsi] = v
            else:
                for k, val in v.items():
                    if k != "last_seen" and cur_v.get(k) is None:
                        cur_v[k] = val
        state.new_excluded = {**new_excluded, **state.new_excluded}
        raise


async def connect_db(url: str, attempts=10):
    import psycopg
    delay = 1.0
    for attempt in range(1, attempts + 1):
        try:
            return await psycopg.AsyncConnection.connect(url, autocommit=True)
        except psycopg.OperationalError as exc:
            if attempt == attempts:
                raise FatalError(f"Kunde inte nå databasen: {type(exc).__name__}") from exc
            log(f"Databasen svarar inte ännu (försök {attempt}). Provar igen om {delay:.0f} s.")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)


async def prepare_db(conn, state: State, mids: dict) -> None:
    """Fyller landslistan, läser uteslutna fartyg och startar en körningsrad."""
    async with conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO mid_countries (mid, iso2, name) VALUES (%s, %s, %s) "
            "ON CONFLICT (mid) DO UPDATE SET iso2 = EXCLUDED.iso2, name = EXCLUDED.name",
            [(m, v[0], v[1]) for m, v in mids.items()])
        await cur.execute("SELECT mmsi FROM excluded_vessels")
        state.excluded = {r[0] for r in await cur.fetchall()}


async def start_run(conn, area: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO collector_runs (started, last_heartbeat, area) VALUES (now(), now(), %s) "
            "RETURNING id", (area,))
        return (await cur.fetchone())[0]


# ---------------------------------------------------------------- lyssnare

async def listen(state: State, url: str, api_key: str, boxes: list, stop: asyncio.Event,
                 silence_s: float) -> None:
    import websockets

    subscription = json.dumps({"APIKey": api_key, "BoundingBoxes": boxes,
                               "FilterMessageTypes": SUBSCRIBED_KINDS})
    backoff, last_prune = 1.0, time.time()
    while not stop.is_set():
        try:
            # aisstream.io kräver komprimering och en prenumeration inom 3 sekunder.
            async with websockets.connect(url, compression="deflate", max_size=None,
                                          open_timeout=15) as ws:
                await ws.send(subscription)
                state.last_message = time.monotonic()
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=silence_s)
                    except asyncio.TimeoutError:
                        raise ConnectionError(f"Inga meddelanden på {silence_s:.0f} s")
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        state.stats.rejected += 1
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if ci_get(msg, "error"):
                        raise FatalError(f"aisstream.io svarade med felet: {ci_get(msg, 'error')}")
                    if ci_get(msg, "MessageType") == "SubscriptionConfirmation":
                        backoff = 1.0
                        log("Prenumerationen bekräftad.")
                        continue
                    backoff = 1.0
                    state.last_message = time.monotonic()
                    now = time.time()
                    state.handle(msg, now)
                    if now - last_prune > 600:
                        state.thinner.prune(now)
                        last_prune = now
        except FatalError:
            raise
        except (websockets.WebSocketException, OSError, ConnectionError) as exc:
            if stop.is_set():
                return
            state.stats.reconnects += 1
            wait = min(backoff, 60.0) * random.uniform(0.7, 1.3)
            log(f"Anslutningen föll ({type(exc).__name__}). Försöker igen om {wait:.0f} s.")
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            backoff *= 2


async def writer(box: list, db_url: str, state: State, run_id: int, stop: asyncio.Event,
                 flush_s: float) -> None:
    """Skriver till databasen med jämna mellanrum och loggar en statusrad varje minut.
    box[0] är databasanslutningen. Den ersätts om den har gått förlorad."""
    next_report = time.monotonic() + 60
    last_received = 0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=flush_s)
        except asyncio.TimeoutError:
            pass
        try:
            await flush(box[0], state, run_id)
        except Exception as exc:  # databasen kan vara nere en stund
            log(f"Kunde inte skriva till databasen ({type(exc).__name__}). {len(state.positions)} "
                f"positioner väntar. Försöker igen.")
            if box[0].closed or box[0].broken:
                try:
                    box[0] = await connect_db(db_url, attempts=1)
                except FatalError:
                    pass
        if time.monotonic() >= next_report:
            s = state.stats
            rate = (s.received - last_received) / 60
            last_received = s.received
            log(f"{s.received} meddelanden ({rate:.0f}/s), {s.stored} sparade positioner, "
                f"{s.thinned} utglesade, {s.excluded} uteslutna, {s.rejected} ogiltiga, "
                f"{len(state.positions)} väntar, {s.reconnects} återanslutningar")
            next_report = time.monotonic() + 60


async def run(args) -> int:
    api_key = args.api_key or os.environ.get("AISSTREAM_API_KEY", "")
    if not api_key:
        print("Ingen API-nyckel. Sätt AISSTREAM_API_KEY i miljön (docker compose läser den ur filen .env).", file=sys.stderr)
        return 1
    mids = load_mid_map(Path(args.mid_file))
    thinner = Thinner(min_gap_s=args.min_gap_s, moving_max_s=args.moving_max_s,
                      still_max_s=args.still_max_s, still_kn=args.still_kn,
                      turn_deg=args.turn_deg, move_m=args.move_m)
    state = State(mids, thinner)
    box = [await connect_db(args.database_url)]
    await prepare_db(box[0], state, mids)
    run_id = await start_run(box[0], args.area)
    log(f"Start. Område: {args.area}. {len(state.excluded)} uteslutna fartyg lästa ur databasen.")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    if args.seconds > 0:
        loop.call_later(args.seconds, stop.set)

    tasks = [asyncio.create_task(listen(state, args.url, api_key, AREAS[args.area], stop,
                                        args.silence_s)),
             asyncio.create_task(writer(box, args.database_url, state, run_id, stop, args.flush_s))]
    code = 0
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in done:
            exc = t.exception()
            if exc:
                raise exc
    except FatalError as exc:
        log(f"Avbryter: {exc}")
        code = 1
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await flush(box[0], state, run_id, final=True)
        except Exception as exc:
            log(f"Sista skrivningen misslyckades ({type(exc).__name__}).")
        await box[0].close()
    s = state.stats
    log(f"Klar. {s.received} meddelanden, {s.stored} sparade positioner, {s.excluded} uteslutna.")
    return code


def main() -> int:
    p = argparse.ArgumentParser(description="NORTH SCOPE: kontinuerlig AIS-insamling till PostGIS.")
    p.add_argument("--area", choices=sorted(AREAS), default=os.environ.get("AREA", "nordeuropa"))
    p.add_argument("--seconds", type=float, default=0, help="avsluta efter så här många sekunder (0 = kör tills den stoppas)")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--api-key", default=None, help="annars läses AISSTREAM_API_KEY")
    p.add_argument("--database-url", default=os.environ.get("DATABASE_URL", DEFAULT_DB))
    p.add_argument("--mid-file", default=str(HERE / "data" / "mid_countries.csv"))
    p.add_argument("--flush-s", type=float, default=5.0)
    p.add_argument("--silence-s", type=float, default=120.0, help="återanslut om inget kommer på så här lång tid")
    p.add_argument("--min-gap-s", type=float, default=30.0)
    p.add_argument("--moving-max-s", type=float, default=180.0)
    p.add_argument("--still-max-s", type=float, default=1800.0)
    p.add_argument("--still-kn", type=float, default=0.5)
    p.add_argument("--turn-deg", type=float, default=25.0)
    p.add_argument("--move-m", type=float, default=1000.0)
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except FatalError as exc:
        print(f"Avbryter: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
