# Experimente fotbalPrediction (v1, 29.09.2026)

## Protocol

- Date: football-data.co.uk, 22 de ligi principale (E0-E3, EC, SC0-SC3, D1, D2, I1, I2, SP1,
  SP2, F1, F2, N1, B1, P1, T1, G1), sezoanele 0506 → 2627. Ligile suplimentare (`new/*.csv`)
  au doar scor și cote de ÎNCHIDERE și nu sunt evaluate.
- Sezoane: istoric/burn-in până la 2122; **tuning numai pe 2223 și 2324**; **confirmare o
  singură dată pe 2425** după înghețarea regulii; **test blocat 2526** o dată pe versiune, la
  final, numai de integrator; 2627 (curent, parțial) doar pentru producție.
- Walk-forward pe zile (`benchmark.py`): toate meciurile unei zile se prezic înainte ca vreun
  rezultat din acea zi să ajungă la model. Contextul are numai câmpuri pre-meci. Cotele intră
  doar cu `--odds`: `avg` = media PRE-închidere (colectată marți/vineri), `closing` = coloanele
  „C” (de ÎNCHIDERE, etichetate ca atare).
- Metrici pe cheie, pe grup de piață (cea mai bună selecție pe meci și regula modelului, cumulată
  pe grup), pe întrebare (1X2, pauză 1X2, scor exact) și pe ligă. ROI numai la prețuri reale:
  1X2, șansă dublă (preț derivat din 1X2, marja păstrată), peste/sub 2.5 și linia asiatică
  listată. Restul piețelor au doar cotă corectă (1/p): **nu se raportează profit**.
- Notație în tabele: `acuratețe / acoperire / cotă corectă medie` (acoperire = meciuri cu cel
  puțin o selecție în grup), `ROI @ pariuri cu preț`.

## Referințe (harness, 2223 | 2324, 22 de ligi)

| Variantă | 1X2 log-loss | 1X2 acuratețe |
|---|---|---|
| baseline fără cote | 1.0133 \| 1.0145 | 49.7% \| 49.3% |
| baseline + cote avg (pondere 0.8) | 0.9968 \| 0.9970 | 51.2% \| 50.9% |
| numai piața (avg, marjă proporțională) | 0.9955 \| 0.9955 | 51.3% \| 50.8% |
| nucleul V8 FootyPreds fără cote | 1.0124 \| 1.0152 | 50.0% \| 49.0% |
| nucleul V8 + cote | 0.9956 \| 0.9961 | 51.4% \| 51.0% |

Regula implicită a baseline-ului (p ≥ 0.80 pe orice cheie) face ~34 de selecții pe meci la cotă
corectă 1.13, cu ROI -3.4% / -3.8% la prețuri reale: nu e o regulă utilizabilă.

## Candidați și verdictul auditului

Toți cei cinci candidați au fost auditați independent: **fără leakage** (verificat și empiric,
prin perturbarea rezultatelor de după o dată: predicțiile de până la acea dată rămân identice),
**metrici reproduse exact**, protocol respectat, risc de supra-ajustare **scăzut**.

### goals_model — matrice de scor pentru goluri și pauză ✅ integrat

Dixon-Coles pe ligă reajustat zilnic (half-life 360, prior 4), țintă „xG” din goluri + șuturi
pe poartă (0.35) + șuturi (0.10), fit separat și mult mai contras pentru totalul de goluri,
ratinguri dinamice (filtru Kalman pe log-atac/apărare, 50/50 cu fitul), prior pentru echipele
promovate/retrogradate din liga anterioară, cota de goluri la pauză pe ligă.

| Metrică (fără cote) | goals_model 2223 \| 2324 | baseline | goals_model 2425 | baseline 2425 |
|---|---|---|---|---|
| 1X2 log-loss | 1.0056 \| 1.0090 | 1.0133 \| 1.0145 | 1.0148 | 1.0204 |
| pauză 1X2 | 1.0415 \| 1.0487 | 1.0446 \| 1.0541 | 1.0484 | 1.0500 |
| scor exact | 2.7825 \| 2.8364 | 2.8020 \| 2.8508 | 2.8038 | 2.8176 |
| peste 2.5 | 0.6782 \| 0.6791 | 0.6833 \| 0.6844 | 0.6843 | 0.6884 |
| acoperire ipotetică 80% șansă dublă | 76.5% \| 66.1% | 58.2% \| 59.2% | 60.8% | 56.4% |

