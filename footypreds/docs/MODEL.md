# Modelul V8.1: rating, formă, H2H, piață și calibrarea golurilor

Codul este în `footypreds/engine/`. Toate piețele full-time provin din **aceeași matrice
de scoruri**, deci 1X2, goluri, GG și scorul corect nu se pot contrazice. Versiunea
`8.1-calibrated-goals` adaugă pasul 6 (calibrarea golurilor); restul este neschimbat.

## De ce V8

V7 folosea numai meciuri din **aceeași competiție** și cerea minimum 8 rezultate per
echipă. La cupe, meciuri europene, naționale sau începutul sezonului refuza predicția.
V8 folosește **toate competițiile** echipei și nu refuză: fiecare meci primește o
predicție și o notă de calitate a datelor (A–D).

## Pașii pentru un meci

1. **Istoric vizibil.** Doar rezultate terminate cu cel puțin 3 ore înainte de start,
   din ultimii 2 ani (`max_days`). Meciurile de juniori nu se amestecă cu seniorii;
   amicalele au pondere 0,5. Echipele sunt potrivite după ID FlashScore sau, pentru
   rândurile H2H fără ID, după nume; un omonim cu alt ID este exclus.
2. **Rating atac/apărare** (`ratings.py`). Model multiplicativ Maher/Dixon-Coles:
   `λ_gazde = μ · avantaj_teren · atac_gazde · apărare_oaspeți`,
   `λ_oaspeți = μ · atac_oaspeți · apărare_gazde`.
   Estimare iterativă ponderată în timp (timp de înjumătățire `half_life`), cu prior
   Gamma(k, k) centrat pe 1 (`prior`): echipele cu puține meciuri rămân aproape de medie.
   Pentru un meci se estimează pe echipe, adversarii lor și celelalte rezultate ale
   adversarilor (2 pași în graf), deci un 3-0 cu un adversar slab contează mai puțin.
3. **Forma recentă** (`form_factors`). Golurile marcate/primite în ultimele
   `form_window` meciuri, comparate cu ce aștepta ratingul, cu prior `form_prior`;
   multiplicatorul intră cu exponentul `form_weight`.
4. **Meciuri directe** (`h2h_factors`). Ultimele până la 6 întâlniri directe, aceeași
   formulă, pondere mică `h2h_weight`.
5. **Matricea de scoruri și piața 1X2** (`markets.py`). Poisson 0–12 goluri, corecție
   Dixon-Coles `rho` pentru 0-0, 1-0, 0-1, 1-1. Dacă există cote 1, X și 2 cu marjă
   plauzibilă (suma inverselor 0,98–1,4), probabilitățile 1X2 devin
   `p_model^(1−w) · p_piață^w` (normalizat), iar matricea este rescalată pe regiunile 1/X/2,
   păstrându-și forma în interiorul fiecărei regiuni.
6. **Calibrarea golurilor** (`analyzer.goals_calibration`, `markets.fit_total`). Vezi
   secțiunea de mai jos: P(peste 2,5) trece printr-o hartă Platt și, dacă există cotă
   peste/sub 2,5, printr-un amestec cu piața. Ambele rate de goluri se înmulțesc cu
   **același factor** ca matricea să dea exact această probabilitate, apoi se reaplică
   reponderarea 1X2 de la pasul 5. Toate piețele de goluri se mișcă împreună, iar 1X2
   rămâne identic.
7. **Piețe derivate.** 1X2, șansă dublă, peste/sub 0,5–4,5, GG/NG, goluri pe echipă,
   câștigă la zero, combinate (GG și peste 2,5, 1 și peste 1,5 etc.), scor corect,
   distribuția totalului de goluri. Pauza se modelează cu 44% din golurile ratelor
   rescalate în prima repriză; pauză/final este rescalat ca să respecte 1X2 final.
   `expected_goals` sunt ratele rescalate.
8. **Calitate.** Scor 0–100 din numărul efectiv de meciuri recente al fiecărei echipe
   (ponderat în timp), vechimea ultimului rezultat și existența cotelor.
   A ≥ 75, B ≥ 55, C ≥ 35, D sub 35. Nota D este afișată, dar nu intră în selecții.
