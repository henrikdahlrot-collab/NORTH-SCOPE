#!/usr/bin/env python3
"""Låtsasversion av aisstream.io, så att du kan prova collector.py utan API-nyckel.

Startar en websocket-server på ws://localhost:8765 som kräver API-nyckeln "test-key"
och skickar påhittade ShipStaticData-meddelanden i samma form som aisstream.io.
Fartygen är påhittade. Skriver också tests/sample_gt_register.csv med påhittad
bruttodräktighet för en del av dem.

Kör i en terminal:
    python3 tests/fake_aisstream.py
och i en annan:
    python3 collector.py --url ws://localhost:8765 --api-key test-key --minutes 0.3 \
        --gt-file tests/sample_gt_register.csv --state data/demo.json
"""
import asyncio
import json
import math
import random
from pathlib import Path

import websockets

HOST, PORT, API_KEY = "localhost", 8765, "test-key"
HERE = Path(__file__).resolve().parent

# Antal påhittade fartyg per landskod (MID). Facit nedan hålls skilt från collector.py.
NORDIC = {"265": 45, "266": 15, "257": 90, "258": 30, "259": 20,
          "219": 70, "220": 40, "230": 70, "251": 12}
COUNTRY_OF_MID = {"265": "SE", "266": "SE", "257": "NO", "258": "NO", "259": "NO",
                  "219": "DK", "220": "DK", "230": "FI", "251": "IS"}
OTHER = {"211": 60, "244": 50, "636": 40, "538": 30, "563": 20}
MIN_GT, PROXY_LENGTH = 300, 40


def imo_number(rng, valid=True):
    digits = [rng.randint(5, 9)] + [rng.randint(0, 9) for _ in range(5)]
    check = sum(d * w for d, w in zip(digits, range(7, 1, -1))) % 10
    if not valid:
        check = (check + 1) % 10
    return int("".join(map(str, digits)) + str(check))


def valid_imo(n):
    s = str(n)
    return len(s) == 7 and sum(int(d) * w for d, w in zip(s[:6], range(7, 1, -1))) % 10 == int(s[6])


def make_fleet(seed=7):
    rng = random.Random(seed)
    fleet, used = [], set()
    for mid, count in {**NORDIC, **OTHER}.items():
        for _ in range(count):
            mmsi = int(mid + "".join(str(rng.randint(0, 9)) for _ in range(6)))
            if mmsi in used:
                continue
            used.add(mmsi)
            length = int(max(8, min(335, rng.lognormvariate(math.log(65), 0.75))))
            has_imo = length >= 40 and rng.random() < 0.8
            spoofed = has_imo and rng.random() < 0.04  # felaktigt IMO-nummer, som vid manipulerad AIS
            imo = imo_number(rng, valid=not spoofed) if has_imo else 0
            gt = int(0.0079 * length ** 2.9 * rng.uniform(0.8, 1.25))
            in_register = has_imo and not spoofed and rng.random() < 0.6
            fleet.append({
                "mmsi": mmsi, "length": length, "imo": imo, "gt": gt, "in_register": in_register,
                "type": rng.choices([70, 80, 60, 30, 52, 90], weights=[40, 15, 12, 15, 8, 10])[0],
                "name": f"TESTFARTYG {len(fleet) + 1}",
            })
    return fleet


def make_noise():
    """Meddelanden som collector.py ska ignorera: kuststation, sjömärke, flygplan."""
    return [
        {"mmsi": 2655001, "length": 0, "imo": 0, "type": 0, "name": "KUSTSTATION"},
        {"mmsi": 992651234, "length": 0, "imo": 0, "type": 0, "name": "SJOMARKE"},
        {"mmsi": 111265001, "length": 0, "imo": 0, "type": 0, "name": "HELIKOPTER"},
    ]


def static_message(v):
    a = int(v["length"] * 0.6)
    beam = max(2, int(v["length"] * 0.16))
    return {
        "MessageType": "ShipStaticData",
        "MetaData": {"MMSI": v["mmsi"], "MMSI_String": v["mmsi"], "ShipName": v["name"],
                     "latitude": 57.7, "longitude": 11.9, "time_utc": "2026-10-01 12:00:00 +0000 UTC"},
        "Message": {"ShipStaticData": {
            "AisVersion": 0, "CallSign": "TEST", "Destination": "GOTEBORG",
            "Dimension": {"A": a, "B": v["length"] - a, "C": beam // 2, "D": beam - beam // 2},
            "Dte": False, "Eta": {"Day": 2, "Hour": 8, "Minute": 0, "Month": 10},
            "FixType": 1, "ImoNumber": v["imo"], "MaximumStaticDraught": 6.5, "MessageID": 5,
            "Name": v["name"], "RepeatIndicator": 0, "Spare": False, "Type": v["type"],
            "UserID": v["mmsi"], "Valid": True}},
    }


def facit(fleet, register):
    """Det collector.py borde räkna fram för de nordiska fartygen."""
    keys = ("seen", "confirmed_ge", "confirmed_lt", "likely_ge", "likely_lt", "unknown")
    out = {code: dict.fromkeys(keys, 0) for code in sorted(set(COUNTRY_OF_MID.values()))}
    for v in fleet:
        code = COUNTRY_OF_MID.get(str(v["mmsi"])[:3])
        if not code:
            continue
        s = out[code]
        s["seen"] += 1
        gt = register.get(v["imo"]) if valid_imo(v["imo"]) else None
        if gt is not None:
            s["confirmed_ge" if gt >= MIN_GT else "confirmed_lt"] += 1
        elif v["length"] >= PROXY_LENGTH:
            s["likely_ge"] += 1
        else:
            s["likely_lt"] += 1
    return out


FLEET = make_fleet()
REGISTER = {v["imo"]: v["gt"] for v in FLEET if v["in_register"]}


async def handler(ws):
    try:
        subscription = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
    except Exception:
        await ws.close(code=1008, reason="Ingen giltig prenumeration inom 3 sekunder")
        return
    if subscription.get("APIKey") != API_KEY or not subscription.get("BoundingBoxes"):
        await ws.send(json.dumps({"error": "Api Key Is Not Valid"}).encode())
        await ws.close()
        return
    try:
        await ws.send(json.dumps({"MessageType": "SubscriptionConfirmation",
                                  "Message": {"CompressionEnabled": True}}).encode())
        rng = random.Random()
        everyone = FLEET + make_noise()
        rng.shuffle(everyone)
        for v in everyone:  # alla fartyg en gång, snabbt
            await ws.send(json.dumps(static_message(v)).encode())
            await asyncio.sleep(0.002)
        while True:  # därefter upprepas några slumpvis, som riktiga periodiska sändningar
            await asyncio.sleep(0.05)
            await ws.send(json.dumps(static_message(rng.choice(FLEET))).encode())
    except websockets.ConnectionClosed:
        return


async def main():
    path = HERE / "sample_gt_register.csv"
    path.write_text("imo,gt\n" + "".join(f"{imo},{gt}\n" for imo, gt in sorted(REGISTER.items())),
                    encoding="utf-8")
    print(f"Skrev {path} med {len(REGISTER)} påhittade fartyg.")
    print("Facit (det collector.py ska räkna fram):")
    print(json.dumps(facit(FLEET, REGISTER), ensure_ascii=False))
    async with websockets.serve(handler, HOST, PORT):
        print(f"Låtsas-aisstream lyssnar på ws://{HOST}:{PORT} (API-nyckel: {API_KEY}). Avsluta med Ctrl+C.")
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