Cu cote avg, ponderile potrivite de autor au fost 1.0/1.0 (modelul devine piața pe 1X2 și
peste/sub 2.5 și modelează doar restul matricei). Auditul: câștigul e structural (xG și
ratingurile dinamice), s-a menținut pe 2425; o mică inconsecvență de ordine în rata de
conversie șuturi→goluri din `_ingest_dynamic` (fără leakage) rămâne documentată, nu reparată,
fiindcă ar schimba predicțiile după confirmare.

### corners_cards — cornere, cartonașe, puncte cartonașe, șuturi pe poartă ✅ integrat

Nivel pe ligă, ratinguri „pentru/contra” pe echipă (prior 20 de meciuri, half-life 180),
corecție de total învățată walk-forward (luna sezonului, |superioritate|, contracție),
binomial negativ + copulă Frank pentru perechea (gazde, oaspeți), factor de arbitru (Anglia și
Scoția), puncte cartonașe = cartonașe + roșii. Regula proprie: listă de 70 de linii calibrate pe
2223+2324, `0.80 ≤ p ≤ 0.93`, o selecție pe grup și meci.

| Familie (log-loss) | baseline cu echipe 2425 | corners_cards 2425 |
|---|---|---|
| cornere peste/sub | 0.6544 | 0.6455 |
| cornere pe echipă | 0.6340 | 0.6235 |
| handicap cornere | 0.6216 | 0.6105 |
| cartonașe | 0.6010 | 0.5865 |
| puncte cartonașe | 0.5943 | 0.5814 |
| șuturi pe poartă | 0.6387 | 0.6334 |

Auditul: ținta de 80% ține la nivel de grup, nu pe fiecare cheie (câteva chei au 72–77% pe
2425); selecțiile din același meci sunt corelate; nu există prețuri reale, deci nu există ROI.

### odds_blend — piața fără marjă + amestec ✅ ideea integrată (pondere fixă 0.9)

Patru metode de scos marja (power a fost cea mai bună pe 2223, ~0.001 nat față de proporțional),
ponderi învățate walk-forward. Rezultat: ponderea învățată a modelului ≈ -0.1 și a pieței ≈ 1.1,
iar câștigul față de piața singură (-0.00036 nat) are interval de încredere care include 0
(recalcul în audit). Recomandarea autorului și a auditului pentru producție: **pool fix, pondere
piață 0.9**, cote acceptate doar cu marjă plauzibilă, rezervă = modelul. Regula ei de selecție
(prețuri reale, o selecție pe meci) a dat 84.0% / 30.4% / 1.20 cu ROI -2.9% pe tuning și
82.8% / 34.4% cu ROI -4.2% pe 2425. Neintegrate: ponderile învățate (amplifică zgomotul
cotelor FlashScore) și regula de selecție (învățată fără încălzire, vezi auditul).

### rating_stack — trăsături de rating + stivă ML ❌ neintegrat

Elo, pi-ratings, formă pe goluri/șuturi/cornere, clasament, stivă logistică + gradient boosting
reantrenată pe sezon. Cel mai bun 1X2 fără cote (1.0007 \| 1.0038; 2425: 1.0112 față de 1.0148
la goals_model), dar cu cote câștigul dispare (2425: 0.9996 față de piața 1.0001), rulează de
~5 ori mai lent, depinde de sklearn/threadpool și auditul a găsit un bug latent (stiva cu cote
nu se construiește dacă primul meci al sezonului nu are preț) și un tuner nereproductibil.
Aplicația are aproape mereu cote 1X2 pentru ligile acoperite, deci câștigul nu justifică
complexitatea. Candidat pentru v2.

### selector — calibrare simetrizată + praguri Wilson ❌ neintegrat

Calibrare Platt pe perechi complementare și praguri învățate walk-forward cu podea 0.80/0.85.
Reduce ECE pe baseline, dar alege ~33 de selecții pe meci, pierde acoperire pe piețele utile
(sub 3.5, peste 1.5, cartonașe sub) și grupul 1X2 rămâne fără selecții. Regula integrată (listă
înghețată + o selecție pe grup) atinge aceleași ținte pe grup cu ~11 selecții pe meci.

