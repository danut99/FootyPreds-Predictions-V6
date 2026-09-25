# FootyPreds V8

Analizor de meciuri de **fotbal, baschet și tenis**: **Python + FastAPI + SQLite**, o
aplicație web (bilete AI, meciuri, live, simulator, portofel virtual) și un **client Excel**
conectat la același API. La fotbal, 1X2, șansă dublă, goluri, GG, scor corect și
pauză/final provin din aceeași matrice de scoruri; baschetul are distribuții de diferență și
total, tenisul Elo pe suprafață și cotele pieței.

> Probabilitățile sunt estimări statistice, nu garanții. Aplicația nu plasează pariuri: portofelul și simulatorul folosesc bani virtuali. 18+.

## Pornire rapidă (Windows)

Necesită Python 3.11+. Din rădăcina proiectului:

```powershell
Copy-Item .env.example .env   # o singură dată, apoi completează RAPIDAPI_KEY
.\start.ps1                   # creează .venv, instalează dependențele, pornește serverul
```

Deschide **http://127.0.0.1:8000**. Fără cheie RapidAPI aplicația rulează în mod demo.

**Fără rețea și fără cotă RapidAPI** (date FlashScore fictive, sigle generate, set de
simulare sintetic; baza reală nu este atinsă):

```powershell
.\.venv\Scripts\python.exe footypreds/scripts/mock_server.py --port 8765   # Ctrl+C oprește
```

## Paginile aplicației

| Rută | Conținut |
|---|---|
| `#/` **Acasă** | biletele AI ale zilei la cotele x2, x5, x10, x100, cele mai sigure selecții simple, banda „Live acum” (un clic deschide meciul), soldul virtual și jocul virtual; o selecție comună mai multor bilete este marcată („apare și în x5”) |
| `#/meciuri` | tabla zilei pe sporturi: carduri cu competiția, probabilitățile principale, pontul și nota A–D; filtre de calitate și stare (inclusiv Live); export Excel |
| `#/meci/{id}?sport=` | analiza pe sport: fotbal (1X2 model vs. piață, goluri calibrate, scor corect, formă, H2H, clasament), baschet (distribuții, handicap, totaluri, odihnă), tenis (Elo pe suprafață, seturi, game-uri) |
| `#/live` | meciuri în joc, actualizare la 30 s (cu pauză), cotă corectă și cotă minimă; detalii cu statistici în română |
| `#/bilete` | generator simplu: doar cota țintă, sporturile și ziua; „Altă variantă” exclude meciurile deja arătate, „Resetează excluderile” când s-au terminat |
| `#/simulator` | strategii scară, bilet zilnic, simple, value; ultimele N zile din aplicație; grafic și jurnal zi cu zi |
| `#/portofel` | depuneri virtuale, pariuri deschise/decontate, evoluția soldului; un pariu fără sold oferă întâi o depunere |
| `#/rezultate` | istoricul biletelor AI, jurnalul selecțiilor pre-meci și calibrarea |
| `#/metoda` | metoda pe sport, limitări măsurate, benchmark |

Ziua aplicației este ziua **UTC** (în România include meciurile până la 03:00, iarna 02:00);
paginile o spun acolo unde listează meciuri. Contractele API exacte (cu exemple JSON) sunt în
[footypreds/docs/CONTRACTS.md](footypreds/docs/CONTRACTS.md).

### Bilete AI

Pentru o cotă țintă, un optimizator exact alege combinația cu cea mai mare probabilitate
estimată (o selecție pe meci, cota totală 0,93–1,12 × ținta). Selecțiile au nota A–C, cote
reale 1,08–4,0, nu pot fi rambursate și au `probabilitate × cotă` între 0,95 și 1,05 (plafon
prudent, nu optim măsurat; vezi MODEL.md). Simulatorul folosește **aceeași regulă**.

### Live

`GET /api/live?sport=…` și `GET /api/live/{id}?sport=…`: probabilitățile se recalculează din
scor și timpul rămas. Cotele listei sunt de dinainte de meci, deci UI-ul arată cota corectă și
cota minimă de la care un pariu ar merita, nu cote live. După start, analiza unui meci nu mai
încarcă cote noi (nicio cotă din timpul meciului nu ajunge în predicții sau în simulator).

