# Servern: kontinuerlig insamling

Det här är den del av NORTH SCOPE som lyssnar på AIS hela tiden och sparar i PostgreSQL med PostGIS. Den sparar fartygens uppgifter och utglesade positioner för alla flaggstater och alla fartygstyper. Räddnings- och militära fartyg sparas aldrig.

```
aisstream.io  →  live_collector.py  →  PostgreSQL + PostGIS (tabellerna i schema.sql)
                                         ↑                ↓
                                    ports.py         port_calls.py  →  anlöp och förbindelser
```

Den befintliga insamlingen på GitHub (`collector.py`) fortsätter att fungera. Servern ersätter den inte än.

## Vad som är provat och inte

Provat (mot en låtsasström och en riktig PostGIS-databas): tolkning av meddelanden, utglesning, uteslutning av fartygstyper, ogiltiga koordinater, kuststationer och sjömärken, återanslutning när anslutningen bryts, databas som startas om under körning, felaktig eller saknad API-nyckel. Dessutom hamninläsning ur UN/LOCODE, anlöpsdetektorn med tio påhittade rörelsemönster och facit (se nedan), omräkning utan dubbletter, förbindelser mellan hamnar, polygonförslag och godkännande.

**Inte provat:** Docker-filerna (`Dockerfile`, `docker-compose.yml`), anslutningen till den riktiga aisstream.io-strömmen, meddelandeformaten för positionsrapporter och klass B-uppgifter från den riktiga strömmen, och detektorn mot riktiga rörelser. Hamnavgränsningarna och inställningarna för anlöp är första gissningar som ska justeras när riktiga positioner finns. Fältnamnen bygger på aisstream.io:s dokumentation, och tolkningen tål olika skiftläge, men första riktiga körningen kan visa avvikelser. Kontrollera därför loggen och tabellen `collector_runs` (se nedan) de första timmarna.

## Starta på en server

Du behöver en Linux-server med Docker och Docker Compose.

1. Kopiera mappen `server` till servern.
2. Kopiera `.env.example` till `.env` och fyll i `AISSTREAM_API_KEY` och ett lösenord i `POSTGRES_PASSWORD`.
3. Starta: `docker compose up -d --build`
4. Följ loggen: `docker compose logs -f collector`. Efter en minut ska en rad visa antal meddelanden och sparade positioner.

Databasens port är inte öppen mot internet. Låt det vara så.

## Hamnar och anlöp

Anlöp räknas ur positionerna. Det kräver att hamnarna finns i databasen, och att avgränsningarna är rimliga.

**1. Läs in hamnarna** (en gång, och gärna om efter ett par månader):

```
docker compose run --rm collector python ports.py load --download
```

Det hämtar UN/LOCODE (öppen data) från GitHub och lägger en cirkel med 3 km radie runt varje hamn. Koordinaterna i UN/LOCODE är i hela bågminuter (1–2 km) och ligger i stadens mitt, inte i hamnbassängen, så cirklarna är grova. Över 5 000 hamnar saknar koordinater helt, bland annat Gdańsk och Riga. Skriptet listar de nordiska som saknas. Lägg de viktiga i `data/port_extra.csv` (kod, namn, land, latitud, longitud, radie i meter). Koordinaterna där är ungefärliga och ska kontrolleras.

**2. Starta anlöpsdetektorn.** Den ingår i `docker compose up -d` (tjänsten `calls`) och räknar om de senaste 72 timmarna var femte minut. Ett anlöp kräver minst 20 minuter i hamnen och att fartyget gått långsamt (högst 2 knop, och utan att ha angett att det ligger för ankar). Fartyg som passerar i fart räknas inte.

**3. Förbättra avgränsningarna efter en till två veckors data.**

```
docker compose run --rm collector python ports.py suggest        # förslag ur stillastående fartyg
docker compose run --rm -v "$PWD:/out" collector python ports.py geojson --out /out/hamnar.geojson
```

Filen visar nuvarande cirkel (blå) och förslag (orange) för varje hamn, och öppnas på https://geojson.io. Granska på kartan. Förslaget är konvexa höljet kring stillastående fartyg plus 250 m, utan fartyg som anger att de ligger för ankar. Godkänn sedan:

```
docker compose run --rm collector python ports.py apply SEGOT DKAAL   # bestämda hamnar
docker compose run --rm collector python ports.py apply --all         # alla med minst 8 fartyg och 50 positioner
```

Inget godkänns av sig självt, och en godkänd polygon skrivs aldrig över av en ny inläsning av hamnlistan. `python3 ports.py stats` ger en översikt.

**Förbindelser** ligger i vyn `port_legs`: varje anlöp tillsammans med fartygets föregående anlöp.

```sql
SELECT from_locode, to_locode, count(*) FROM port_legs
WHERE from_locode IS NOT NULL GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 20;
```

**Kända begränsningar** (ska också redovisas på sidan):

- Ett fartyg på redden eller på väg förbi nära en hamn kan räknas som anlöp så länge avgränsningen är en cirkel, och kajplatser utanför cirkeln missas.
- Positionerna glesas ut, så stopp under cirka 20 minuter kan missas. Det gäller främst tätt trafikerade färjelinjer. Mät efter en vecka och justera utglesningen om det behövs.
- Fartyg som redan låg i hamnen när insamlingen började får `arrival_known = false`. Fartyg som stänger av AIS eller lämnar täckningen i hamnen stängs efter 24 timmars tystnad med `departure_known = false`. Räkna inte med dessa rader när du mäter uppehållstider.
- Ett fartyg som lämnar täckningen på öppet hav och dyker upp i nästa hamn får ändå en förbindelse, men utan känd sträcka däremellan.

Om databasen redan skapades med en tidigare version av `schema.sql` ska tabellerna `ports` och `port_calls` (som då var tomma) tas bort före en ny inläsning: `DROP VIEW port_legs; DROP TABLE port_calls, ports CASCADE;`

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
| `ports.py` | läser in hamnar, föreslår och godkänner polygoner |
| `port_calls.py` | anlöpsdetektorn |
| `data/port_extra.csv` | hamnar som saknar koordinater i UN/LOCODE, med ungefärliga koordinater |
| `data/mid_countries.csv` | landskoder (MID) för alla flaggstater, från ITU:s lista |
| `docker-compose.yml`, `Dockerfile` | databas och insamling som ett paket |
| `.env.example` | mall för nyckel och lösenord |

## Prov

Från projektets rot, mot en egen PostGIS-databas med `schema.sql` inläst (rensar tabellerna, använd aldrig mot riktig data):

```
python3 tests/test_live_collector.py
python3 tests/test_port_calls.py
TEST_DATABASE_URL=postgresql://north:north@localhost:5432/northscope python3 tests/run_live_test.py
TEST_DATABASE_URL=postgresql://north:north@localhost:5432/northscope python3 tests/run_port_test.py
```

`run_live_test.py` startar `tests/fake_live_aisstream.py` själv. `run_port_test.py` lägger in påhittade hamnar och rörelser och jämför med facit.

## Villkor

Läs villkoren hos aisstream.io om lagring och publicering innan sidan görs öppen. Högst tre samtidiga anslutningar per konto. Kör inte insamlingen på två ställen med samma nyckel utan att räkna med det.