## Ce s-a integrat (model v1 = `fotbal-1.0`)

- `FootballModel` = goals_model (goluri + pauză, parametrii impliciți ai candidatului) +
  corners_cards (numărători, parametrii și regula candidatului) + cote: marja scoasă cu „power”,
  pool 0.9 pe 1X2 și peste/sub 2.5 (`PRODUCTION_ODDS_WEIGHTS`), carte acceptată doar cu suma
  inverselor în 0.97–1.25.
- Regula pentru goluri și pauză (`tune_rule.py`, numai 2223+2324, rulat fără cote ȘI cu cote avg):
  o cheie e permisă pentru ținta T dacă în AMBELE variante are ≥ 30 de selecții în banda
  `[T, 0.93]` (cu pragul de experiență), acuratețe cumulată ≥ T, supra-încredere ≤ 3 puncte și
  niciun sezon (≥ 20 selecții) sub T - 2 puncte. Rezultat: 40 de chei pentru 80%, 35 pentru 85%
  (`selection_rule.json`, cu statisticile pe cheie). În producție se alege **o cheie pe grup și
  meci**, cea eligibilă cu p minim. Cheile de pauză se decontează numai din football-data.
- Costul ponderii 0.9 față de 1.0 pe cote curate (tuning, 1X2 log-loss): 0.9937 \| 0.9954 față
  de 0.9934 \| 0.9950. Este asigurarea recomandată pentru cotele FlashScore, mai zgomotoase.

## Validare pe tuning (2223 | 2324) — model și regulă finale

Regula de goluri/pauză e în eșantion pe aceste sezoane (a fost derivată pe ele); lista pentru
numărători e cea înghețată de corners_cards tot pe ele.

| Întrebare | fără cote | cote avg (pre-închidere) |
|---|---|---|
| 1X2 log-loss / acuratețe | 1.0056 / 50.3% \| 1.0090 / 49.7% | 0.9937 / 51.4% \| 0.9954 / 51.0% |
| pauză 1X2 | 1.0415 \| 1.0487 | 1.0404 \| 1.0480 |
| scor exact (log-loss / top-1) | 2.7825 / 12.9% \| 2.8364 / 12.9% | 2.7665 / 13.4% \| 2.8181 / 13.1% |

Regula modelului pe grup, fără cote (80% | strict 85%), 2223 ; 2324:

| Grup | regula 80% | regula 85% |
|---|---|---|
| 1X2 | 83.3/1.1/1.20 (-2.7% @81) ; 85.2/1.4/1.19 (-3.6% @108) | 87.0/0.3 ; 97.0/0.4 (33 selecții) |
| șansă dublă | 85.4/25.5/1.20 (-2.7% @1980) ; 83.7/25.6/1.20 (-4.8% @1995) | 90.2/12.2/1.14 (-2.4%) ; 88.5/12.4/1.14 (-4.2%) |
| goluri | 85.6/98.4/1.17 ; 85.1/98.4/1.17 | 88.6/69.3/1.13 ; 87.5/64.3/1.14 |
| goluri pe echipă | 85.4/96.7/1.18 ; 85.2/96.4/1.19 | 89.5/72.8 ; 88.7/73.5 |
| egal = anulat | 88.3/12.3/1.17 ; 87.3/12.5/1.17 | 90.1/6.2 ; 89.0/6.1 |
| handicap asiatic | 83.9/98.3/1.20 ; 84.1/98.4/1.20 | 88.7/90.6/1.15 ; 88.0/90.2/1.15 |
| pauză | 87.9/91.8/1.13 ; 87.3/93.1/1.14 | 88.8/78.4 ; 88.4/74.3 |
| cornere | 82.1/62.4/1.21 ; 81.7/61.9/1.21 | 87.4/20.1 ; 86.6/17.4 |
| cornere pe echipă | 82.6/93.8/1.21 ; 82.2/93.7/1.21 | 87.8/71.1 ; 86.3/74.4 |
| handicap cornere | 83.2/94.0/1.20 ; 82.2/93.7/1.20 | 87.2/74.0 ; 86.6/73.9 |
| cartonașe | 85.2/94.1/1.19 ; 85.5/94.0/1.19 | 89.6/83.7 ; 89.9/82.3 |
| cartonașe pe echipă | 84.5/91.4/1.20 ; 85.0/92.2/1.19 | 90.0/87.3 ; 89.8/89.8 |
| puncte cartonașe | 84.7/93.8/1.19 ; 85.2/93.9/1.19 | 89.0/88.1 ; 88.9/87.8 |
| șuturi pe poartă | 83.6/93.2/1.19 ; 84.6/93.2/1.19 | 86.9/52.2 ; 89.4/51.5 |
| șuturi pe echipă | 82.8/94.1/1.21 ; 83.0/94.0/1.21 | 87.6/93.3 ; 87.1/92.9 |
| toate (≈11.1 selecții/meci; 8.7 strict) | 84.5/98.5/1.19 (-2.7% @2069) ; 84.4/98.5/1.19 (-4.7% @2107) | 88.6 (-2.4%) ; 88.3 (-3.9%) |

