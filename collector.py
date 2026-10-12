#!/usr/bin/env python3
"""collector.py: räknar fartyg per nordisk flagga ur AIS-data från aisstream.io.

Så här fungerar det:
  1. Lyssnar på aisstream.io i några minuter och sparar fartygens statiska
     AIS-meddelanden (typ 5: namn, IMO-nummer, fartygstyp och mått).
  2. Härleder flagga ur landskoden (MID) i MMSI-numret.
  3. Slår upp bruttodräktighet (GT) via IMO-numret i gt_register.csv.
  4. Skriver docs/data.json, som docs/index.html visar.
  5. Lägger en rad per körning i docs/history.json (antal per land, fartygstyp
     och längdklass), så att utveckling över tid kan visas. Historik går inte
     att hämta i efterhand, eftersom aisstream.io inte spelar upp gammal data.

Exempel:
  python3 collector.py --minutes 10      # prova i 10 minuter
  python3 collector.py --minutes 0       # räkna om utan att lyssna
  python3 collector.py --area nordic     # bara nordiska farvatten
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_URL = "wss://stream.aisstream.io/v0/stream"

# Landskod (MID) = de tre första siffrorna i ett fartygs MMSI-nummer.
# Kontrollera mot ITU:s MID-lista om du lägger till länder.
COUNTRIES = {
    "SE": {"name": "Sverige", "mids": ["265", "266"]},
    "NO": {"name": "Norge", "mids": ["257", "258", "259"]},
    "DK": {"name": "Danmark", "mids": ["219", "220"]},
    "FI": {"name": "Finland", "mids": ["230"]},
    "IS": {"name": "Island", "mids": ["251"]},
}
MID_TO_CODE = {mid: code for code, c in COUNTRIES.items() for mid in c["mids"]}

# Områden som två motstående hörn: [[lat, lon], [lat, lon]].
AREAS = {
    "global": [[[-90.0, -180.0], [90.0, 180.0]]],
    "nordic": [[[52.0, -12.0], [72.0, 32.0]], [[62.0, -25.0], [67.5, -12.0]]],
}


# Fartygstyper efter AIS-koden (ITU-R M.1371). Klasserna är grova med flit:
# koden anges av besättningen och är inte alltid rätt.
TYPE_CLASSES = ("passenger", "cargo", "tanker", "fishing", "highspeed",
                "special", "pleasure", "other", "unknown")
# Längdklasser i meter, ur måtten A+B i AIS.
LENGTH_BUCKETS = ("unknown", "lt40", "40-99", "100-199", "ge200")


class CollectError(RuntimeError):
    """Insamlingen kunde inte fortsätta."""


# ---------------------------------------------------------------- hjälpfunktioner

def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def load_env(path: Path) -> None:
    """Läser KEY=värde-rader ur .env (ingen extern modul behövs)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


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


def valid_imo(value) -> bool:
    """Kontrollsiffran i ett IMO-nummer: sju siffror, viktad summa av de sex första."""
    s = str(value or "")
    if len(s) != 7 or not s.isdigit():
        return False
    total = sum(int(d) * w for d, w in zip(s[:6], range(7, 1, -1)))
    return total % 10 == int(s[6])


def to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def ship_class(code) -> str:
    """AIS-fartygstyp (tvåsiffrig kod) till en av TYPE_CLASSES."""
    code = to_int(code)
    if code == 0:
        return "unknown"
    if 60 <= code <= 69:
        return "passenger"
    if 70 <= code <= 79:
        return "cargo"
    if 80 <= code <= 89:
        return "tanker"
    if code == 30:
        return "fishing"
    if 40 <= code <= 49:
        return "highspeed"
    if code in (36, 37):
        return "pleasure"
    if 31 <= code <= 35 or 50 <= code <= 59:  # bogsering, bagger, lots, räddning, myndighet m.m.
        return "special"
    return "other"