9. **Selecția pentru jurnal.** Piața cu probabilitatea cea mai mare dintre cele 14
   piețe comparabile (1X2, șansă dublă, 1,5/2,5/3,5, GG/NG), numai dacă depășește pragul
   (implicit 85%) și nota este A–C.

## Calibrarea golurilor

### Problema măsurată (sezonul de validare 2024–25)

Pe predicțiile walk-forward din 2024–25 (16 ligi football-data.co.uk, 5485 meciuri),
P(peste 2,5) a modelului era **prea extremă**: deviația standard 0,106, față de 0,091 la
casele de pariuri. Când modelul spunea 28% peste 2,5, s-au înregistrat 47%. Când spunea
36%, s-au înregistrat 42%. „Sub 2,5” la 70–80% a ieșit în 53–57% din cazuri. Casele au avut
un log loss mai mic (0,6745 față de 0,6823). Selecțiile „cele mai sigure” erau aproape
toate peste/sub 2,5, adică exact piața unde modelul exagera. Pe sezonul de test 2025–26,
selecțiile din simulator afișau 56–64%, dar au ieșit 44–55%.

### Soluția (minimă, o singură matrice)

1. **Harta Platt** pe P(peste 2,5) brută (după reponderarea 1X2, cum rulează în aplicație):
   `logit p' = a + b · logit p`, cu `a = totals_intercept = 0,0556` și
   `b = totals_slope = 0,7241`. Panta sub 1 trage probabilitățile extreme spre mijloc.
   Harta este monotonă și rămâne în (0, 1). Identitatea este (0, 1).
2. **Amestecul cu piața** când există o cotă peste/sub 2,5 validă (suma inverselor
   0,95–1,4): `logit p* = (1−w) · logit p' + w · logit q`, unde q este probabilitatea
   pieței fără marjă. Pe validare, log loss a scăzut monoton până la
   `w = totals_market_weight = 1,0`. Deci, cu cotă, piața decide P(peste 2,5).
3. **Rescalarea**: factorul s pentru ambele rate (căutare Illinois pe log s, în
   intervalul ×0,25–×4) face ca P(peste 2,5) după reponderarea 1X2 să fie exact p*.
   P(peste 2,5) crește cu s în fiecare regiune 1/X/2, deci soluția este unică. Peste
   1,5/3,5, GG, goluri pe echipă, scorul corect și handicapurile se schimbă coerent.
4. `components.totals` din analiză arată `model_over25`, `calibrated_over25`,
   `market_over25`, `market_weight`, `over25` și `scale`.

Potrivirea este reproductibilă și folosește numai sezonul de validare:

```
python -m footypreds.evaluation.tune --totals          # 16 ligi, dacă sunt descărcate
python -m footypreds.evaluation.calibration --fit      # plus tabelele de fiabilitate
```

`evaluation/calibration.py` rulează predicțiile walk-forward ale sezonului 2024–25 cu
parametrii identitate. Harta Platt se potrivește prin verosimilitate maximă (Newton
amortizat). Ponderea se alege pe o grilă de 0,05, după log loss. Sezonul de test este
refuzat. Ligile și sezoanele sunt în `protocol.json` → `calibration`.

### Plafonul de valoare al recomandărilor (`recommend.MAX_VALUE = 1,05`)

O selecție intră în bilete numai dacă `0,95 ≤ probabilitate × cotă ≤ 1,05`. Peste 1,05,
modelul contrazice prețul mai mult decât marja casei. Nu există nicio dovadă măsurată că
modelul are dreptate în aceste cazuri. Pe validare, cu probabilitățile proprii ale
modelului pentru peste/sub 2,5 (fără calibrare), rezultatele au fost:

| probabilitate × cotă | selecții | ROI |
|---|---:|---:|
| 0,95–1,00 | 1948 | −6,2% |
| 1,00–1,05 | 1502 | −2,5% |
| 1,05–1,10 | 978 | −8,8% |
| 1,10–1,15 | 466 | −11,4% |
| 1,15–1,20 | 214 | +5,7% |
| 1,20–1,30 | 144 | −20,1% |
| ≥ 1,30 | 33 | −25,8% |