GG/NG și scorul exact nu intră niciodată în listă (nu ating 80% nicăieri). Cu cote avg imaginea e
aceeași (toate grupurile 81.7–88.4% cu regula de 80%); 1X2 urcă la 2.5% acoperire cu 85.3% ;
86.6% (ROI -1.6% @187 ; -0.2% @194) și șansa dublă are -3.3% @2104 ; -4.5% @2133.
Pe ligă (fără cote, cumulat): 82.5% (SC1) … 85.7% (I1); strict 87.3% (SC3) … 89.5% (EC).

## Confirmarea 2425 (o singură rulare pe variantă, după înghețare)

Fișierele model/regulă au fost înghețate înainte (sha256 în
`footypreds/data/fotbal/bench/final/frozen-before-2425.sha256`; după confirmare s-au schimbat
doar terminațiile de linie CRLF → LF, fără efect asupra predicțiilor). 7681 de meciuri.

| Întrebare | fără cote | cote avg |
|---|---|---|
| 1X2 log-loss / acuratețe | 1.0148 / 49.5% | 1.0006 / 50.6% |
| pauză 1X2 | 1.0484 | 1.0472 |
| scor exact | 2.8038 | 2.7836 |

| Grup | fără cote: 80% | fără cote: 85% | cote avg: 80% | cote avg: 85% |
|---|---|---|---|---|
| 1X2 | 88.2/1.1/1.19 (-0.2% @85) | 88.9/0.4 | 88.4/2.4/1.18 (+1.9% @181) | 89.9/1.0 (-0.1% @79) |
| șansă dublă | 83.7/24.9/1.20 (-5.1% @1911) | 90.0/12.1/1.13 (-2.9% @928) | 84.0/28.6/1.20 (-4.2% @2195) | 88.5/13.9/1.14 (-4.1% @1067) |
| goluri | 86.5/98.5/1.17 | 89.0/64.7/1.14 | 86.1/98.3/1.17 | 88.6/67.4 |
| goluri pe echipă | 85.1/96.7/1.19 | 89.7/73.4 | 85.1/96.3/1.19 | 89.8/74.9 |
| egal = anulat | 86.3/12.1/1.17 | 89.9/6.1 | 87.0/13.3/1.17 | 91.4/7.2 |
| handicap asiatic | 84.7/98.3/1.20 | 88.7/90.1/1.15 | 84.8/98.2/1.20 | 89.0/90.7 |
| pauză | 87.8/95.0/1.14 | 89.0/76.1 | 88.0/93.3/1.14 | 89.1/75.0 |
| cornere | 82.8/66.3/1.21 | 87.1/21.4/1.15 | idem | idem |
| cornere pe echipă | 82.2/93.5/1.21 | 86.8/72.2 | idem | idem |
| handicap cornere | 82.7/93.6/1.20 | 86.9/74.4 | idem | idem |
| cartonașe | 85.6/93.9/1.19 | 89.4/81.2 | idem | idem |
| cartonașe pe echipă | 83.8/92.5/1.19 | 88.6/90.6 | idem | idem |
| puncte cartonașe | 85.1/93.8/1.19 | 88.9/86.7 | idem | idem |
| șuturi pe poartă | 82.9/93.0/1.19 | 86.1/51.8 | idem | idem |
| șuturi pe echipă | 82.8/93.9/1.21 | 87.3/93.0 | idem | idem |
| toate (11.2 selecții/meci) | 84.4/98.5/1.19 (-4.9% @1997) | 88.3 (-2.8% @956) | 84.5/98.5/1.19 (-3.8% @2376) | 88.3 (-3.8% @1146) |

