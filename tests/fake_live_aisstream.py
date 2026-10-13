#!/usr/bin/env python3
"""Låtsasversion av aisstream.io för att prova server/live_collector.py.

Skickar ShipStaticData, StaticDataReport och positionsrapporter för påhittade fartyg,
och dessutom sådant som insamlingen ska vägra spara:
  - räddnings- och militära fartyg (AIS-typ 35, 51, 55, 58), både med typen känd från början
    och med positioner som kommer före typen
  - kuststation, sjömärke, flygplan och okänd landskod
  - positioner med ogiltiga koordinater (91/181 och 0/0)
Första anslutningen bryts efter 3 sekunder, för att prova återanslutning.
Facit skrivs till filen som anges med --facit.

    python3 tests/fake_live_aisstream.py --facit facit.json
"""
import argparse
import asyncio
import json
import random

import websockets

HOST, PORT, API_KEY = "localhost", 8766, "test-key"
FLAG_OF_MID = {"265": "SE", "266": "SE", "257": "NO", "219": "DK", "230": "FI", "251": "IS",
               "636": "LR", "538": "MH", "211": "DE", "273": "RU", "563": "SG", "244": "NL"}
TYPES = [70, 80, 60, 30, 52, 90, 37]


def mmsi_for(mid, n):
    return f"{mid}{n:06d}"


def build_scenario(seed=11):
    rng = random.Random(seed)
    normal, n = [], 0
    for mid in FLAG_OF_MID:
        for _ in range(3):
            n += 1
            normal.append({"mmsi": mmsi_for(mid, 1000 + n), "mid": mid, "flag": FLAG_OF_MID[mid],
                           "type": rng.choice(TYPES), "length": rng.randint(12, 300),
                           "moving": n % 3 != 0, "class_b": n % 9 == 0,
                           "lat": 55.0 + rng.uniform(0, 4), "lon": 8.0 + rng.uniform(0, 8)})
    excluded = [{"mmsi": mmsi_for("265", 5000 + i), "type": t, "late": False, "lat": 57.0, "lon": 11.5}
                for i, t in enumerate((35, 51, 55, 58))]
    excluded += [{"mmsi": mmsi_for("257", 6000 + i), "type": t, "late": True, "lat": 58.0, "lon": 10.5}
                 for i, t in enumerate((35, 51))]
    noise = [{"mmsi": "002655001", "what": "kuststation"}, {"mmsi": "992651234", "what": "sjömärke"},
             {"mmsi": "111265001", "what": "flygplan"}, {"mmsi": "999123456", "what": "okänd landskod"}]
    bad = [{"mmsi": mmsi_for("265", 7001), "lat": 91.0, "lon": 181.0},
           {"mmsi": mmsi_for("265", 7002), "lat": 0.0, "lon": 0.0}]
    return normal, excluded, noise, bad


def static_a(mmsi, ship_type, length, name):
    a = int(length * 0.6)
    return {"MessageType": "ShipStaticData",
            "MetaData": {"MMSI": int(mmsi), "ShipName": name},
            "Message": {"ShipStaticData": {
                "UserID": int(mmsi), "Name": name, "Type": ship_type, "ImoNumber": 0,
                "CallSign": "TEST", "Destination": "GOTEBORG",
                "Dimension": {"A": a, "B": length - a, "C": 3, "D": 3},
                "Eta": {"Month": 10, "Day": 2, "Hour": 8, "Minute": 0},
                "MaximumStaticDraught": 6.5, "Valid": True}}}


def static_b(mmsi, ship_type, length, name):
    a = int(length * 0.6)
    return {"MessageType": "StaticDataReport",
            "MetaData": {"MMSI": int(mmsi), "ShipName": name},
            "Message": {"StaticDataReport": {
                "UserID": int(mmsi), "Valid": True,
                "ReportA": {"Name": name, "Valid": True},
                "ReportB": {"ShipType": ship_type, "CallSign": "TESTB", "Valid": True,
                            "Dimension": {"A": a, "B": length - a, "C": 2, "D": 2}}}}}


def position(mmsi, lat, lon, sog, cog, class_b=False):
    kind = "StandardClassBPositionReport" if class_b else "PositionReport"
    return {"MessageType": kind,
            "MetaData": {"MMSI": int(mmsi), "latitude": lat, "longitude": lon},
            "Message": {kind: {"UserID": int(mmsi), "Latitude": lat, "Longitude": lon,
                               "Sog": sog, "Cog": cog, "NavigationalStatus": 0, "Valid": True}}}