### Simulator și scara (rollover)

Simularea este **oarbă**: pentru ziua D modelul vede doar rezultatele de dinainte de D,
biletul și miza se fixează, abia apoi se află scorurile. **Scara**: în fiecare zi un singur
bilet la cota țintă, miza = partea reinvestită × soldul scării; un bilet pierdut închide scara
și (implicit) a doua zi pornește alta cu suma inițială, iar totalul investit se adună. Un
bilet anulat returnează miza și ziua contează ca ținută; „încasează după N zile” închide scara
după N bilete reușite și pornește alta (și fără repornire după pierdere). La scară, rezultatul
onest este **câștigul net = returnat − investit** (graficul pornește de la zero).

UI: `#/simulator` → strategia „Scară”, suma (ex. 5), cota țintă (ex. 2), reinvestire,
încasare, repornire. Setul „Ultimele zile”: alegi N zile și sporturile, apeși **Pregătește
datele** (spune câte cereri FlashScore folosește: una pe zi și sport, plus 14 zile de formă;
zilele salvate sunt sărite), apoi **Rulează simularea**. Setul „recent” folosește numai cotele
1X2 salvate înainte de start; meciurile amânate intră ca bilete anulate.

CLI:

```powershell
.\.venv\Scripts\python.exe -m footypreds.evaluation.sim_datasets --download   # ligi suplimentare (football-data.co.uk)
.\.venv\Scripts\python.exe -m footypreds.simulator --dataset football-plus --mode ladder --target 2 --bankroll 5 --start 2025-08-01 --end 2026-05-24
.\.venv\Scripts\python.exe -m footypreds.simulator --dataset football-plus --mode ladder --target 5 --bankroll 5 --reinvest 0.5 --max-days 3 --no-restart
.\.venv\Scripts\python.exe -m footypreds.simulator --dataset football --mode ticket --target 2 --stake 10
```

Rezultatele reale sunt negative (și pariul pe favoritul casei pierde): modelul nu are un
avantaj măsurat față de piață. La fotbal, sezonul 2024-25 este **în eșantion** (calibrarea
golurilor și plafonul de valoare au fost potrivite pe el); cifrele din afara eșantionului sunt
2025-26 și de după. Seturile football-data folosesc cote medii (marjă mai mare decât cele mai
bune cote comparate de aplicație).

## Excel

1. **Clientul Excel** (`footypreds/excel_client/`): modul VBA cu butoane care cheamă API-ul
   local, pentru **toate cele trei sporturi** (sportul din Panou B3): predicții, analiza
   meciului, valoare, track record, recomandări AI, live, simulare (inclusiv scara și ultimele
   zile, cu întrebare înainte de a folosi cereri FlashScore) și portofelul virtual. Alternativ,
   conexiuni Power Query fără macro-uri. Detalii în
   [footypreds/excel_client/README.md](footypreds/excel_client/README.md) și
   [PowerQuery.md](footypreds/excel_client/PowerQuery.md).
2. **Export instant**: butonul *Descarcă Excel* de pe tabla unui sport sau
   `GET /api/export.xlsx?day=AAAA-LL-ZZ&sport=…`.

```powershell
.\.venv\Scripts\python.exe footypreds/excel_client/build_xlsm.py   # construiește .xlsm (necesită Excel instalat)
.\.venv\Scripts\python.exe -m footypreds.cli export --day 2026-09-26
```

## Model și evaluare

Detalii în [footypreds/docs/MODEL.md](footypreds/docs/MODEL.md) și [footypreds/docs/EVALUATION.md](footypreds/docs/EVALUATION.md). Pe scurt:

- Ratingul atac/apărare este ponderat în timp și ajustat după adversari, cu shrinkage spre medie.
- Peste rating se aplică forma recentă și, cu pondere mică, H2H.
- Matricea de scoruri are corecția Dixon-Coles și e combinată cu cotele 1X2 când există.
- Golurile sunt calibrate (8.1): hartă Platt pe P(peste 2,5); cu cotă peste/sub 2,5, probabilitatea este cea a pieței fără marjă.
- Parametrii au fost aleși **numai** pe sezonul de validare 2024–25. Sezonul 2025–26 este testul blocat: [footypreds/docs/BENCHMARK.md](footypreds/docs/BENCHMARK.md).

```powershell
.\.venv\Scripts\python.exe -m footypreds.evaluation.dataset            # arhive publice, fără RapidAPI
.\.venv\Scripts\python.exe -m footypreds.evaluation.run --validate     # sezonul de validare
.\.venv\Scripts\python.exe -m footypreds.evaluation.tune --totals      # calibrarea golurilor (numai validare)
.\.venv\Scripts\python.exe -m footypreds.evaluation.tennis_eval --download   # arhive tenis (tennis-data.co.uk)
```

Testul blocat (`evaluation.run` fără opțiuni) se rulează o singură dată pe versiune de model.

## Structură

```text
footypreds/
  api.py            FastAPI: tabla zilei, analiză, sincronizare, export, bilete, track record; leagă routerele
  excel_api.py      tabele CSV/TSV pentru clientul Excel (/api/excel/...)
  engine/           markets, ratings, history, form, analyzer, backtest
  provider.py       client FlashScore (RapidAPI) + parsare
  store.py          SQLite: meciuri, cache, jurnal, planuri
  tickets.py        generatorul de bilete
  sports/           multi-sport: registru, chei de piață, decontare, cote, baschet, tenis
  recommend.py      recomandările zilnice și generatorul de bilete (recommend_api.py)
  live.py           probabilități live (live_api.py)
  simulator.py      simulatorul de bankroll orb (sim_api.py) și wallet.py (portofel virtual)
  excel.py          exportul .xlsx
  excel_client/     modul VBA, Power Query, instrucțiuni
  web/              SPA: index.html, app.css, core.js, câte un script pe pagină, app.js (router)
  evaluation/       benchmark reproductibil (dataset, run, tune, baseline V7)
  tests/            pytest, fără rețea
  scripts/          mock_server.py (offline), ui_smoke.py (browser), check_connections.py (rețea, opt-in)
  artifacts/        capturile ui_smoke.py (excluse din Git)
  docs/             model, evaluare, benchmark, produs, migrare
  data/             SQLite și date benchmark (excluse din Git)
```

## Dezvoltare

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q                         # fără rețea
.\.venv\Scripts\python.exe -m ruff check footypreds
.\.venv\Scripts\python.exe -m ruff format --check footypreds
node --check footypreds/web/app.js          # la fel pentru fiecare web/*.js
.\.venv\Scripts\python.exe -m uvicorn footypreds.api:app --reload --host 127.0.0.1 --port 8000
.\.venv\Scripts\python.exe -m playwright install chromium       # o dată
.\.venv\Scripts\python.exe footypreds/scripts/mock_server.py --port 8765                           # terminal 1
.\.venv\Scripts\python.exe footypreds/scripts/ui_smoke.py --base http://127.0.0.1:8765             # terminal 2
```

`ui_smoke.py` parcurge toate paginile pe desktop (1440) și telefon (390) și eșuează la erori
de consolă, încălcări CSP, cereri eșuate sau scroll orizontal; capturile ajung în
`footypreds/artifacts/`. Împotriva serverului real consumă cereri FlashScore: folosește mock-ul.

Configurare (`.env`): `RAPIDAPI_KEY`, `DATABASE_PATH` (relativ la `footypreds/`), `CACHE_TTL_SECONDS` (implicit 900) și `HISTORY_CACHE_TTL_SECONDS` (implicit 21600).

Aplicația este gândită pentru localhost, fără autentificare. Nu o expune pe internet.

Versiunile Excel/VBA vechi au fost mutate în `..\FootyPreds-legacy-backup\`. Detalii în [footypreds/docs/MIGRATION.md](footypreds/docs/MIGRATION.md). Cheile RapidAPI din fișierele vechi trebuie revocate.