- **Ținta pe grup a ținut în afara eșantionului**: fiecare grup ≥ 82.2% cu regula de 80% și
  ≥ 86.1% cu regula strictă, în ambele variante; pe ligă 81.8% (B1) … 85.9% (EC), strict
  86.7% (B1) … 89.5% (EC).
- **Pe cheie, nu**: cu regula de 80%, 12 din 96 de chei cu ≥ 50 de selecții sunt sub 80% (fără
  cote): away_under_1.5 78.6% (599), ah_1_-0.25 77.5%, ah_1_+0.5 79.0%, ah_2_+2.5 77.8%,
  home_corners_under_5.5 78.3%, away_corners_under_4.5 76.9%, corners_ah_1_-0.5 77.1%,
  away_cards_under_2.5 77.0%, sot_over_7.5 76.7%, sot_under_9.5 80.0%, home_sot_over_4.5 79.2%,
  away_sot_under_3.5 72.2% (79). Cu regula strictă, 9 din 87 sub 85% (cel mai jos ah_2_+0.5 79.2%
  pe 53 și home_corners_under_6.5 81.6% pe 250).
- **Bani**: fiecare ROI pe ≥ 100 de pariuri cu preț real este negativ (șansă dublă -4.2% …
  -5.1%, toate -3.8% … -4.9%). Singurul plus, 1X2 cu cote +1.9% pe 181 de pariuri, e zgomot
  (interval larg, și pe tuning a fost -1.6% / -0.2%).

## Testul blocat 2526 (rularea oficială, 29.09.2026, o singură dată pe variantă, după înghețare)

Rulat la final, după ce modelul, regula, aplicația, testele și documentația au fost terminate;
sha256 al fișierelor înghețate în `footypreds/data/fotbal/bench/final/frozen-before-2526.sha256`.
Nimic nu s-a schimbat după aceste rulări (doar această secțiune și rezumatul din README).
Comenzi:

```powershell
python -m fotbalPrediction.benchmark --model fotbalPrediction.model:benchmark_factory --seasons 2526 --locked-test --json footypreds/data/fotbal/bench/final/locked2526-none.json
python -m fotbalPrediction.benchmark --model fotbalPrediction.model:benchmark_factory --seasons 2526 --locked-test --odds avg --json footypreds/data/fotbal/bench/final/locked2526-avg.json
```

Fără cote (ieșirea benchmark-ului, verbatim; `select` = regula de 80%, `select_high` = 85%):

