# FootyPreds V7

Aplicație locală de analiză fotbalistică: **Python + FastAPI + SQLite**, cu frontend **HTML/CSS/JavaScript**, fără framework sau build frontend. Excel nu mai este necesar.

**85% este un prag estimat de selecție, nu o acuratețe demonstrată.** Modelul actual este un reper Poisson necalibrat. Jurnalul real, evaluarea retrospectivă și demo-ul sunt separate.

## Pornire rapidă — Windows

Necesită Python 3.11+. Din rădăcina proiectului:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env  # doar dacă nu ai deja .env
```

Completează `RAPIDAPI_KEY` în `.env`, apoi rulează ` .\start.ps1` și deschide **http://127.0.0.1:8000**. Scriptul creează mediul și instalează dependențele dacă `.venv` lipsește. Pentru dezvoltare cu restart automat:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Cheia furnizată pentru această instalare este deja în `.env`, exclus din Git. Nu apare în HTML, JavaScript sau răspunsurile backendului. Fără cheie, demo-ul și importul CSV funcționează în continuare.

### VS Code Live Server

Poți deschide `web/index.html` cu Live Server pe **5500 sau 5501**. CSS și JavaScript folosesc căi relative; nu este necesar un folder fizic `static`. Backendul Python trebuie să ruleze separat pe **8000**. CORS permite explicit aceste origini locale. Dacă ai versiunea veche în cache, folosește **Ctrl+F5**.

`/static` este doar un alias oferit de FastAPI pentru fișierele din `web/`. Deschiderea prin `file://` poate afișa stilurile, dar nu este suportată pentru cererile API.

## Utilizare

### Ticket Lab și Plan mode

Pagina principală este acum **Ticket Lab**, cu o interfață gamified, calendar de progres și bilete persistente:

Direcția pentru modulele Daily Tips, Goals Lab și Correct Score este descrisă în [planul produsului](docs/PRODUCT.md), cu stadiul fiecărei funcții.

- **Plan 7 zile**: câte un bilet pentru fiecare zi, cu ținta implicită de cotă 2. Datele planului sunt în UTC.
- **Bilet custom**: cota aleasă (inclusiv 10), data, maximum 1–5 selecții și probabilitatea minimă per selecție.
- Generatorul caută cota disponibilă cea mai apropiată în intervalul ±10%; nu inventează o cotă exactă. Pentru cotă 10, presetul permite maximum 5 selecții.
- O singură selecție per meci; fără echipe repetate. Opțional, fiecare selecție provine din altă ligă.
- Biletele reale folosesc **numai cotele 1X2** furnizate de lista FlashScore. Nu există ofertă verificată pentru toate piețele modelului. Cotele pot proveni din surse diferite și trebuie reverificate la aceeași casă.
- Fiecare zi analizează maximum 8 meciuri: până la 17 cereri fără cache/zi, 119/săptămână. Cache-ul reduce acest număr. Meciurile eligibile insuficiente lasă ziua fără bilet, cu explicație.
- Generarea rulează în fundal, afișează progresul și se poate urmări după reîncărcarea paginii. Un restart întrerupe jobul, păstrând biletele deja create. Se rulează o singură generare simultan, într-un singur proces Uvicorn.
- **Planurile mele** păstrează ultimele 30 de planuri în listă. **Verifică rezultate** preia scorurile pentru zilele ajunse la termen. O selecție pierdută face biletul nereușit; toate câștigate îl fac reușit. Meciurile fără scor final rămân în așteptare.
- **Demo** are echipe, scoruri și cote sintetice, nu contribuie la statisticile reale și nu consumă RapidAPI.

Pragul generatorului este separat de filtrul de 85% din Match center. Probabilitatea combinată este produsul probabilităților individuale, sub ipoteza aproximativă de independență; nu este validată pentru bilete și nu garantează rezultat. Nu există plasare automată de pariuri sau mize.

### Match center și evaluare

1. **Meciuri**: alege data și încarcă FlashScore. Poți căuta după echipă sau ligă.
2. **Analizează**: încarcă prima pagină de rezultate pentru fiecare echipă, cu cache de 15 minute. Maximum două cereri externe per analiză, când cache-ul lipsește. Analiza în lot procesează maximum 10 meciuri.
3. **Selecții**: minimum 8 rezultate per echipă, unul în ultimele 90 de zile, plus pragul ales. Maximum o piață per meci. Lipsa datelor sau a probabilității necesare produce „Fără selecție”.
4. **Rezultate**: prima selecție eligibilă salvată înainte de start rămâne neschimbată. Încarcă din nou data meciului după final pentru decontare. Pragurile schimbate ulterior nu rescriu jurnalul.
5. **Backtesting**: evaluează istoricul local sau un CSV. Rezultatele retrospective nu intră în jurnalul prospectiv. Demo-ul folosește echipe și scoruri sintetice, etichetate explicit.

Predicțiile sunt exclusiv înainte de start. Nu sunt implementate predicții live, cornere sau mize automate. Cotele 1X2 sunt folosite în biletele de analiză; cotele istorice nu sunt date de intrare pentru model.

## Model și evaluare

- Rate de goluri cu avantajul terenului, ponderare temporală (timp de înjumătățire 180 zile) și regularizare către media ligii.
- Maximum 30 de observații recente per echipă; istoric de maximum 730 zile.
- O singură matrice Poisson normalizată produce 1X2, șansă dublă, total goluri și ambele marchează.
- Backtest cronologic: exclude rezultatul meciului evaluat, meciurile simultane și rezultatele mai apropiate de start decât 3 ore.
- Acuratețe, volum, acoperire, Brier score pentru selecții și interval Wilson 95%.
- Criteriul intern pentru susținerea țintei: minimum 100 selecții evaluate și limita inferioară Wilson ≥85%. Criteriul este orientativ: meciurile pot fi corelate, iar rezultatele nu garantează performanțe viitoare.

