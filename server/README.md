# Servern: kontinuerlig insamling

Det här är den del av NORTH SCOPE som lyssnar på AIS hela tiden och sparar i PostgreSQL med PostGIS. Den sparar fartygens uppgifter och utglesade positioner för alla flaggstater och alla fartygstyper. Räddnings- och militära fartyg sparas aldrig.

```
aisstream.io  →  live_collector.py  →  PostgreSQL + PostGIS (tabellerna i schema.sql)
```

Den befintliga insamlingen på GitHub (`collector.py`) fortsätter att fungera. Servern ersätter den inte än.

## Vad som är provat och inte

Provat (mot en låtsasström och en riktig PostGIS-databas): tolkning av meddelanden, utglesning, uteslutning av fartygstyper, ogiltiga koordinater, kuststationer och sjömärken, återanslutning när anslutningen bryts, databas som startas om under körning, felaktig eller saknad API-nyckel.

**Inte provat:** Docker-filerna (`Dockerfile`, `docker-compose.yml`), anslutningen till den riktiga aisstream.io-strömmen, och meddelandeformaten för positionsrapporter och klass B-uppgifter från den riktiga strömmen. Fältnamnen bygger på aisstream.io:s dokumentation, och tolkningen tål olika skiftläge, men första riktiga körningen kan visa avvikelser. Kontrollera därför loggen och tabellen `collector_runs` (se nedan) de första timmarna.

## Starta på en server

Du behöver en Linux-server med Docker och Docker Compose.

1. Kopiera mappen `server` till servern.
2. Kopiera `.env.example` till `.env` och fyll i `AISSTREAM_API_KEY` och ett lösenord i `POSTGRES_PASSWORD`.
3. Starta: `docker compose up -d --build`
4. Följ loggen: `docker compose logs -f collector`. Efter en minut ska en rad visa antal meddelanden och sparade positioner.

Databasens port är inte öppen mot internet. Låt det vara så.

## Kontrollera att det fungerar

```
docker compose exec db psql -U north northscope
```

```sql
-- Senaste körningarna: meddelanden, sparade positioner, återanslutningar
SELECT id, started, last_heartbeat, received, stored_positions, thinned_out, excluded, reconnects
FROM collector_runs ORDER BY id DESC LIMIT 5;

-- Antal fartyg per flagga
SELECT flag, count(*) FROM vessels GROUP BY flag ORDER BY count(*) DESC LIMIT 15;

-- Diskutrymme
SELECT pg_size_pretty(pg_database_size('northscope'));
```

`last_heartbeat` ska ligga högst en halv minut bakåt. Om den gör det inte körs insamlingen inte.

## Hur mycket data blir det

Det vet vi inte än. Standardinställningarna sparar högst en position var tredje minut för ett fartyg i fart och var halvtimme för ett fartyg som ligger still, och oftare vid kursändringar. Mät storleken efter ett dygn och efter en vecka (frågan ovan) innan du planerar disk och område. Börja med området `nordeuropa`. Området `global` ger mångfalt mer data.

Inställningarna ändras med flaggor till `live_collector.py` (se `--help`): `--min-gap-s`, `--moving-max-s`, `--still-max-s`, `--turn-deg`, `--move-m`.

## Fartyg som aldrig sparas

Fartyg med AIS-typ 35 (militär), 51 (sök och räddning), 55 (polis och kustbevakning) och 58 (sjukvårdstransport) sparas aldrig. När ett fartyg anger en sådan typ raderas även det som redan sparats om det. Fartyget läggs i tabellen `excluded_vessels`.

- Fartyg som anger fel typ eller ingen typ kan inte fångas automatiskt. Lägg kända MMSI-nummer i tabellen för hand: `INSERT INTO excluded_vessels (mmsi, reason) VALUES ('265123456', 'manual');` Insamlingen läser listan vid start.
- Typkoderna ska kontrolleras mot ITU-R M.1371 innan sidan görs öppen.
- Listan med typer finns i `EXCLUDED_TYPES` i `live_collector.py`.

## Säkerhetskopia

```
docker compose exec -T db pg_dump -U north northscope | gzip > northscope-$(date +%F).sql.gz
```

Positioner går inte att hämta i efterhand, så gör en kopia regelbundet och spara den utanför servern.

## Filer

| Fil | Innehåll |
|---|---|
| `schema.sql` | tabeller för fartyg, positioner, hamnar, anlöp och drift |
| `live_collector.py` | insamlingen |
| `data/mid_countries.csv` | landskoder (MID) för alla flaggstater, från ITU:s lista |
| `docker-compose.yml`, `Dockerfile` | databas och insamling som ett paket |
| `.env.example` | mall för nyckel och lösenord |

## Prov

Från projektets rot, mot en egen PostGIS-databas med `schema.sql` inläst (rensar tabellerna, använd aldrig mot riktig data):

```
python3 tests/test_live_collector.py
TEST_DATABASE_URL=postgresql://north:north@localhost:5432/northscope python3 tests/run_live_test.py
```

Det andra provet startar `tests/fake_live_aisstream.py` själv.

## Villkor

Läs villkoren hos aisstream.io om lagring och publicering innan sidan görs öppen. Högst tre samtidiga anslutningar per konto. Kör inte insamlingen på två ställen med samma nyckel utan att räkna med det.