```text
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!!! ATENȚIE: rulezi TESTUL BLOCAT (2526). Rezultatul NU se folosește pentru tuning.
!!! Rulează-l o singură dată pe versiune de model.
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
ÎNTREBĂRI (multi-clasă)
sezon piață         n  logloss   brier    acc    ece
2526  1x2        7646   1.0169  0.6095   49.1  0.015
2526  ht_1x2     7646   1.0496  0.6358   44.1  0.009
2526  cs         7646   2.8131  0.9263   13.0  0.005

GRUPURI: cea mai probabilă piață pe meci (acc%/cov%/cota corectă medie[/ROI% la cote reale])
sezon grup          meciuri                  best>=.80                  best>=.85     cov@80 (prag)     cov@85 (prag)                 transfer80                     select                select_high
2526  1x2              7646      87.9/0.9/1.20/+1.3@66     78.3/0.3/1.15/-12.8@23       4.7 (0.701)       1.1 (0.788)                          -      89.7/0.8/1.19/+2.9@58      81.8/0.3/1.15/-8.8@22
2526  dc               7646   86.8/24.7/1.17/-4.5@1883    90.6/11.4/1.12/-4.2@868      61.8 (0.740)      31.9 (0.780)                          -   84.4/24.1/1.20/-5.4@1843    90.0/10.8/1.14/-4.1@825
2526  goals            7646            93.1/100.0/1.08            93.1/100.0/1.08     100.0 (0.903)     100.0 (0.903)                          -             87.0/98.5/1.17             88.9/66.3/1.14
2526  team_goals       7646             90.0/99.8/1.11             90.9/89.7/1.10     100.0 (0.794)     100.0 (0.794)                          -             84.8/96.5/1.19             88.5/73.2/1.13
2526  btts             7646                    -/0.0/-                    -/0.0/-           0.0 (-)           0.0 (-)                          -                    -/0.0/-                    -/0.0/-
2526  dnb              7646             88.5/12.3/1.17              93.5/6.0/1.12      25.9 (0.697)      16.6 (0.751)                          -             88.3/11.2/1.18              93.7/5.0/1.13
2526  ah               7646            96.9/100.0/1.03            96.9/100.0/1.03     100.0 (0.902)     100.0 (0.902)                          -      84.5/98.4/1.20/+0.0@3    88.5/89.9/1.15/-100.0@2
2526  cs               7646                    -/0.0/-                    -/0.0/-           0.0 (-)           0.0 (-)                          -                    -/0.0/-                    -/0.0/-
2526  ht               7646             88.4/99.2/1.14             89.2/82.4/1.13     100.0 (0.793)     100.0 (0.793)                          -             88.5/96.6/1.14             89.2/80.0/1.13
2526  corners          7094             82.8/69.8/1.19             85.9/22.6/1.15     100.0 (0.760)      41.8 (0.826)                          -             81.2/65.9/1.21             85.1/21.7/1.15
2526  team_corners     7094             88.0/99.5/1.14             89.1/75.0/1.12     100.0 (0.790)     100.0 (0.790)                          -             81.1/93.5/1.21             85.8/70.8/1.14
2526  corners_ah       7094             88.8/99.9/1.12             90.2/79.5/1.10     100.0 (0.795)     100.0 (0.795)                          -             82.4/93.9/1.20             86.7/74.9/1.14
2526  cards            7646            93.3/100.0/1.08            93.3/100.0/1.08     100.0 (0.871)     100.0 (0.871)                          -             85.8/94.0/1.19             89.9/83.4/1.13
2526  team_cards       7646            92.6/100.0/1.09            92.6/100.0/1.09     100.0 (0.862)     100.0 (0.862)                          -             86.0/92.2/1.19             89.6/90.8/1.13
2526  bookings         7646            94.4/100.0/1.07            94.4/100.0/1.07     100.0 (0.893)     100.0 (0.893)                          -             85.0/93.9/1.19             89.3/88.2/1.14
2526  sot              7094             85.2/98.3/1.17             88.6/48.9/1.13     100.0 (0.782)     100.0 (0.782)                          -             83.0/92.4/1.20             87.7/46.4/1.14
2526  team_sot         7094            94.5/100.0/1.06            94.5/100.0/1.06     100.0 (0.884)     100.0 (0.884)                          -             81.3/94.0/1.21             86.2/93.2/1.14
2526  all              7646            97.1/100.0/1.03            97.1/100.0/1.03     100.0 (0.936)     100.0 (0.936)                          -   84.4/98.5/1.19/-5.1@1904    88.3/98.5/1.14/-4.4@849
[... tabelul PIEȚE, în fișierul complet ...]
meciuri evaluate: 7646, predicții pe piețe: 1750442, rânduri trimise: 162216; încărcare 1.74s, rulare 82.3s
```

Varianta de producție cu cote (media PRE-închidere avg, marjă „power”, pondere piață 0.9):