def length_bucket(length) -> str:
    """Längd i meter till en av LENGTH_BUCKETS."""
    n = to_int(length)
    if n <= 0:
        return "unknown"
    if n < 40:
        return "lt40"
    if n < 100:
        return "40-99"
    if n < 200:
        return "100-199"
    return "ge200"


def parse_static(msg) -> dict | None:
    """Plockar ut det vi behöver ur ett ShipStaticData-meddelande, eller None."""
    if not isinstance(msg, dict) or ci_get(msg, "MessageType") != "ShipStaticData":
        return None
    meta = ci_get(msg, "MetaData") or ci_get(msg, "Metadata") or {}
    body = ci_get(ci_get(msg, "Message") or {}, "ShipStaticData") or {}

    mmsi = str(to_int(ci_get(body, "UserID") or ci_get(meta, "MMSI")))
    # Fartyg har nio siffror. Kuststationer, flygplan och sjömärken har andra former.
    if len(mmsi) != 9 or mmsi[:3] not in MID_TO_CODE:
        return None

    dim = ci_get(body, "Dimension") or {}
    length = to_int(ci_get(dim, "A")) + to_int(ci_get(dim, "B"))
    beam = to_int(ci_get(dim, "C")) + to_int(ci_get(dim, "D"))

    imo = to_int(ci_get(body, "ImoNumber"))
    ship_type = to_int(ci_get(body, "Type"))
    name = str(ci_get(body, "Name") or ci_get(meta, "ShipName") or "").strip()

    return {
        "mmsi": mmsi,
        "flag": MID_TO_CODE[mmsi[:3]],
        "name": name or None,
        "imo": imo if valid_imo(imo) else None,  # felaktiga IMO-nummer ignoreras
        "ship_type": ship_type or None,
        "length": length or None,
        "beam": beam or None,
    }


def merge(vessels: dict, rec: dict, stamp: str) -> None:
    cur = vessels.get(rec["mmsi"])
    if cur is None:
        vessels[rec["mmsi"]] = {**rec, "first_seen": stamp, "last_seen": stamp}
        return
    for key in ("name", "imo", "ship_type", "length", "beam"):
        if rec.get(key):
            cur[key] = rec[key]
    cur["last_seen"] = stamp


def load_gt_register(path: Path) -> dict[int, float]:
    """CSV med kolumnerna imo och gt. Både komma och semikolon fungerar."""
    if not path.exists():
        return {}
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines:
        return {}
    delimiter = ";" if lines[0].count(";") > lines[0].count(",") else ","
    register: dict[int, float] = {}
    for row in csv.DictReader(lines, delimiter=delimiter):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        imo, gt = row.get("imo", ""), row.get("gt", "").replace(" ", "").replace(" ", "")
        if not imo.isdigit() or not gt:
            continue
        try:
            register[int(imo)] = float(gt.replace(",", "."))
        except ValueError:
            continue
    return register


def load_state(path: Path) -> tuple[dict, bool | None, str | None]:
    """Sedda fartyg från tidigare körningar, om de kom från en testserver och när
    insamlingen startade (äldre filer saknar tidpunkten, då uppskattas den)."""
    if not path.exists():
        return {}, None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        vessels = dict(data.get("vessels", {}))
        started = data.get("started")
        if not started and vessels:
            started = min(v.get("first_seen") or v["last_seen"] for v in vessels.values())
        return vessels, bool(data.get("sample", False)), started
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print(f"Varning: kunde inte läsa {path}. Börjar om med tomt läge.", file=sys.stderr)
        return {}, None, None


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- räkning

