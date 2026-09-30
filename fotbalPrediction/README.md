# fotbalPrediction v1 — predicții de fotbal fără leakage

Site separat (portul 8020) și model propriu pentru fotbal, antrenat walk-forward pe fișierele
CSV football-data.co.uk (22 de ligi principale, 0506 → sezonul curent 2627). Structura imită
`tenisPrediction/`. Istoricul complet, candidații, auditurile și toate tabelele sunt în
[`EXPERIMENTS.md`](EXPERIMENTS.md).

## Pe scurt, cinstit

- **1X2 pe toate meciurile rămâne la ~50%** (log-loss 1.015 pe 2425 fără cote); cu cotele
  pre-meci modelul ajunge practic la piață (1.0006 față de ~1.000 pentru piața fără marjă).
  Modelul **nu bate casele de pariuri**.
- **80% / 85% se ating doar pe selecții**, pe fiecare **grup de piață** (șansă dublă, goluri,
  handicap, pauză, cornere, cartonașe, șuturi pe poartă): pe sezonul de confirmare 2425 (în
  afara eșantionului) toate grupurile au 82–88% cu regula de 80% și 86–91% cu regula strictă.
- Testul blocat 2526 (rulat o singură dată, la final): regula de 80% a dat 81.1–89.7% pe grup
  (84.4% pe toate selecțiile), regula strictă ≥ 85.1% pe fiecare grup cu excepția 1X2 fără cote
  (81.8%, doar 22 de selecții); 1X2 log-loss 1.017 fără cote și 1.0025 cu cote. Detalii verbatim
  în EXPERIMENTS.md.
- Prețul acestei precizii: **cote scurte**. Cota corectă medie a selecțiilor este ~1.19 (regula
  80%) și ~1.14 (regula 85%). Oriunde există preț real (1X2, șansă dublă derivată, peste/sub 2.5,
  linia asiatică listată), randamentul la miză fixă a fost **negativ**: de ex. șansă dublă
  -5.1% (2425, fără cote, 1911 pariuri), toate selecțiile cu preț -3.8% … -4.9% (2425) și -4.5% … -5.1% (2526).
  Cornerele, cartonașele, pauza, șuturile, GG/NG și liniile alternative de goluri **nu au prețuri
  istorice**: pentru ele raportăm doar precizia, acoperirea și cota corectă, niciodată profit.
- Unele chei individuale coboară sub țintă chiar dacă grupul o atinge (lista în EXPERIMENTS.md);
  selecțiile din același meci (cartonașe, puncte cartonașe, cartonașe pe echipă) sunt corelate,
  deci nu le combina între ele ca și cum ar fi independente.

## Cum funcționează

- `data.py` (harness comun): descărcarea și parsarea CSV-urilor football-data (coloane de
  rezultat, pauză, șuturi, șuturi pe poartă, cornere, faulturi, cartonașe, arbitru și cote;
  cotele „C” sunt de ÎNCHIDERE și sunt etichetate separat).
- `markets.py` (harness comun): catalogul de piețe (chei compatibile cu nucleul FootyPreds plus
  pauză, cornere, cartonașe, puncte cartonașe galben=1/roșu=2, șuturi pe poartă), decontarea și
  probabilitățile dintr-o singură matrice (acasă, deplasare) pe statistică.
- `benchmark.py` (harness comun): walk-forward pe zile, cu predicțiile unei zile făcute înaintea
  oricărui rezultat din aceeași zi; sezoane: tuning 2223/2324, confirmare 2425, test blocat 2526
  (refuzat fără `--locked-test`), 2627 niciodată evaluat.
- `model.py` — modelul de producție (compunere de candidați auditați):
  - goluri + pauză: `candidates/goals_model.py` (Dixon-Coles pe ligă cu țintă xG din șuturi pe
    poartă, fit separat pentru totalul de goluri, ratinguri dinamice Kalman, prior pentru echipele
    promovate/retrogradate, cota de goluri la pauză pe ligă);
  - cornere, cartonașe, puncte cartonașe, șuturi pe poartă: `candidates/corners_cards.py`
    (binomial negativ + copulă Frank, ratinguri pe echipă, arbitru pentru Anglia/Scoția);
  - cu cote reale: marja scoasă cu metoda „power”, apoi 1X2 și peste/sub 2.5 amestecate cu piața
    cu pondere FIXĂ 0.9 (ideea din `candidates/odds_blend.py`); restul matricei se aliniază.
  - regula de selecție: listă de piețe permise înghețată pe 2223+2324 (`selection_rule.json`,
    produsă de `tune_rule.py`), `0.80 ≤ p ≤ 0.93` (strict `0.85`), ambele echipe cu cel puțin 5
    meciuri de ligă în ultimii 2 ani și **o selecție pe grup de piață și meci** (cheia eligibilă
    cu probabilitatea cea mai mică, deci cota cea mai lungă). Peste 0.93 cota corectă e sub
    ~1.08: nu se selectează.
  - `FootballPredictor`: modelul antrenat pe toate datele locale (inclusiv 2627 parțial),
    potrivirea numelor FlashScore și răspunsul complet pe piață (probabilitate, cotă corectă,
    cotă reală când există, „selectează”/„fără pariu”, decizia strictă).