```text
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!!! ATENȚIE: rulezi TESTUL BLOCAT (2526). Rezultatul NU se folosește pentru tuning.
!!! Rulează-l o singură dată pe versiune de model.
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
ÎNTREBĂRI (multi-clasă)
sezon piață         n  logloss   brier    acc    ece
2526  1x2        7646   1.0025  0.5999   50.4  0.007
2526  ht_1x2     7646   1.0487  0.6351   44.4  0.010
2526  cs         7646   2.7948  0.9244   13.6  0.008

GRUPURI: cea mai probabilă piață pe meci (acc%/cov%/cota corectă medie[/ROI% la cote reale])
sezon grup          meciuri                  best>=.80                  best>=.85     cov@80 (prag)     cov@85 (prag)                 transfer80                     select                select_high
2526  1x2              7646     85.2/2.1/1.19/-1.6@162      85.7/0.7/1.14/-4.7@56       6.9 (0.708)       3.7 (0.761)                          -     87.4/1.9/1.19/+0.9@143      87.8/0.6/1.14/-2.4@49
2526  dc               7646   88.0/27.4/1.17/-3.8@2088   91.1/14.0/1.12/-4.1@1064      73.1 (0.729)      41.5 (0.766)                          -   84.3/26.8/1.20/-4.9@2045   88.8/13.5/1.14/-4.8@1031
2526  goals            7646            93.6/100.0/1.07            93.6/100.0/1.07     100.0 (0.892)     100.0 (0.892)                          -             87.1/98.4/1.17             88.7/65.2/1.14
2526  team_goals       7646             90.6/99.8/1.11             91.2/90.6/1.10     100.0 (0.794)     100.0 (0.794)                          -             85.1/96.5/1.19             88.9/75.3/1.13
2526  btts             7646                    -/0.0/-                    -/0.0/-           0.0 (-)           0.0 (-)                          -                    -/0.0/-                    -/0.0/-
2526  dnb              7646             88.8/15.3/1.16              93.6/8.6/1.11      32.4 (0.677)      21.3 (0.737)                          -             87.7/13.4/1.17              92.5/6.7/1.13
2526  ah               7646            97.3/100.0/1.03            97.3/100.0/1.03     100.0 (0.912)     100.0 (0.912)                          -             84.7/98.4/1.20             88.6/90.9/1.15
2526  cs               7646                    -/0.0/-                    -/0.0/-           0.0 (-)           0.0 (-)                          -                    -/0.0/-                    -/0.0/-
2526  ht               7646             88.5/98.3/1.14             89.3/79.8/1.13     100.0 (0.793)     100.0 (0.793)                          -             88.4/95.1/1.14             89.2/76.6/1.13
2526  corners          7094             82.8/69.8/1.19             85.9/22.6/1.15     100.0 (0.760)      41.8 (0.826)                          -             81.2/65.9/1.21             85.1/21.7/1.15
2526  team_corners     7094             88.0/99.5/1.14             89.1/75.0/1.12     100.0 (0.790)     100.0 (0.790)                          -             81.1/93.5/1.21             85.8/70.8/1.14
2526  corners_ah       7094             88.8/99.9/1.12             90.2/79.5/1.10     100.0 (0.795)     100.0 (0.795)                          -             82.4/93.9/1.20             86.7/74.9/1.14
2526  cards            7646            93.3/100.0/1.08            93.3/100.0/1.08     100.0 (0.871)     100.0 (0.871)                          -             85.8/94.0/1.19             89.9/83.4/1.13
2526  team_cards       7646            92.6/100.0/1.09            92.6/100.0/1.09     100.0 (0.862)     100.0 (0.862)                          -             86.0/92.2/1.19             89.6/90.8/1.13
2526  bookings         7646            94.4/100.0/1.07            94.4/100.0/1.07     100.0 (0.893)     100.0 (0.893)                          -             85.0/93.9/1.19             89.3/88.2/1.14
2526  sot              7094             85.2/98.3/1.17             88.6/48.9/1.13     100.0 (0.782)     100.0 (0.782)                          -             83.0/92.4/1.20             87.7/46.4/1.14
2526  team_sot         7094            94.5/100.0/1.06            94.5/100.0/1.06     100.0 (0.884)     100.0 (0.884)                          -             81.3/94.0/1.21             86.2/93.2/1.14
2526  all              7646            97.4/100.0/1.03            97.4/100.0/1.03     100.0 (0.937)     100.0 (0.937)                          -   84.5/98.5/1.19/-4.5@2188   88.3/98.5/1.14/-4.7@1080
[... tabelul PIEȚE, în fișierul complet ...]
meciuri evaluate: 7646, predicții pe piețe: 1750442, rânduri trimise: 162216; încărcare 1.74s, rulare 99.7s
cote în ctx: avg (pre-închidere)
```

Citire:

- 1X2: log-loss 1.0169 / acuratețe 49.1% fără cote și 1.0025 / 50.4% cu cote (pe 2425: 1.0148 și
  1.0006). Nicio îmbunătățire față de piață.