def build_report(vessels: dict, gt_by_imo: dict, args, sample: bool, now: datetime) -> dict:
    cutoff = now - timedelta(days=args.window_days)
    keys = ("seen", "confirmed_ge", "confirmed_lt", "likely_ge", "likely_lt", "unknown")
    stats = {
        code: {**dict.fromkeys(keys, 0),
               "types": dict.fromkeys(TYPE_CLASSES, 0),
               "lengths": dict.fromkeys(LENGTH_BUCKETS, 0)}
        for code in COUNTRIES
    }

    for v in vessels.values():
        if datetime.fromisoformat(v["last_seen"]) < cutoff:
            continue
        s = stats[v["flag"]]
        s["seen"] += 1
        s["types"][ship_class(v.get("ship_type"))] += 1
        s["lengths"][length_bucket(v.get("length"))] += 1
        gt = gt_by_imo.get(v["imo"]) if v.get("imo") else None
        if gt is not None:  # bruttodräktigheten är känd från registret
            s["confirmed_ge" if gt >= args.min_gt else "confirmed_lt"] += 1
        elif not v.get("length"):  # varken GT eller mått
            s["unknown"] += 1
        elif v["length"] >= args.proxy_length:  # grov uppskattning från längd
            s["likely_ge"] += 1
        else:
            s["likely_lt"] += 1

    return {
        "generated_at": now.isoformat(),
        "sample": sample,
        "area": args.area,
        "window_days": args.window_days,
        "min_gt": args.min_gt,
        "proxy_length_m": args.proxy_length,
        "gt_register_size": len(gt_by_imo),
        "countries": [
            {"code": code, "name": c["name"], "mids": c["mids"], **stats[code]}
            for code, c in COUNTRIES.items()
        ],
    }


def coverage_days(started: str | None, now: datetime, window_days: float) -> float | None:
    """Hur många dygn av räknefönstret som insamlingen faktiskt har pågått.
    Under uppbyggnaden är siffrorna lägre än de blir, och raden märks därefter."""
    try:
        began = datetime.fromisoformat(started)
        return round(min(window_days, max(0.0, (now - began).total_seconds() / 86400)), 2)
    except (TypeError, ValueError):
        return None


def make_snapshot(report: dict, minutes: float, received: int, coverage: float | None) -> dict:
    """En historikrad: samma siffror som i rapporten, plus hur de kom till."""
    keep = ("seen", "confirmed_ge", "confirmed_lt", "likely_ge", "likely_lt", "unknown", "types", "lengths")
    return {
        "t": report["generated_at"],
        "area": report["area"],
        "window_days": report["window_days"],
        "coverage_days": coverage,
        "listened_min": round(minutes, 1),
        "received": received,
        "min_gt": report["min_gt"],
        "proxy_length_m": report["proxy_length_m"],
        "gt_register_size": report["gt_register_size"],
        "countries": {c["code"]: {k: c[k] for k in keep} for c in report["countries"]},
    }


