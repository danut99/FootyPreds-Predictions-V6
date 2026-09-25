# FootyPreds V8

Analizor de meciuri de fotbal: **Python + FastAPI + SQLite**, o aplicație web și un
**client Excel** conectat la același API. Pentru fiecare meci calculează 1X2, șansă
dublă, peste/sub goluri, ambele marchează, goluri pe echipă, scor corect și
pauză/final. Toate provin din aceeași matrice de scoruri. Folosește forma echipelor
din **toate competițiile**, meciurile directe, clasamentul și cotele pieței.

> Probabilitățile sunt estimări statistice, nu garanții. Aplicația nu plasează pariuri. 18+.

## Pornire rapidă (Windows)

Necesită Python 3.11+. Din rădăcina proiectului:

```powershell
Copy-Item .env.example .env   # o singură dată, apoi completează RAPIDAPI_KEY
.\start.ps1                   # creează .venv, instalează dependențele, pornește serverul
```

Deschide **http://127.0.0.1:8000**. Fără cheie RapidAPI aplicația rulează în mod demo.
Pentru dezvoltare:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m uvicorn footypreds.api:app --reload --host 127.0.0.1 --port 8000
```

## Ce face

| Pagină | Conținut |
|---|---|
| **Predicții** | toate meciurile zilei pe competiții; tab-uri 1X2 / Goluri / GG / Scor corect / Pauză-Final; notă de calitate A–D; ponturile zilei; export Excel |
| **Pagina de meci** | rezumat, 1X2 model vs. piață, xG, hartă scor corect, total goluri, pauză/final, toate piețele cu cotă corectă și valoare (EV), formă ultimele 5/10 + acasă/deplasare, serii, meciuri directe, clasament, observații |
| **Bilete** | plan de 7 zile sau bilet pe o zi, cu cote 1X2 reale |
| **Track record** | prima selecție pre-meci, decontată automat, cu rată de reușită și benzi de calibrare |
| **Metodologie** | cum funcționează modelul și benchmark-ul |

API-ul este multi-sport (fotbal, baschet, tenis) și are deja, pentru interfața nouă:

- `GET /api/recommendations?day=…`: biletele zilei alese de AI pentru cotele 2, 5, 10 și 100 și cele mai sigure selecții simple, din toate sporturile;
- `POST /api/tickets/generate`: bilet la cota dorită, fără „probabilitate minimă”;
- `GET /api/live?sport=…`: meciuri live cu probabilități în timp real și cota minimă corectă;
- `POST /api/simulate`: simulare cu o sumă de bani pe meciuri din trecut, orb (walk-forward);
- `/api/wallet`: portofel virtual pe recomandările viitoare.

Contractele exacte (cu exemple JSON) sunt în [footypreds/docs/CONTRACTS.md](footypreds/docs/CONTRACTS.md). Sunt bani virtuali: estimări statistice, nu garanții. 18+.

- **Analiză completă**: o cerere FlashScore aduce ~50 de meciuri recente per echipă, din toate competițiile, plus H2H. A doua cerere aduce clasamentul. Rezultatele stau în cache 6 ore.
- **Sincronizează istoric**: încarcă rezultatele zilelor trecute, câte o cerere pe zi. Zilele încheiate nu se mai cer a doua oară. Astfel toate meciurile zilei au formă.
- Spre deosebire de V7, un meci **nu mai este refuzat** când echipele au puține meciuri directe sau puține meciuri în aceeași competiție. Primește predicție și o notă de calitate. Doar notele A–C intră în selecții.

## Excel

Există două moduri de a lucra în Excel:

1. **Clientul Excel** (`footypreds/excel_client/`). Este un modul VBA cu butoane care cheamă API-ul local: încarcă predicțiile zilei, analizează meciul selectat (formă, H2H, scor corect), valoarea și track record-ul. Alternativ, conexiuni Power Query fără macro-uri. Instalarea și butoanele sunt descrise în [footypreds/excel_client/README.md](footypreds/excel_client/README.md).
2. **Export instant**: butonul *Descarcă Excel* sau `GET /api/export.xlsx?day=AAAA-LL-ZZ`. Rezultatul este un workbook cu foile Predicții, Scor corect, Formă, Valoare, Pauză-Final și Legendă.

```powershell
.\.venv\Scripts\python.exe -m footypreds.cli export --day 2026-09-26
```

## Model și evaluare

Detalii în [footypreds/docs/MODEL.md](footypreds/docs/MODEL.md) și [footypreds/docs/EVALUATION.md](footypreds/docs/EVALUATION.md). Pe scurt:

- Ratingul atac/apărare este ponderat în timp și ajustat după adversari, cu shrinkage spre medie.
- Peste rating se aplică forma recentă și, cu pondere mică, H2H.
- Matricea de scoruri are corecția Dixon-Coles și e combinată cu cotele 1X2 când există.
- Parametrii au fost aleși **numai** pe sezonul de validare 2024–25. Sezonul 2025–26 este testul blocat: [footypreds/docs/BENCHMARK.md](footypreds/docs/BENCHMARK.md).

```powershell
.\.venv\Scripts\python.exe -m footypreds.evaluation.dataset   # arhive publice, fără RapidAPI
.\.venv\Scripts\python.exe -m footypreds.evaluation.run       # evaluare offline
.\.venv\Scripts\python.exe -m footypreds.evaluation.tennis_eval --download   # arhive tenis (tennis-data.co.uk)
.\.venv\Scripts\python.exe -m footypreds.evaluation.sim_datasets --download  # ligi suplimentare pentru simulator
.\.venv\Scripts\python.exe -m footypreds.simulator --dataset football --start 2024-08-01 --end 2025-06-30 --mode ticket --target 2 --stake 10
```

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
  web/              index.html, app.css, app.js
  evaluation/       benchmark reproductibil (dataset, run, tune, baseline V7)
  tests/            pytest, fără rețea
  scripts/          check_connections.py (rețea, opt-in), ui_smoke.py (browser)
  docs/             model, evaluare, benchmark, produs, migrare
  data/             SQLite și date benchmark (excluse din Git)
```

## Dezvoltare

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check footypreds
.\.venv\Scripts\python.exe -m ruff format --check footypreds
node --check footypreds/web/app.js
.\.venv\Scripts\python.exe -m playwright install chromium   # o dată
.\.venv\Scripts\python.exe footypreds/scripts/ui_smoke.py   # cu serverul pornit
```

Configurare (`.env`): `RAPIDAPI_KEY`, `DATABASE_PATH` (relativ la `footypreds/`), `CACHE_TTL_SECONDS` (implicit 900) și `HISTORY_CACHE_TTL_SECONDS` (implicit 21600).

Aplicația este gândită pentru localhost, fără autentificare. Nu o expune pe internet.

Versiunile Excel/VBA vechi au fost mutate în `..\FootyPreds-legacy-backup\`. Detalii în [footypreds/docs/MIGRATION.md](footypreds/docs/MIGRATION.md). Cheile RapidAPI din fișierele vechi trebuie revocate.
