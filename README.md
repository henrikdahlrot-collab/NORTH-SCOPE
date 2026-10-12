# Nordisk flagg: fartyg över 300 brutto (AIS)

Startversion av en sida som visar hur många fartyg över 300 brutto som setts i AIS, fördelade på Sverige, Norge, Danmark, Finland och Island.

```
aisstream.io (AIS)  →  collector.py  →  docs/data.json  →  docs/index.html
```

**Plattform:** Python 3 för insamlingen och vanlig HTML och JavaScript för sidan. Inga ramverk och inget bygge. Redigera i VS Code eller valfri editor.

## Läs det här först

1. **AIS innehåller ingen bruttodräktighet.** Skriptet slår upp GT via IMO-numret i `gt_register.csv`. Fartyg som saknas där räknas som *troliga* om AIS anger en längd på minst 40 m. Det är en grov gissning. Fartyg utan längd räknas som okända.
2. **Flaggan härleds ur MMSI-numret** (landskoden MID, till exempel 265 och 266 för Sverige), inte ur ett register. Den kan avvika från den registrerade flaggstaten.
3. **Siffrorna visar fartyg som setts, inte flottornas storlek.** Fartyg utanför mottagarnas räckvidd eller med avstängd AIS räknas inte, och en del AIS-data är felaktig. Jämför gärna med Trafikanalys officiella registerstatistik.

## Kom igång

1. Installera Python 3.10 eller senare från python.org. På Windows: kryssa i "Add python.exe to PATH" och skriv `py` i stället för `python3` nedan.
2. Skapa ett konto på <https://aisstream.io> och kopiera API-nyckeln från sidan Account.
3. Öppna en terminal i projektmappen och kör:
   ```
   python3 -m pip install -r requirements.txt
   ```
   Kopiera sedan `.env.example` till `.env` och klistra in nyckeln efter `AISSTREAM_API_KEY=`.
4. Samla data i 10 minuter:
   ```
   python3 collector.py --minutes 10
   ```
   Lägg till `--area nordic` om du bara vill ha nordiska farvatten. Det ger mindre trafik att läsa.
5. Visa sidan:
   ```
   python3 -m http.server -d docs 8000
   ```
   Öppna sedan <http://localhost:8000>. Sidan måste öppnas via en webbserver. Dubbelklick på `index.html` fungerar inte, eftersom webbläsaren då blockerar inläsningen av `data.json`.

Skriptet sparar sedda fartyg i `data/vessels.json`. Varje körning bygger vidare på de tidigare, och fartyg som inte setts på 7 dygn rensas bort.

## Prova utan API-nyckel

`tests/fake_aisstream.py` är en låtsasversion av aisstream.io med påhittade fartyg. Starta den i en terminal:

```
python3 tests/fake_aisstream.py
```

Kör i en annan terminal:

```
python3 collector.py --url ws://localhost:8765 --api-key test-key --minutes 0.3 --gt-file tests/sample_gt_register.csv --state data/demo.json
```

Sidan märks då med "Exempeldata". Siffror från en testserver blandas aldrig med riktiga: skriptet börjar om med tomt läge om källan byts.

## Bruttodräktighet

Lägg fartygens GT i `gt_register.csv` med kolumnerna `imo,gt` (komma eller semikolon, hela tal). Kör sedan om utan att lyssna:

```
python3 collector.py --minutes 0
```

Var GT finns:

- Skrapa inte Equasis. Villkoren förbjuder bulknedladdning och det finns inget API.
- En kommersiell fartygsdatabas med API, till exempel från MarineTraffic (Kpler) eller VesselFinder, är det mest realistiska. Kolla vilken prisnivå som ger tillgång till bruttodräktighet.
- Nationella register: Transportstyrelsens fartygsregister kräver godkänd direktåtkomst.

## Bra att veta om aisstream.io

- Anslut aldrig direkt från webbläsaren och lägg aldrig nyckeln i `index.html`. Det är därför insamlingen körs som ett eget skript.
- Högst tre anslutningar per konto. Prenumerationen måste skickas inom 3 sekunder.
- Tjänsten har ingen garanterad drifttid och missade meddelanden spelas inte upp igen. Skriptet återansluter med växande väntetid.
- Okomprimerade anslutningar begränsas sedan september 2026. Skriptet använder komprimering.
- Läs villkoren på aisstream.io innan du publicerar sidan öppet.

## Ändra inställningar

| Vad | Hur |
|---|---|
| Gräns för bruttodräktighet | `--min-gt 500` |
| Längd för grov uppskattning | `--proxy-length 45` |
| Hur långt tillbaka fartyg räknas | `--window-days 14` |
| Fler länder | lägg till rader i `COUNTRIES` i `collector.py` (MID enligt ITU:s lista) |

## Lägg upp på GitHub (valfritt)

`.github/workflows/collect.yml` samlar in data var sjätte timme och sparar siffrorna i `docs/data.json`. **Filen är skriven men inte provkörd på GitHub.**

1. Skapa ett repo och ladda upp mappen. `.env` och `data/` följer inte med.
2. Settings, Secrets and variables, Actions: skapa hemligheten `AISSTREAM_API_KEY`.
3. Settings, Pages: Deploy from a branch, branch `main`, mapp `/docs`.
4. Actions, Samla AIS-data, Run workflow.

Bara siffrorna i `docs/data.json` hamnar i repot. Fartygslistan sparas i GitHubs cache.

## Filer

| Fil | Innehåll |
|---|---|
| `collector.py` | samlar AIS, räknar per land och skriver `docs/data.json` |
| `docs/index.html` | sidan, med diagram, tabell och metodtext |
| `docs/data.json` | exempeldata tills du kört en riktig insamling |
| `gt_register.csv` | din tabell med bruttodräktighet per IMO-nummer |
| `tests/fake_aisstream.py` | låtsasserver för test |
| `.env.example` | mall för API-nyckeln |

## Nästa steg

- Fördela fartygen på typ (färja, tank, bulk) med `ship_type`, som redan sparas.
- Skilj flaggstat från ägare. Det kräver ägaruppgifter ur en fartygsdatabas.
- Spara en historik per vecka så att sidan kan visa utveckling över tid.