Plafonul cu ROI maxim pe validare este 1,05 (−4,6%, față de −6,1% fără plafon). Este o
**euristică prudentă, nu un optim potrivit**: 1,05 este cel mai bun din 7 plafoane încercate
pe aceleași ~3450 selecții (cote în jur de 1,9), iar diferențele dintre plafoane sunt de
ordinul unei erori standard a ROI (≈ 1,6 puncte procentuale; banda 1,15–1,20 a dat chiar
+5,7%). Populația pe care a fost măsurat (peste/sub 2,5 necalibrat) nu mai există: acum o
cotă peste/sub 2,5 fixează probabilitatea la prețul pieței. Regula se aplică totuși tuturor
piețelor și sporturilor, **fără o măsurare proprie** pentru 1X2, handicapuri, alte totaluri,
baschet sau tenis, fiindcă niciunul nu are un avantaj măsurat față de piață.

Simulatorul folosește exact aceeași regulă (`recommend.leg_allowed`, apelată și de
`simulator.model_legs`), cu excepția strategiei „value”, care testează intenționat
dezacordurile modelului cu prețul și nu are plafon.
În simulator, plafonul nu schimbă nimic pe seturile football-data: acolo, după calibrare,
probabilitățile selecțiilor cu cotă sunt practic cele ale pieței.

### Rezultate

Sezonul de test blocat 2025–26 (5 ligi, 1752 meciuri, `docs/BENCHMARK.md`). Valorile sunt
log loss ↓ / ECE ↓. „Piață” înseamnă cotele 1X2 și peste/sub 2,5.

| Piață | 8.0 fără cote | 8.1 fără cote | 8.0 + piață | 8.1 + piață | Case |
|---|---|---|---|---|---|
| Peste 1,5 | 0,5421 / 0,030 | 0,5397 / 0,011 | 0,5413 / 0,027 | 0,5329 / 0,008 | |
| Peste 2,5 | 0,6886 / 0,034 | 0,6844 / 0,015 | 0,6862 / 0,032 | 0,6761 / 0,017 | 0,6761 |
| Peste 3,5 | 0,6007 / 0,030 | 0,5982 / 0,015 | 0,6001 / 0,031 | 0,5912 / 0,010 | |
| GG | 0,6905 / 0,028 | 0,6875 / 0,007 | 0,6909 / 0,029 | 0,6833 / 0,009 | |

1X2 nu se schimbă deloc: pe test, log loss este tot 0,9944 fără cote și 0,9791 cu piață.
Pe cele 5485 de meciuri de validare, diferența maximă este 3·10⁻¹⁵.
Selecțiile de la pragul fix de 85% (model + piață) au trecut de la 489 selecții cu 87,9%
reușită la 401 selecții cu 91,5% reușită. Pentru „sub 2,5” ≥ 60%, 8.0 afișa în medie 64,7%
și au ieșit 53,6% (222 meciuri). 8.1 afișează 63,1% și au ieșit 56,7% (127 meciuri). Și
casele au fost prea încrezătoare în această bandă în 2025–26.

**Simulator** (miză fixă 10 din 1000; regulile recomandărilor; 2025-08-01 – 2026-05-24).
Aici rezultatele sunt **mai slabe** după calibrare:

| Set / strategie | 8.0: pariuri, profit, ROI | 8.1: pariuri, profit, ROI | Favoritul casei |
|---|---|---|---|
| football, bilet ×2 | 152, −296,32, −19,5% | 48, −126,80, −26,4% | −18,7% |
| football, simple (3/zi) | 483, −485,80, −10,1% | 338, −593,80, −17,6% | −1,4% |
| football-plus, bilet ×2 | 213, −189,14, −8,9% | 76, −160,40, −21,1% | −20,4% |
| football-plus, simple (3/zi) | 656, −688,60, −10,5% | 459, −770,50, −16,8% | −8,5% |

Cauza este măsurată. În 2025–26, cotele medii football-data au marja mai mare: 7,4% la
peste/sub și 1X2, față de 5,5–5,9% în sezoanele anterioare. Cu P(peste 2,5) egală cu
piața, `probabilitate × cotă` este 1/1,074 ≈ 0,93 și nicio selecție de goluri nu mai trece
pragul fix `MIN_VALUE = 0,95`. Rămân numai selecțiile 1X2 unde restul de 10% al modelului
este mai optimist decât piața. Acestea sunt, din nou, supraestimate: afișau în medie
38–49% și au ieșit 32–40%. Înainte de calibrare, selecțiile de goluri afișau 56–64% și au
ieșit 44–55%.