CatBoost/LightGBM sunt candidați pentru etapa următoare, **nu modele deja antrenate în această versiune**. Comparația corectă necesită mai multe sezoane, caracteristici disponibile la momentul predicției și o perioadă de test neatinsă. Vezi [planul modelului](docs/MODEL.md).

## Import și colectare istoric

### Benchmark strict pe meciuri reale

În **Backtesting → Deschide raportul strict**, găsești evaluarea pe **7.156 de meciuri reale**: 5.404 pentru istoric inițial și 1.752 în sezonul de test 2024–2025. Sunt cinci ligi: Premier League, La Liga, Bundesliga, Serie A și Ligue 1.

Rezultat pentru modelul inițial, fără ajustare după test: **52,1% la 1X2**, respectiv **428/477 selecții reușite (89,7%)** la prag fix 85%, cu **27,2% acoperire**. Cotele normalizate au avut 53,6% la 1X2; modelul nu le-a depășit. Procentul selecțiilor pe piețe mixte nu este rezultat pentru bilete combinate și nu garantează performanța viitoare.

```powershell
.\.venv\Scripts\python.exe -m evaluation.dataset  # 20 CSV-uri publice, fără RapidAPI
.\.venv\Scripts\python.exe -m evaluation.run      # evaluare offline, fără cereri externe
```

Datele brute, checksum-urile și predicțiile individuale sunt în `data/benchmark/`, excluse din Git. [Raport complet](docs/BENCHMARK.md) · [Protocol și teste adversariale](docs/EVALUATION.md).

### CSV și istoric FlashScore

CSV UTF-8, maximum 2 MB / 3000 meciuri; `kickoff` include fusul orar. Golurile trebuie să fie scoruri finale pentru timpul regulamentar. ID-urile și meciurile duplicate sunt respinse.

```csv
id,kickoff,league,home,away,home_goals,away_goals
m1,2025-01-01T18:00:00+00:00,Example League,Team A,Team B,2,1
```

Colectare reală, maximum 31 zile per comandă; fiecare zi fără cache consumă o cerere API:

```powershell
.\.venv\Scripts\python.exe -m app.cli collect --start 2026-09-01 --end 2026-09-07
.\.venv\Scripts\python.exe -m app.cli backtest --threshold 0.85
```

Păstrează aceeași denumire a echipei și a ligii în CSV. Datele xG vechi fără dată de referință nu sunt importate automat.

## RapidAPI MCP

Backendul folosește REST pentru acces predictibil și cache; MCP este disponibil separat pentru clienți AI. Nu este necesar pentru pornirea aplicației.

`mcp.example.json` configurează bridge-ul Python către `mcp-remote@0.14.3`. Adaptează cele două căi absolute dacă muți proiectul și adaugă intrarea în configurația clientului MCP. Necesită Node.js/npx; prima pornire poate descărca pachetul. Cheia este citită din `.env` și transmisă prin variabilă de mediu, fără a fi scrisă în JSON.

Verificare explicită a conexiunilor reale (consumă cereri din abonament):

```powershell
.\.venv\Scripts\python.exe scripts/check_connections.py --mcp
```

Pe 21 septembrie 2026 au fost verificate REST, `initialize` și `tools/list`; MCP a expus 44 de instrumente. Aceasta verifică serviciul, fără a înregistra automat serverul în clientul AI. Parametrii bridge-ului urmează [documentația mcp-remote](https://github.com/punkpeye/mcp-remote#custom-headers).

## Structură și dezvoltare

```text
app/          API, model, adaptor FlashScore, SQLite, CLI și bridge MCP
web/          index.html, style.css, app.js
tests/        teste unitare și de integrare, fără rețea
scripts/      verificări explicite de conexiune și browser
data/         SQLite/cache local, exclus din Git
evaluation/   protocol fix, dataset real și evaluator cu scoruri ascunse
docs/         model, migrare, protocol și raport benchmark
legacy/       arhiva V6; nu este folosită de runtime
```

Python: patru spații, `snake_case`, constante `UPPER_CASE`, Ruff. Instrucțiunile VBA din `AGENTS.md` descriu versiunea veche și au fost păstrate nemodificate; comenzile V7 sunt cele de aici.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests scripts evaluation
.\.venv\Scripts\python.exe -m ruff format --check app tests scripts evaluation
node --check web/app.js
```

Verificări browser, cu serverul Python pornit; nu consumă API extern:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-browser.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe scripts/browser_smoke.py
.\.venv\Scripts\python.exe scripts/live_server_smoke.py  # portul 5501 trebuie să fie liber
.\.venv\Scripts\python.exe scripts/studio_smoke.py       # plan 7 zile, custom 10, raport strict
.\.venv\Scripts\python.exe scripts/competitions_smoke.py # ligi multiple, naționale, filtre și mobil
```

Capturile sunt în `artifacts/`, exclus din Git. GitHub Actions rulează verificările Python fără secrete. În PR descrie schimbarea, testele, efectele asupra modelului și adaugă capturi pentru modificări UI.

## Configurație și arhivă

`DATABASE_PATH` configurează SQLite; `CACHE_TTL_SECONDS` configurează cache-ul. Aplicația este concepută pentru localhost, fără autentificare publică. Nu o expune pe internet în această formă.

Versiunile vechi au fost mutate, nu șterse. Arhiva, workbookul și extragerea locală pot conține chei istorice; sunt excluse din noile adăugări Git. Cheile deja expuse în mesaje sau versiuni vechi trebuie înlocuite în RapidAPI; mutarea fișierelor nu le revocă și nu curăță istoricul Git. Vezi [migrarea](docs/MIGRATION.md).