- `names.py` + `team_overrides.json`: liga FlashScore → cod football-data, echipa FlashScore →
  numele football-data (suprascrieri manuale, potrivire exactă, aceleași cuvinte, conținere unică,
  `difflib` cu prag și avans). O potrivire ambiguă nu se ghicește: meciul rămâne „fără model”.
- `train.py`: antrenare + pickle în `footypreds/data/fotbalPrediction/` cu cheia
  VERSION + amprenta codului modelului (`MODEL_SOURCES`: `model.py`, `markets.py`, `data.py`,
  `benchmark.py`, `candidates/goals_model.py`, `candidates/corners_cards.py`) + parametri +
  regulă + amprenta fișierelor de date (~1 minut). Orice modificare a acestor fișiere (chiar și
  doar de formatare) reantrenează la următoarea pornire; celelalte candidate nu intră în model.
- `app.py` + `web/`: aplicația de pe portul 8020 (vezi mai jos).

## Instalare, date, antrenare, pornire

Python 3.11+ (numpy, scipy, fastapi, httpx; aceleași cerințe ca FootyPreds).

```powershell
# 1) datele football-data.co.uk (≈60 MB, o singură dată; pauză politicoasă între cereri)
python -m fotbalPrediction.data --download
# actualizarea sezonului curent (2627), de ex. marțea și vinerea:
python -m fotbalPrediction.data --download --refresh
python -m fotbalPrediction.data --coverage --seasons 2223,2324,2425   # tabel de acoperire

# 2) antrenare (opțional: aplicația antrenează singură în fundal la pornire)
python -m fotbalPrediction.train

# 3) aplicația (FootyPreds rulează montat sub /core; cheia RapidAPI din .env ca de obicei)
python -m uvicorn fotbalPrediction.app:app --host 127.0.0.1 --port 8020
```

Deschide http://127.0.0.1:8020. Cât timp modelul se antrenează, `/api/health` răspunde
`"status": "loading"`, iar pagina afișează rezerva FootyPreds.

## Aplicația

- Tabla zilei vine de la `/core/api/predictions?sport=football` (FlashScore, cu paginare), apoi
  meciurile, cu cotele lor 1/X/2, se trimit la `POST /api/fotbal-probabilities`.
- Filtre: stare meci, format (meciuri / 2 combinate din meciuri diferite), meciuri cu model /
  toate, șansă minimă (toate, ≥75%, ≥80%, ≥85% = regula strictă, „sigur + cotă” = selectată de
  regula de 80% și cotă reală sau corectă ≥ 1.20), tip pariu (1X2, șansă dublă, goluri, goluri pe
  echipă, GG/NG, handicap, scor exact, pauză, cornere, cartonașe, șuturi pe poartă).
- Zilele încheiate arată câștigat/pierdut pe selecție. Golurile se decontează din scorul final
  FlashScore (prelungirile și penalty-urile rămân nedecontate). Pauza, cornerele, cartonașele și
  șuturile se decontează **numai** din rândul football-data al meciului (zero apeluri RapidAPI),
  deci rămân „nedecontate” până când `--refresh` aduce rândul și aplicația reîncarcă modelul; nu
  intră în statistici până atunci. Cât timp rândul lipsește, selecția afișată pe un meci încheiat
  (și cele 3 selecții pe meci folosite la „2 combinate”) pune în față selecțiile de goluri,
  decontabile imediat; ordinea depinde doar de tipul pieței, nu de rezultat. Cardul spune când
  selecția principală a fost amânată. După sosirea rândului se revine la selecția principală.
- Reîncărcare: cel mult o dată la 5 minute (la cereri), aplicația compară cheia pickle-ului cu
  datele și codul de pe disc; dacă diferă (după `python -m fotbalPrediction.data --download
  --refresh`), reantrenează în fundal și servește modelul vechi până e gata (~1 minut).
- Predicțiile făcute înainte de start se salvează în `footypreds/data/fotbalPrediction/
  journal.sqlite3`. Fără predicție în jurnal, un meci e marcat „retroactiv” dacă ziua lui este cel
  mult ultima zi de antrenare SAU dacă modelul are deja rândul football-data al meciului (±1 zi:
  un meci de după miezul nopții la București e datat cu o zi mai devreme în football-data).
  Retroactivele nu intră în precizia zilei, iar o combinație din „2 combinate” care conține un
  meci retroactiv apare „NEVERIFICABILĂ” și nu intră în precizia verificată.