def read_history(path: Path) -> list | None:
    """Tidigare rader. Tom lista om filen saknas, None om den inte går att läsa
    (då rörs den inte, så att historiken aldrig skrivs över av misstag)."""
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))["snapshots"]
        return rows if isinstance(rows, list) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_history(path: Path, rows: list) -> None:
    """En rad per körning i filen, så att ändringarna syns tydligt i GitHub."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = ",\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text('{"version":1,"snapshots":[\n' + body + "\n]}\n", encoding="utf-8")
    os.replace(tmp, path)


def print_summary(report: dict) -> None:
    print()
    print(f"{'Land':<10}{'Sedda':>8}{'Bekräftade':>13}{'Troliga':>10}")
    for c in sorted(report["countries"], key=lambda c: -(c["confirmed_ge"] + c["likely_ge"])):
        print(f"{c['name']:<10}{c['seen']:>8}{c['confirmed_ge']:>13}{c['likely_ge']:>10}")
    if report["gt_register_size"] == 0:
        print("\nTips: gt_register.csv är tom, så alla siffror är uppskattningar från längd.")


# ---------------------------------------------------------------- insamling

async def collect(url: str, api_key: str, boxes: list, seconds: float, vessels: dict) -> tuple[int, int]:
    """Lyssnar på aisstream.io. Returnerar (mottagna meddelanden, nordiska träffar)."""
    import websockets  # importeras här så att --minutes 0 fungerar utan paketet

    subscription = json.dumps({
        "APIKey": api_key,
        "BoundingBoxes": boxes,
        "FilterMessageTypes": ["ShipStaticData"],
    })
    deadline = time.monotonic() + seconds
    received = matched = failures = 0
    backoff = 1.0
    next_progress = time.monotonic() + 60

    while time.monotonic() < deadline:
        confirmed = False
        try:
            # aisstream.io kräver komprimering (permessage-deflate) och en
            # prenumeration inom 3 sekunder efter att anslutningen öppnats.
            async with websockets.connect(url, compression="deflate", max_size=None, open_timeout=15) as ws:
                await ws.send(subscription)
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return received, matched
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=min(left, 30))
                    except asyncio.TimeoutError:
                        continue
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if ci_get(msg, "error"):
                        raise CollectError(f"aisstream.io svarade med felet: {ci_get(msg, 'error')}")

                    kind = ci_get(msg, "MessageType")
                    if kind == "SubscriptionConfirmation":
                        confirmed, failures, backoff = True, 0, 1.0
                        continue
                    if kind != "ShipStaticData":
                        continue
                    confirmed, failures, backoff = True, 0, 1.0
                    received += 1
                    rec = parse_static(msg)
                    if rec:
                        matched += 1
                        merge(vessels, rec, now_utc().isoformat())

                    if time.monotonic() >= next_progress:
                        print(f"  {received} meddelanden mottagna, {len(vessels)} nordiska fartyg hittills")
                        next_progress = time.monotonic() + 60
        except (websockets.WebSocketException, OSError, asyncio.TimeoutError) as exc:
            if not confirmed:
                failures += 1
                if failures >= 3:
                    raise CollectError(
                        "Fick ingen bekräftelse från aisstream.io efter tre försök. Kontrollera att "
                        "API-nyckeln stämmer, att du högst har tre anslutningar öppna och att datorn "
                        "når internet."
                    ) from exc
            wait = min(backoff, 30.0) * random.uniform(0.7, 1.3)
            wait = min(wait, max(0.0, deadline - time.monotonic()))
            print(f"Anslutningen föll ({type(exc).__name__}). Försöker igen om {wait:.0f} s.")
            await asyncio.sleep(wait)
            backoff *= 2
    return received, matched


# ---------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser(description="Räknar fartyg per nordisk flagga ur AIS-data.")
    p.add_argument("--minutes", type=float, default=30,
                   help="hur länge det ska lyssnas (0 = räkna bara om). Standard: 30")
    p.add_argument("--area", choices=sorted(AREAS), default="global",
                   help="global eller nordic. Standard: global")
    p.add_argument("--window-days", type=float, default=7,
                   help="räkna fartyg som setts de senaste N dygnen. Standard: 7")
    p.add_argument("--min-gt", type=int, default=300,
                   help="gräns för bruttodräktighet. Standard: 300")
    p.add_argument("--proxy-length", type=int, default=40,
                   help="längd i meter som grov uppskattning när GT saknas. Standard: 40")
    p.add_argument("--gt-file", default=str(ROOT / "gt_register.csv"),
                   help="CSV med kolumnerna imo och gt")
    p.add_argument("--state", default=str(ROOT / "data" / "vessels.json"),
                   help="var sedda fartyg sparas mellan körningar")
    p.add_argument("--out", default=str(ROOT / "docs" / "data.json"),
                   help="filen som webbsidan läser")
    p.add_argument("--history", default=None,
                   help="fil med en rad per körning. Standard: docs/history.json. Körningar mot "
                        "testserver sparas bara om du anger den här själv")
    p.add_argument("--no-history", action="store_true", help="spara ingen historikrad")
    p.add_argument("--url", default=None, help="annan websocket-adress (för test)")
    p.add_argument("--api-key", default=None, help="API-nyckel (annars AISSTREAM_API_KEY eller .env)")
    args = p.parse_args()

    load_env(ROOT / ".env")
    url = args.url or os.environ.get("AISSTREAM_URL") or DEFAULT_URL
    state_path, out_path = Path(args.state), Path(args.out)

    vessels, state_sample, started = load_state(state_path)
    gt_by_imo = load_gt_register(Path(args.gt_file))
    exit_code = 0
    received = matched = 0
    listened_s = 0.0

    if args.minutes > 0:
        api_key = args.api_key or os.environ.get("AISSTREAM_API_KEY", "")
        if not api_key:
            print(
                "Ingen API-nyckel hittades.\n"
                "  1. Skapa ett konto på https://aisstream.io och kopiera nyckeln.\n"
                "  2. Kopiera .env.example till .env och klistra in den efter AISSTREAM_API_KEY=",
                file=sys.stderr,
            )
            return 1

        sample = url != DEFAULT_URL  # data från en testserver märks som exempeldata
        if state_sample is not None and state_sample != sample:
            print("Obs: de sparade fartygen kommer från en annan källa (test eller riktig). "
                  "Börjar om med tomt läge.")
            vessels, started = {}, None

        print(f"Lyssnar i {args.minutes:g} minuter ({args.area}). Avbryt med Ctrl+C.")
        began_listening = time.monotonic()
        try:
            received, matched = asyncio.run(
                collect(url, api_key, AREAS[args.area], args.minutes * 60, vessels))
            print(f"Klart: {received} meddelanden mottagna, {matched} från nordiska fartyg.")
            if received == 0:
                print("Inga meddelanden kom in. Prova en längre tid eller kontrollera nyckeln.")
        except CollectError as exc:
            print(f"Fel: {exc}", file=sys.stderr)
            exit_code = 1
        except KeyboardInterrupt:
            print("\nAvbrutet. Sparar det som hunnit samlas in.")
        listened_s = time.monotonic() - began_listening
    else:
        sample = bool(state_sample)

    now = now_utc()
    cutoff = now - timedelta(days=args.window_days)
    vessels = {m: v for m, v in vessels.items() if datetime.fromisoformat(v["last_seen"]) >= cutoff}
    started = started or now.isoformat()  # ingen tidigare insamling: den börjar nu

    write_json(state_path, {"updated": now.isoformat(), "started": started,
                            "sample": sample, "vessels": vessels})
    report = build_report(vessels, gt_by_imo, args, sample, now)
    write_json(out_path, report)
    print_summary(report)
    print(f"\nSkrev {out_path}")

    if save_history(args, report, sample, exit_code, received, listened_s, started, now):
        exit_code = 1
    return exit_code


def save_history(args, report: dict, sample: bool, exit_code: int, received: int,
                 listened_s: float, started: str | None, now: datetime) -> bool:
    """Lägger till en historikrad när den är trovärdig. Returnerar True vid fel."""
    if args.no_history or args.minutes <= 0:
        return False
    if exit_code != 0 or received == 0:
        print("Ingen historikrad sparades eftersom insamlingen inte gav några nya meddelanden.")
        return False
    if sample and not args.history:
        print("Ingen historikrad sparades eftersom data kommer från en testserver. "
              "Ange --history om den ändå ska sparas.")
        return False

    path = Path(args.history) if args.history else ROOT / "docs" / "history.json"
    rows = read_history(path)
    if rows is None:
        print(f"Fel: kunde inte läsa {path}. Filen lämnas orörd och ingen historikrad sparades. "
              "Rätta eller flytta filen och kör igen.", file=sys.stderr)
        return True
    rows.append(make_snapshot(report, listened_s / 60, received,
                              coverage_days(started, now, args.window_days)))
    write_history(path, rows)
    print(f"Lade till rad {len(rows)} i {path}")
    return False


if __name__ == "__main__":
    sys.exit(main())