class Fake:
    def __init__(self, facit_path, drop_after=3.0):
        self.normal, self.excluded, self.noise, self.bad = build_scenario()
        self.facit_path, self.drop_after = facit_path, drop_after
        self.connections = 0
        self.sent_positions = {}   # mmsi -> antal skickade positioner
        self.t0 = None

    def write_facit(self):
        json.dump({
            "normal": [{k: v[k] for k in ("mmsi", "flag", "type", "length", "moving")}
                       for v in self.normal],
            "excluded": [e["mmsi"] for e in self.excluded],
            "noise": [n["mmsi"] for n in self.noise],
            "bad": [b["mmsi"] for b in self.bad],
            "sent_positions": self.sent_positions,
            "connections": self.connections,
        }, open(self.facit_path, "w"), indent=1)

    async def send(self, ws, msg, mmsi=None):
        await ws.send(json.dumps(msg).encode())
        if mmsi and "Position" in msg["MessageType"]:
            self.sent_positions[mmsi] = self.sent_positions.get(mmsi, 0) + 1

    async def handler(self, ws):
        try:
            sub = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
        except Exception:
            await ws.close(code=1008)
            return
        if sub.get("APIKey") != API_KEY or not sub.get("BoundingBoxes"):
            await ws.send(json.dumps({"error": "Api Key Is Not Valid"}).encode())
            await ws.close()
            return
        self.connections += 1
        first = self.connections == 1
        started = asyncio.get_event_loop().time()
        try:
            await ws.send(json.dumps({"MessageType": "SubscriptionConfirmation",
                                      "Message": {"CompressionEnabled": True}}).encode())
            for v in self.normal:
                f = static_b if v["class_b"] else static_a
                await self.send(ws, f(v["mmsi"], v["type"], v["length"], f"TEST {v['mmsi']}"))
            for e in self.excluded:
                if not e["late"]:
                    await self.send(ws, static_a(e["mmsi"], e["type"], 40, "UTESLUTEN"))
            tick, late_sent = 0, False
            while True:
                await asyncio.sleep(0.1)
                tick += 1
                elapsed = asyncio.get_event_loop().time() - started
                if first and elapsed >= self.drop_after:
                    self.write_facit()
                    await ws.close(code=1011)  # bryt anslutningen en gång
                    return
                for v in self.normal:
                    if v["moving"]:
                        lat = v["lat"] + tick * 0.00005
                        cog = 20.0 if (tick // 25) % 2 == 0 else 110.0  # svänger då och då
                        await self.send(ws, position(v["mmsi"], lat, v["lon"], 12.0, cog, v["class_b"]), v["mmsi"])
                    else:
                        await self.send(ws, position(v["mmsi"], v["lat"], v["lon"], 0.0, 360.0, v["class_b"]), v["mmsi"])
                for e in self.excluded:
                    await self.send(ws, position(e["mmsi"], e["lat"], e["lon"], 5.0, 90.0), e["mmsi"])
                    if e["late"] and not late_sent and elapsed > 2.0:
                        pass
                if elapsed > 2.0 and not late_sent:
                    late_sent = True
                    for e in self.excluded:
                        if e["late"]:
                            await self.send(ws, static_a(e["mmsi"], e["type"], 40, "SEN TYP"))
                for n in self.noise:
                    await self.send(ws, position(n["mmsi"], 57.0, 11.0, 1.0, 10.0), n["mmsi"])
                for b in self.bad:
                    await self.send(ws, position(b["mmsi"], b["lat"], b["lon"], 1.0, 10.0), b["mmsi"])
                if tick % 50 == 0:
                    self.write_facit()
        except websockets.ConnectionClosed:
            self.write_facit()


async def main(args):
    fake = Fake(args.facit, args.drop_after)
    async with websockets.serve(fake.handler, HOST, PORT):
        print(f"Låtsas-aisstream (positioner) på ws://{HOST}:{PORT}, nyckel: {API_KEY}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--facit", default="facit.json")
    ap.add_argument("--drop-after", type=float, default=3.0)
    try:
        asyncio.run(main(ap.parse_args()))
    except KeyboardInterrupt:
        pass