- Meciurile din ligi fără model (echipe naționale, cupe, alte țări) sau cu echipe nerecunoscute,
  dar cu cote 1/X/2 în FlashScore, primesc **estimarea din cote** (`market_model.py`): marja
  scoasă din cote, totalul de goluri dedus din probabilitatea de egal, apoi toate piețele de
  goluri (șansă dublă, peste/sub, goluri pe echipă, GG/NG, handicap, scor exact) dintr-o singură
  matrice de scor. Selecțiile urmează lista înghețată `market_rule.json` (41 de chei la 80%, 35
  la 85%). Fără cornere, cartonașe, șuturi sau pauză (nu există istoric de echipă). Filtrul
  „Meciuri afișate”: „Model + cote” (implicit), „Doar model”, „Toate”.
- Meciurile fără cote afișează probabilitățile FootyPreds de bază, marcate clar, fără selecții.
  `GET /api/unresolved` listează numele de adăugat în `team_overrides.json`.
- LIVE: `/core/api/live?sport=football` (la cerere, un refresh pe apăsare) și statisticile
  meciului la `/core/api/live/{id}` (un apel RapidAPI).
- Rute: `/`, `/api/health`, `/api/teams?q=`, `/api/predict?home=&away=&league=E0&day=&odds_1=
  &odds_x=&odds_2=[&full=true]`, `POST /api/fotbal-probabilities`, `/api/unresolved`, `/core/...`.

## Benchmark și teste

```powershell
$env:PYTHONIOENCODING = "utf-8"
python -m fotbalPrediction.benchmark --model fotbalPrediction.model:benchmark_factory --seasons 2223,2324 [--odds avg]
python -m fotbalPrediction.tune_rule          # rederivează selection_rule.json (numai 2223+2324)
python -m fotbalPrediction.market_eval --report   # estimarea din cote pe 2223+2324 (--derive rederivează market_rule.json)
python -m pytest -q footypreds/tests/test_fotbal_benchmark.py footypreds/tests/test_fotbal_model.py footypreds/tests/test_fotbal_app.py footypreds/tests/test_fotbal_market.py
python -m ruff check footypreds fotbalPrediction
python -m ruff format --check footypreds fotbalPrediction
node --check fotbalPrediction/web/app.js
```

Confirmarea 2425 se rulează o dată, după înghețarea regulii; testul blocat 2526 o dată pe
versiune (`--seasons 2526 --locked-test`), numai de integrator.

## Limite cunoscute

- Numele FlashScore ale ligilor de top nu au putut fi verificate pe date locale; suprascrierile din
  `team_overrides.json` sunt un punct de plecare și trebuie completate din `/api/unresolved`.
- Arbitrul (folosit la cartonașe în Anglia/Scoția) nu vine de la FlashScore: în producție modelul
  rulează fără arbitru (varianta `--no-referee` a costat ~0.003 log-loss pe cartonașe). Cifrele de
  cartonașe din benchmark (inclusiv 2425/2526) sunt calculate CU arbitru; pe tuning 2223+2324, în
  ligile E*/SC*, fără arbitru selecțiile pierd 0-0.4 puncte la regula de 80% și 0.3-1.5 puncte la
  regula strictă (tabelul în EXPERIMENTS.md).
- Aplicația reîncarcă modelul singură după un `--refresh` (verificare la cel mult 5 minute), dar
  reantrenarea durează ~1 minut; până atunci servește modelul vechi.
- Liniile asiatice sfert (x.25/x.75) se decontează pe jumătăți; „câștigat” include și jumătatea
  câștigată, ca în benchmark. Nucleul FootyPreds nu le decontează.
- Ligile suplimentare (ROU, AUT, …) au doar scor și cote de închidere: nu au model de echipă;
  în aplicație primesc estimarea din cote, ca orice alt meci fără model.
- Estimarea din cote a fost validată numai pe ligi de seniori (22 principale + 16 suplimentare).
  Pe amicale, echipe naționale, fotbal feminin, tineret și cupe regula se aplică la fel, dar nu
  a fost măsurată acolo. Depinde de calitatea cotelor din lista FlashScore (o singură casă, cu
  marjă) și, ca tot restul, nu bate piața: selecțiile au cote scurte și ROI negativ la cote reale.
- Convenția cartonașelor diferă (Anglia/Scoția nu numără primul galben al unui dublu galben);
  decontarea urmează football-data, o casă de pariuri poate deconta altfel.