- Regula de 80%, pe grup: toate grupurile între **81.1%** (cornere pe echipă) și **89.7%** (1X2,
  dar numai 58 de selecții); toate selecțiile 84.4% / 84.5% la ~11.2 selecții pe meci și cotă
  corectă medie 1.19. Pe ligă: 83.4% (D2) … 85.6% (G1) fără cote, 83.5% … 86.1% cu cote.
- Regula strictă de 85%, pe grup: toate grupurile ≥ 85.1% (cornere), **cu o excepție: 1X2 fără
  cote 81.8% pe doar 22 de selecții** (cu cote 87.8% pe 49); toate selecțiile 88.3%. Pe ligă:
  86.9% (B1) … 89.9% (I1).
- Pe cheie (≥ 50 de selecții): 14 din 96 de chei sunt sub 80% cu regula de 80% (cele mai slabe:
  corners_under_11.5 77.7% pe 806, sot_under_9.5 77.6% pe 406, away_corners_under_4.5 77.6% pe
  152), 12 din 77 sunt sub 85% cu regula strictă (corners_under_11.5 73.8% pe 61). Ținta ține pe
  grup, nu garantat pe fiecare linie.
- **Bani**: toate selecțiile cu preț real -5.1% (1904 pariuri, fără cote) și -4.5% (2188, cu
  cote); șansă dublă -5.4% / -4.9%. Plusurile 1X2 (+2.9% pe 58, +0.9% pe 143) sunt zgomot. Nu
  există nicio dovadă de profit; cornerele, cartonașele, pauza și șuturile nu au prețuri istorice.

## Arbitrul la cartonașe: benchmark față de producție (29.09.2026, numai tuning 2223 | 2324)

Benchmark-ul rulează implicit cu arbitrul din CSV (Anglia/Scoția), producția fără el (FlashScore
nu îl dă). Măsurat pe selecții, doar pe ligile E0, E1, E2, E3, EC, SC0-SC3 (singurele cu
arbitru), 2223+2324, 6712 meciuri, `--markets cards,team_cards,bookings`, cu și fără
`--no-referee` (fișierele în `footypreds/data/fotbal/bench/referee/`). 2425 și 2526 NU au fost
rerulate. Format: acuratețe% / acoperire% / cota corectă medie.

| grup | regula 80%, cu arbitru | regula 80%, fără arbitru | strict 85%, cu arbitru | strict 85%, fără arbitru |
|---|---|---|---|---|
| cards | 85.7 / 93.8 / 1.19 | 85.7 / 93.8 / 1.19 | 89.6 / 87.8 / 1.13 | 89.3 / 90.7 / 1.13 |
| team_cards | 84.2 / 93.6 / 1.21 | 83.8 / 93.8 / 1.21 | 89.9 / 92.6 / 1.14 | 88.4 / 93.0 / 1.14 |
| bookings | 85.9 / 93.7 / 1.19 | 85.7 / 93.7 / 1.19 | 89.7 / 91.4 / 1.14 | 89.1 / 92.6 / 1.14 |

Fără arbitru, precizia pe selecțiile de cartonașe din Anglia/Scoția scade cu 0-0.4 puncte la
regula de 80% și cu 0.3-1.5 puncte la regula strictă (cel mai mult la cartonașe pe echipă). Rămâne
peste țintă pe tuning, dar cifrele 2425/2526 raportate mai sus pentru cartonașe (calculate cu
arbitru) sunt ușor optimiste pentru producție în aceste ligi. Celelalte ligi nu au arbitru în CSV,
deci nu sunt afectate.

## Observații despre harness (fișierele harness nu au fost modificate)

- T1 2223 conține 29 de meciuri acordate 3-0 la masa verde (Gaziantep, Hatayspor după cutremur)
  fără cote pre-închidere; sunt tratate ca meciuri reale (auditul odds_blend: coboară log-loss-ul
  1X2 din 2223 cu ~0.001 pentru toate variantele). Ar trebui filtrate în `data.py`.
- Pentru liniile asiatice sfert, p = E[câștig]/(E[câștig]+E[pierdere]) nu este P(y=1) (jumătatea
  câștigată contează ca y=1), deci log-loss/ECE pe liniile sfert sunt ușor deplasate.
- Liniile asiatice listate în afara ±2.5 nu sunt în catalog (fără efect: nu se prezic).
- `leagues[cod].select` folosește cheia `picks`, iar metricile pe cheie `n_selected`.