Pe sezonul de validare 2024–25, cu aceleași reguli, selecțiile sunt acum calibrate
(prezis / reușit). **Atenție: aceste cifre sunt în eșantion** — calibrarea golurilor și
plafonul de valoare au fost potrivite exact pe acest sezon (16 ligi), deci sunt optimiste.
Cifrele din afara eșantionului sunt cele din 2025–26 de mai sus; tot în eșantion sunt și
rulările scării pe football-plus 2024-08-01 – 2025-06-30.

| Set / strategie | 8.0: prezis / reușit, ROI | 8.1: prezis / reușit, ROI |
|---|---|---|
| football, bilet ×2 | 57,8% / 53,3%, −3,4% | 51,0% / 50,9%, −4,8% |
| football, simple | 63,8% / 61,4%, −3,3% | 60,2% / 60,1%, −5,4% |
| football-plus, bilet ×2 | 58,0% / 44,2%, −17,5% | 51,6% / 51,7%, −4,7% |
| football-plus, simple | 66,1% / 62,8%, −1,8% | 57,7% / 59,2%, −2,9% |

Pe validare, profitul însumat a trecut de la −727 la −604. Concluzia sinceră:
probabilitățile de goluri sunt acum corecte, dar modelul **nu are un avantaj** față de
prețurile pieței. Selecțiile pierd în jur de marja casei, iar un prag fix de valoare
selectează exact dezacordurile modelului cu piața. Pe validare, nicio variantă a pragului
(0,85–0,95, sau raportat la prețul fără marjă) nu a fost clar mai bună, așa că pragul nu
s-a schimbat.

## Parametri (aleși numai pe sezonul de validare 2024–25)

| Parametru | Valoare | Observație |
|---|---:|---|
| `half_life` | 540 zile | 365–730 aproape identice pe validare |
| `prior` | 8 | shrinkage spre medie |
| `rho` | −0,12 | mai multe egaluri la scor mic |
| `form_weight` | 0,15 | optimul de validare era 0; costă ~0,001 log loss, păstrat intenționat |
| `h2h_weight` | 0,1 | |
| `market_weight` | 0,9 | validarea continua să se îmbunătățească spre 1,0 (doar piața) |
| `totals_intercept` | 0,0556 | Platt pe P(peste 2,5), 16 ligi |
| `totals_slope` | 0,7241 | < 1: totalurile brute erau prea extreme |
| `totals_market_weight` | 1,0 | log loss minim la 1,0 (0,67447; 0,68019 la 0) |

Căutarea este în `footypreds/evaluation/tune.py`, iar calibrarea golurilor în
`tune.py --totals` / `evaluation/calibration.py`. Pe ligile de top, casele de pariuri
sunt foarte greu de bătut; modelul adaugă informație mai ales acolo unde nu există cote.

## Limite

- Nu știe accidentări, loturi, vreme, motivație sau teren neutru.
- Diferențele de nivel între ligi fără meciuri comune nu pot fi estimate corect.
- 1X2 nu este calibrat separat (amestec 0,9 cu piața). GG, goluri pe echipă și
  handicapurile urmează matricea rescalată, dar nu au o hartă proprie.
- Harta Platt a fost potrivită pe o singură linie (2,5) și un singur sezon; ECE pe
  sezonul de test rămâne 0,01–0,02.
- Probabilitatea unei selecții nu este un avantaj: fără o diferență măsurată față de piață,
  orice strategie pierde în medie cel puțin marja casei.
- Problema totalurilor a fost observată întâi pe sezonul 2025–26 (sezonul de test), apoi
  corectată numai cu parametri potriviți pe 2024–25. Câștigul de pe test confirmă o
  corecție motivată de test, nu este o descoperire oarbă.
- Seturile football-data folosesc cotele medii (coloanele Avg, marjă mai mare), nu cele mai
  bune cote pe care le compară aplicația: în simulator selecțiile peste/sub 2,5 (egale cu
  piața) cad sub pragul de valoare, deci simulatorul nu testează selecțiile de goluri pe care
  aplicația le poate recomanda.
