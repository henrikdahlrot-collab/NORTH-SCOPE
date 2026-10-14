-- NORTH SCOPE: datamodell för den kontinuerliga insamlingen.
-- Körs en gång mot en tom databas (docker-compose gör det automatiskt).
-- Skriptet kan köras igen utan skada: allt skapas bara om det saknas.

CREATE EXTENSION IF NOT EXISTS postgis;

-- ------------------------------------------------------------------ flaggstater
-- Landskoden (MID) är de tre första siffrorna i ett fartygs MMSI-nummer.
-- Fylls ur data/mid_countries.csv av live_collector.py vid start.
CREATE TABLE IF NOT EXISTS mid_countries (
    mid   text PRIMARY KEY,
    iso2  text NOT NULL,
    name  text NOT NULL
);

-- ------------------------------------------------------------------ fartyg
-- Ett fartyg per MMSI-nummer. Flaggan härleds ur MID och är inte registrerad flaggstat.
CREATE TABLE IF NOT EXISTS vessels (
    mmsi            text PRIMARY KEY,
    mid             text NOT NULL,
    flag            text NOT NULL,                -- ISO-landskod ur MID
    imo             integer,
    name            text,
    call_sign       text,
    ship_type       smallint,                     -- AIS-kod 0-99, angiven av besättningen
    length_m        smallint,
    beam_m          smallint,
    draught_m       real,
    destination     text,
    eta             text,
    first_seen      timestamptz NOT NULL,
    last_seen       timestamptz NOT NULL,
    static_updated  timestamptz
);
CREATE INDEX IF NOT EXISTS vessels_flag_idx ON vessels (flag);
CREATE INDEX IF NOT EXISTS vessels_imo_idx ON vessels (imo) WHERE imo IS NOT NULL;

-- Fartyg som aldrig ska sparas (räddning, militär, myndighet). Fylls av insamlingen
-- när ett fartyg anger en sådan typ, och kan fyllas på för hand med kända MMSI-nummer.
CREATE TABLE IF NOT EXISTS excluded_vessels (
    mmsi      text PRIMARY KEY,
    reason    text NOT NULL,                      -- 'ais_type:35', 'manual' osv.
    added_at  timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------ positioner
-- Utglesade positioner. Insamlingen sparar inte varje meddelande, se live_collector.py.
CREATE TABLE IF NOT EXISTS positions (
    mmsi        text        NOT NULL,
    ts          timestamptz NOT NULL,
    geom        geometry(Point, 4326) NOT NULL,
    sog_kn      real,                             -- fart över grund
    cog_deg     real,                             -- kurs över grund
    nav_status  smallint,
    PRIMARY KEY (mmsi, ts)
);
CREATE INDEX IF NOT EXISTS positions_geom_idx ON positions USING gist (geom);
CREATE INDEX IF NOT EXISTS positions_ts_idx   ON positions USING brin (ts);

-- ------------------------------------------------------------------ hamnar och anlöp
-- Hamnarna läses in av ports.py. area är avgränsningen som används för att avgöra om ett
-- fartyg är i hamnen: först en cirkel runt koordinaten ('circle'), senare en polygon
-- som är granskad och godkänd ('polygon'). area_suggested är ett förslag ur verkliga positioner.
CREATE TABLE IF NOT EXISTS ports (
    locode           text PRIMARY KEY,            -- UN/LOCODE, t.ex. SEGOT
    name             text NOT NULL,
    country          text NOT NULL,
    geom             geometry(Point, 4326) NOT NULL,
    area             geometry(Polygon, 4326),
    radius_m         integer NOT NULL DEFAULT 3000,
    area_source      text NOT NULL DEFAULT 'circle',   -- 'circle' eller 'polygon'
    coord_source     text NOT NULL DEFAULT 'unlocode', -- 'unlocode' eller 'manual'
    area_suggested   geometry(Polygon, 4326),
    suggested_support integer,                    -- antal stillastående positioner bakom förslaget
    suggested_vessels integer,
    suggested_at     timestamptz
);
CREATE INDEX IF NOT EXISTS ports_geom_idx ON ports USING gist (geom);
CREATE INDEX IF NOT EXISTS ports_area_idx ON ports USING gist (area);

-- Ett anlöp = ett fartyg som varit i en hamn en viss tid. Skrivs av port_calls.py.
CREATE TABLE IF NOT EXISTS port_calls (
    id               bigserial PRIMARY KEY,
    mmsi             text        NOT NULL,
    locode           text        NOT NULL REFERENCES ports (locode),
    arrived          timestamptz NOT NULL,
    departed         timestamptz,                 -- NULL = fartyget ligger kvar
    last_inside      timestamptz,                 -- senaste position inne i hamnen
    arrival_known    boolean NOT NULL DEFAULT true,   -- false: fartyget låg redan i hamnen när insamlingen började
    departure_known  boolean NOT NULL DEFAULT true,   -- false: fartyget försvann ur täckningen i hamnen
    UNIQUE (mmsi, locode, arrived)
);
CREATE INDEX IF NOT EXISTS port_calls_locode_idx ON port_calls (locode, arrived);
CREATE INDEX IF NOT EXISTS port_calls_open_idx ON port_calls (mmsi) WHERE departed IS NULL;

-- Förbindelser mellan hamnar: varje anlöp tillsammans med fartygets föregående anlöp.
CREATE OR REPLACE VIEW port_legs AS
SELECT mmsi,
       lag(locode)   OVER w AS from_locode,
       locode        AS to_locode,
       lag(departed) OVER w AS departed,
       arrived
FROM port_calls
WINDOW w AS (PARTITION BY mmsi ORDER BY arrived);

-- ------------------------------------------------------------------ drift
-- En rad per körning av insamlingen. Visar var det finns luckor och hur stor volymen är.
CREATE TABLE IF NOT EXISTS collector_runs (
    id                 bigserial PRIMARY KEY,
    started            timestamptz NOT NULL,
    last_heartbeat     timestamptz NOT NULL,
    ended              timestamptz,
    area               text NOT NULL,
    received           bigint NOT NULL DEFAULT 0,   -- meddelanden som kom in
    stored_positions   bigint NOT NULL DEFAULT 0,   -- positioner som sparades
    thinned_out        bigint NOT NULL DEFAULT 0,   -- positioner som glesades bort
    excluded           bigint NOT NULL DEFAULT 0,   -- meddelanden från uteslutna fartyg
    rejected           bigint NOT NULL DEFAULT 0,   -- ogiltiga meddelanden
    reconnects         integer NOT NULL DEFAULT 0,
    note               text
);
