# Experimente tenisPrediction (v1 → v2 → v3)

Jurnalul candidaților evaluați cu benchmark-ul comun `tenisPrediction/benchmark.py`, al
auditurilor lor și al deciziilor pentru modelul de producție v2.

## Protocol

- Date: fișierele TML (format Sackmann) din `tml-data/`, 1990–2024, toate cele patru circuite
  (ATP, Challenger, calificări ATP, WTA) trimise la `update()`. Nimic nu se antrenează în avans:
  fiecare meci evaluat este prezis înainte ca rezultatul lui să intre în stare.
- Validare: 2023 și 2024, ATP și WTA (Challenger opțional). **2025 este testul blocat**: rulat
  la final, după înghețarea modelului și a pragurilor. **Atenție: 2025 nu este curat** — vezi
  secțiunea „Expunerea anului 2025” de mai jos.
- Metrica principală: `coverage_at_80` — cea mai mare parte a meciurilor care pot fi selectate
  după încrederea modelului păstrând precizia ≥ 80%. Atenție: pragul lui este ales după fapt, pe
  același an, deci este un plafon optimist. Cifrele cinstite sunt `select` (regula proprie a
  modelului, aplicată în timp real) și `transfer_80` (pragul anului precedent aplicat anului
  curent).
- Secundare: log-loss, Brier, acuratețe, ECE.
- O precizie de peste 80% pe **toate** meciurile nu este posibilă: nici cotele de închidere ale
  pieței (media tennis-data.co.uk) nu trec de ~68–70%. Ținta realistă este o selecție de ~30–40% din meciuri cu
  ~80–82% precizie.

Coloane în tabele: log-loss / acuratețe / cov@80 (prag) / `select` precizie/acoperire /
`transfer_80` precizie/acoperire.

## Referința v1 (`candidates/baseline.py`)

Elo general + Elo pe suprafață + rang (k=28), selecție `încredere ≥ 0.72` și ≥ 5 meciuri istorice.

| grup | log-loss | acc | cov@80 | select | transfer_80 |
|---|---|---|---|---|---|
| atp/2023 | 0.6149 | 0.6563 | 0.3264 (0.750) | 0.774/0.391 | – |
| atp/2024 | 0.6092 | 0.6574 | 0.3583 (0.725) | 0.793/0.361 | 0.816/0.310 |
| wta/2023 | 0.6132 | 0.6607 | 0.2780 (0.735) | 0.778/0.278 | – |
| wta/2024 | 0.6162 | 0.6446 | 0.3024 (0.732) | 0.786/0.308 | 0.805/0.292 |

Regula de selecție v1 **nu** ține 80% în afara eșantionului (0.774–0.793).

## Candidați (toți reproduși exact de integrator, aceeași comandă, 1990–2024)

Toate cele șase audituri adversariale au concluzionat: **fără leakage** (citire linie cu linie,
test de simetrie a orientării, rulări cu `--salt` diferit cu metrici identice în afară de ECE;
pentru `serve_return` și test de „otrăvire” a viitorului: 0.0 diferență în predicțiile de
dinainte de prag).

### elo_plus — Elo 538 extins + stivă logistică online

Elo cu K descrescător 250/(n+5)^0.4, Elo pe suprafață, MOV, decădere la inactivitate, prior din
rang pentru jucători noi, rating pe game-uri, serviciu/retur, Elo rapid; stivă ridge fără
intercept reantrenată lunar; prag de selecție adaptiv (țintă 0.81).

| grup | log-loss | acc | cov@80 | select | transfer_80 |
|---|---|---|---|---|---|
| atp/2023 | 0.5977 | 0.6657 | 0.4235 (0.689) | 0.801/0.383 | – |
| atp/2024 | 0.5977 | 0.6639 | 0.4012 (0.696) | 0.799/0.384 | 0.796/0.422 |
| wta/2023 | 0.5949 | 0.6775 | 0.3676 (0.700) | 0.801/0.284 | – |
| wta/2024 | 0.6005 | 0.6620 | 0.3736 (0.689) | 0.805/0.263 | 0.801/0.341 |

Audit: fără leakage, risc de overfit scăzut. Ținta 0.81 nu a fost suficientă pentru ATP 2024
(0.799). Integrat: ideea pragului adaptiv din predicții în afara eșantionului.

### glicko — rating bayesian Glicko/Kalman + stivă + selecție penalizată cu RD

| grup | log-loss | acc | cov@80 | select | transfer_80 |
|---|---|---|---|---|---|
| atp/2023 | 0.5962 | 0.6689 | 0.4094 (0.689) | 0.802/0.406 | – |
| atp/2024 | 0.5916 | 0.6695 | 0.4450 (0.671) | 0.811/0.402 | 0.808/0.408 |
| wta/2023 | 0.5939 | 0.6783 | 0.3838 (0.694) | 0.801/0.388 | – |
| wta/2024 | 0.5982 | 0.6711 | 0.4020 (0.682) | 0.807/0.377 | 0.809/0.372 |

Audit: fără leakage, risc de overfit **mediu** (~36 de trăsături alese pe 2023, marje de doar
0.1–1.1 puncte peste 80%). Concluzie utilă: incertitudinea ratingului ajută în interiorul lui p,
nu ca filtru separat (poarta dură pe RD pierde multă acoperire). Neintegrat ca model (vezi
ansamblul de mai jos).

### serve_return — ratinguri serviciu/retur + lanț Markov + Elo + stivă lunară

| grup | log-loss | acc | cov@80 | select | transfer_80 |
|---|---|---|---|---|---|
| atp/2023 | 0.5981 | 0.6652 | 0.4188 (0.688) | 0.806/0.387 | – |
| atp/2024 | 0.5931 | 0.6666 | 0.4257 (0.679) | 0.816/0.375 | 0.808/0.404 |
| wta/2023 | 0.5972 | 0.6729 | 0.3684 (0.689) | 0.805/0.343 | – |
| wta/2024 | 0.5996 | 0.6696 | 0.3645 (0.696) | 0.804/0.351 | 0.792/0.381 |

Audit: fără leakage (test de otrăvire: 0.0), risc scăzut. Componenta Markov serviciu/retur este
semnalul nou cel mai valoros (−0.006 log-loss la ablație). Aceeași idee există deja în
`stack_ml`, deci nu a fost integrat separat.

### stack_ml — motor de trăsături + regresie logistică simetrică pe sezon ✅ integrat

Elo 538 general și pe suprafață, Elo rapid, Elo pe game-uri și pe puncte, serviciu/retur ajustat
după adversar cu lanț Markov exact (antisimetric), rang, puncte, vârstă, înălțime, intrare
(Q/WC/LL/PR), formă, volatilitate, oboseală, H2H; 46 de diferențe antisimetrice + interacțiuni cu
circuitul, experiența și volatilitatea; LR fără intercept, reantrenată la început de sezon.

| grup | log-loss | acc | cov@80 | select (0.72) | transfer_80 |
|---|---|---|---|---|---|
| atp/2023 | 0.5966 | 0.6635 | 0.3798 (0.720) | 0.801/0.379 | – |
| atp/2024 | 0.5881 | 0.6734 | 0.4535 (0.684) | 0.832/0.365 | 0.832/0.365 |
| wta/2023 | 0.5918 | 0.6772 | 0.3856 (0.705) | 0.807/0.349 | – |
| wta/2024 | 0.5940 | 0.6620 | 0.4290 (0.676) | 0.817/0.320 | 0.815/0.350 |

Audit: fără leakage, risc scăzut; câștigul se păstrează pe 2021/2022 (ani netunați). Cel mai bun
log-loss mediu pe ATP+WTA. Varianta HGB (`mode="stack"`) nu a adus nimic → eliminată.

### calibrated_selector — stivă în două etape + selector cu fereastră rulantă ✅ idee integrată

| grup | log-loss | acc | cov@80 | select | transfer_80 |
|---|---|---|---|---|---|
| atp/2023 | 0.6046 | 0.6585 | 0.3536 (0.728) | 0.805/0.333 | – |
| atp/2024 | 0.5940 | 0.6662 | 0.3933 (0.697) | 0.810/0.345 | 0.825/0.328 |
| wta/2023 | 0.5982 | 0.6808 | 0.3562 (0.692) | 0.823/0.282 | – |
| wta/2024 | 0.6027 | 0.6624 | 0.3604 (0.689) | 0.807/0.275 | 0.803/0.349 |

Audit: fără leakage, risc scăzut. Contribuția principală: regula de selecție în timp real — pragul
cel mai mic la care ultimele N predicții în afara eșantionului (cu rezultat cunoscut) au precizia
≥ 0.82; ținta 0.82 (nu 0.81) a ținut ≥ 80% în toate sezoanele 2016–2023 pe ATP și WTA.
Challenger a căzut sub 80% în 3 din 8 sezoane la 0.82. Integrată în v2 (cu 0.83 pentru
Challenger).

### market_context — Elo 538 + rating pe puncte + stivă pe perioade + analiză de piață

| grup | log-loss | acc | cov@80 | select (0.715) | transfer_80 |
|---|---|---|---|---|---|
| atp/2023 | 0.6005 | 0.6605 | 0.3755 (0.711) | 0.802/0.365 | – |
| atp/2024 | 0.5918 | 0.6708 | 0.4241 (0.695) | 0.822/0.375 | 0.824/0.384 |
| wta/2023 | 0.5972 | 0.6671 | 0.3931 (0.697) | 0.805/0.350 | – |
| wta/2024 | 0.6004 | 0.6734 | 0.3789 (0.702) | 0.811/0.347 | 0.794/0.393 |

Audit: fără leakage, risc scăzut. Regula fixă 0.715 dă 0.796 pe WTA 2022 (sub 80%). Contribuția
cheie: **analiza de piață** (cote tennis-data.co.uk 2023/2024, potrivire 98–99%). Cota de
închidere a pieței fără marjă (**media AvgW/AvgL** publicată de tennis-data.co.uk — nu Pinnacle,
cum s-a scris inițial; loader-ul folosește PSW/PSL doar ca rezervă când media lipsește) rămâne
mai bună decât orice model TML (ATP LL 0.5875/0.5838,
cov@80 0.450/0.485), iar ponderea optimă a modelului într-un amestec logit, aleasă pe 2023, este
0.00. Integrat: ponderea piață/model din aplicație.

## Ce s-a integrat în v2 și de ce

1. **Ansamblu sau model unic?** Media pe scara logit a candidaților, pe aceleași meciuri
   (2023/2024, ATP+WTA), câștigă cel mult ~0.0005 log-loss mediu față de `stack_ml` singur
   (`stack_ml+glicko`: 0.5947/0.5880/0.5910/0.5946 față de 0.5966/0.5881/0.5918/0.5940), iar
   cov@80 se mișcă ±0.03 (zgomot). S-a ales cea mai simplă variantă: **motorul și stiva
   `stack_ml`** (mutate în `tenisPrediction/engine.py`).
2. **Potrivirea stivei**: solver Newton propriu (numpy, fără scikit-learn), mai exact decât
   lbfgs-ul inițial (obiectiv mai mic; lbfgs se oprea înainte de convergență), refit la fiecare
   început de sezon din 2014, ca modelul să aibă predicții în afara eșantionului pentru selecție.
   Pornirea caldă este protejată (se păstrează punctul de start mai bun dintre zero și
   coeficienții anteriori).
3. **Regula de selecție** (ideea `calibrated_selector` / `elo_plus`): pentru fiecare circuit,
   fereastra ultimelor 1000 de predicții în afara eșantionului cu rezultat cunoscut; la fiecare
   100 de rezultate noi pragul devine cea mai mică încredere la care felia de sus are precizia ≥
   0.82 (0.83 la Challenger/calificări), minimum 100 de meciuri, niciodată sub 0.60. Aleasă pe
   2023/2024 (vezi mai jos).
4. **Ponderea pieței** în aplicație: 0.00 (validată pe 2023, confirmată pe 2024, și pentru v2).

### Alegerea regulii de selecție (numai 2023/2024)

Criteriu fixat înainte de comparație: acoperirea medie maximă pe ATP+WTA, cu condiția ca pentru
fiecare grup precizia − 0.84·SE ≥ 0.80 (margine statistică de ~1 punct).

| regulă | acoperire medie | cea mai slabă limită | atp23 | atp24 | wta23 | wta24 |
|---|---|---|---|---|---|---|
| fix 0.70 | 0.399 | 0.785 ✗ | .794/.423 | .812/.413 | .797/.397 | .811/.361 |
| fix 0.72 | 0.353 | 0.792 ✗ | .802/.378 | .831/.365 | .808/.350 | .816/.319 |
| fix 0.73 | 0.331 | 0.804 ✓ | .814/.353 | .838/.347 | .815/.327 | .825/.296 |
| rulant 0.81 / 2000 | 0.368 | 0.792 ✗ | .802/.414 | .818/.405 | .815/.329 | .817/.324 |
| **rulant 0.82 / 1000** | **0.346** | **0.805 ✓** | .815/.371 | .820/.396 | .816/.302 | .825/.313 |
| rulant 0.82 / 2000 | 0.336 | 0.804 ✓ | .814/.370 | .821/.381 | .824/.301 | .831/.291 |
| rulant 0.83 / 2000 | 0.304 | 0.819 ✓ | .829/.318 | .829/.358 | .837/.274 | .841/.267 |

### Validarea finală v2 (model și praguri înghețate)

Comandă:
`python -m tenisPrediction.benchmark --model tenisPrediction.model:benchmark_factory --years 2023,2024 --tours atp,wta,challenger`
(~75 s).

| grup | n | log-loss | Brier | acc | ECE | cov@80 (prag) | select | transfer_80 |
|---|---|---|---|---|---|---|---|---|
| atp/2023 | 2975 | 0.5965 | 0.2068 | 0.6629 | 0.0270 | 0.3899 (0.715) | 0.815/0.371 | – |
| atp/2024 | 3056 | 0.5880 | 0.2021 | 0.6728 | 0.0184 | 0.4535 (0.684) | 0.820/0.396 | 0.828/0.375 |
| wta/2023 | 2788 | 0.5917 | 0.2040 | 0.6790 | 0.0114 | 0.3859 (0.704) | 0.816/0.302 | – |
| wta/2024 | 2639 | 0.5942 | 0.2058 | 0.6620 | 0.0202 | 0.4233 (0.679) | 0.825/0.313 | 0.814/0.351 |
| challenger/2023 | 5673 | 0.6302 | 0.2202 | 0.6434 | 0.0093 | 0.2098 (0.743) | 0.812/0.116 | – |
| challenger/2024 | 6031 | 0.6351 | 0.2225 | 0.6377 | 0.0196 | 0.1845 (0.747) | 0.803/0.109 | 0.797/0.195 |

- `--salt x`: log-loss, acuratețe, cov@80, `select` identice (doar ECE se schimbă) → modelul
  este exact simetric, orientarea nu poate fi învățată.
- Verificare de stabilitate pe ani netunați (2021/2022, ATP+WTA, după înghețare):
  `select` ATP 0.809/0.333 și 0.814/0.417, WTA 0.822/0.266 și **0.800**/0.366 (exact la
  limită); log-loss ATP 0.5945/0.5761, WTA 0.5953/0.5991. Referința v1 pe aceiași ani: `select`
  0.780, 0.794, 0.786, 0.749.
- Piață (tennis-data.co.uk, subsetul potrivit): ponderea v2 aleasă pe 2023 = 0.00 pe ATP și WTA;
  stiva piață+model dă modelului coeficient −0.08 (ATP) și +0.03 (WTA). Pe 2024, ATP: piață
  0.5833, v2 0.5863, v1 0.6092 log-loss.

### Expunerea anului 2025 (de citit înainte de cifrele testului blocat)

- Experimentul inițial al inginerului principal (`elo_exp.py` din scratchpad, 29.09.2026 13:00,
  înaintea oricărui candidat) a încărcat datele până în 2025 și a afișat log-loss/acuratețe
  `test_2025` pentru toate cele 6 configurații Elo, plus o stivă logistică potrivită pe 2024 și
  scorată pe 2025, cu acoperire și precizie la pragurile 0.65/0.72/0.80. Setările motorului
  ajunse în producție coincid cu configurațiile din acel script (K = 250/(n+5)^0.4, pondere
  suprafață 0.5, K×1.1 la Grand Slam, decădere la inactivitate, 1990+ cu Challenger și
  calificări). Rezultatul de selecție pe 2025 (80.2% la 30.7% acoperire, prag 0.72) a fost apoi
  pus în textul sarcinii fiecărui subagent.
- Nu se poate dovedi că alegerea configurației a folosit cifrele 2025 (validarea 2024 era afișată
  alături), dar este o **expunere a setului de test**. Prin urmare 2025 nu mai este un test
  curat, ci doar o estimare aproximativ independentă.
- Testul blocat a fost rulat de mai multe ori: rularea oficială `runs/locked_v2` (16:07), apoi
  `v1_test` (16:09) și `v2_test` (16:10), reluări ale recenzenților. `model.py` și `engine.py`
  nu s-au mai modificat după prima rulare, deci nimic nu s-a reglat pe ele. Totuși regula „o
  singură rulare pe versiune” a fost încălcată; **nu se mai rulează 2025 pentru v2**.
- Dovada cea mai curată în afara eșantionului sunt anii **2021/2022** (după înghețare, nereglați
  și nevăzuți în experimentul inițial): `select` ATP 0.809/0.814, WTA 0.822/0.800.
- Cifrele `select` 2023/2024 (0.815–0.825) sunt **în eșantion**: regula a fost aleasă dintre 18
  variante pe exact acești ani, deci sunt optimiste. Estimările cinstite sunt 2021/2022 (și, cu
  rezerva de mai sus, 2025).

### Testul blocat 2025 (rularea oficială, 29.09.2026, după înghețare)

Comandă: `python -m tenisPrediction.benchmark --model tenisPrediction.model:benchmark_factory
--years 2025 --tours atp,wta --locked-test` (1990–2025 trimise, 382.267 rânduri, 5.526 evaluate).
Aceeași comandă cu `tenisPrediction.candidates.baseline:factory` pentru v1.

| grup | model | n | log-loss | Brier | acc | ECE | cov@80 (prag) | select precizie/acoperire (n) |
|---|---|---|---|---|---|---|---|---|
| ATP 2025 | **v2** | 2922 | 0.6048 | 0.2099 | 0.6660 | 0.0209 | 0.2981 (0.755) | **0.801 / 0.277** (808) |
| ATP 2025 | v1 | 2922 | 0.6241 | 0.2173 | 0.6562 | 0.0281 | 0.2228 (0.789) | 0.762 / 0.369 (1079) |
| WTA 2025 | **v2** | 2604 | 0.6113 | 0.2109 | 0.6697 | 0.0186 | 0.3114 (0.732) | **0.805 / 0.290** (754) |
| WTA 2025 | v1 | 2604 | 0.6237 | 0.2175 | 0.6404 | 0.0391 | 0.2615 (0.761) | 0.768 / 0.316 (823) |

- 2025 a fost un an mai greu de prezis decât 2023/2024 (log-loss mai mare pentru ambele modele).
- Regula rulantă v2 a rămas peste 80% (0.801 și 0.805) **doar ca estimare punctuală**, la
  acoperire mai mică decât pe validare (~28–29% față de ~30–40%): pragul s-a ridicat singur pe
  măsură ce precizia recentă a scăzut. Marja este de 0.07 puncte (ATP) și 0.5 puncte (WTA);
  intervalele Wilson 95% sunt [0.772, 0.827] pentru ATP (808 selectate) și [0.775, 0.832] pentru
  WTA (754). Nu este o garanție că regula ține 80%.
- `cov@80` își alege pragul pe același an pe care îl scorează, deci este optimist prin
  construcție (ATP 2025: 0.298 față de 0.277 la `select`). Comparațiile se fac pe `select` și
  `transfer_80`.
- Calibrare: ECE general 0.021 pe ATP 2025 ascunde o supraîncredere în zona folosită de selecție
  (încredere declarată vs. precizie reală: 0.774 vs 0.706, 0.826 vs 0.760, 0.876 vs 0.832;
  meciurile ATP selectate au în medie 0.852 încredere și 0.801 precizie). Pe validarea
  2023/2024 aceleași intervale erau la 0.01–0.03. Pragul rulant absoarbe efectul pentru
  selecție, dar probabilitățile afișate pentru favoriți clari (de ex. 85%) au fost cu ~5 puncte
  prea mari pe ATP 2025. Nu s-a recalibrat pe 2025 (ar fi reglare pe testul blocat).
- Un prag fix ar fi picat: v2 la 0.72 dă 0.778 (ATP) / 0.799 (WTA); pragul cov@80 din 2024
  aplicat pe 2025 dă 0.763 / 0.772. Regula v1 (0.72 + 5 meciuri) dă 0.762 / 0.768.
- Nimic nu s-a modificat după această rulare.

## Observații despre harness (nu s-a modificat `benchmark.py`)

- Evenimentele cu aceeași dată de start sunt trimise complet (inclusiv finala) înaintea
  evenimentului următor cu aceeași dată. Verificare pe 2023/2024: niciun meci ATP/WTA evaluat nu
  are un jucător apărut într-un alt eveniment cu dată de start egală sau ulterioară deja trimis,
  deci starea jucătorilor nu are look-ahead între evenimente. Rămân efecte globale mici: media
  de serviciu `mu`, conținutul ferestrei rulante de selecție (poate include rezultate jucate după
  meciul prezis) și harta „acasă”. Afectează toți candidații la fel și poate înfrumuseța puțin
  precizia `select`.
- Rândurile Challenger sunt datate la sfârșitul săptămânii; 299 din 11.704 meciuri Challenger
  evaluate au un jucător cu un rând ATP/calificări de până la 5 zile mai devreme deja trimis →
  mic avantaj de look-ahead **numai** în metricile `challenger/*` (toți candidații).
- `--records` aplatizează câmpurile ctx în fiecare înregistrare (în loc de cheia `ctx`).
- Câteva meciuri de Davis Cup par duplicate în TML (impact neglijabil); unii jucători apar sub
  două chei (id lipsă → `name:...`, sau un al doilea id): la rezolvarea numelor se păstrează
  cheia cu cele mai multe meciuri.

## Limite ale regulii de selecție în aplicație

- Pragurile rulante au fost validate numai pe ATP main tour, WTA main tour (fișierele TML WTA
  conțin nivelurile 250/500/1000/G/D/O/F, fără WTA 125) și Challenger/calificări ATP. Pentru
  ITF (bărbați și femei), WTA 125 / „Challenger Women” și dublu, `match_facts` întoarce
  `validated=False`, iar decizia este mereu „fără pariu” (probabilitatea rămâne afișată).
- Regula Challenger este la limită pe validare: `transfer_80` 0.797 pe 2024 (sub 0.80).

## Bug-uri reparate în v2

- `resolve_player` nu recunoștea numele FlashScore cu mai multe cuvinte în nume de familie
  („De Minaur A.”, „Carreno Busta P.”, „Davidovich Fokina A.”, „Mpetshi Perricard G.”), nici
  diacriticele, cratimele și apostrofurile; acum potrivește „<cuvinte nume> <inițiale>.” pe
  sufixul numelui complet, acceptă inițiale multiple („Cerundolo J. M.”) sau prefixe
  („Pliskova Ka.”, „Zhang Zh.”), separă ATP/WTA și întoarce `None` la ambiguitate reală.
- `predict()` calcula experiența din numele nerezolvat, deci meciurile din API primeau mereu
  „fără pariu”; acum experiența și decizia folosesc cheia rezolvată.
- `predict_api_match` ghicea suprafața din numele ligii; acum folosește
  `footypreds.sports.tennis.surface_of` (sufixul FlashScore „..., clay”), cu rezervă pe cuvinte
  cheie și apoi „Hard”, iar circuitul (ATP/WTA/Challenger), `best_of` și nivelul (Grand Slam,
  Davis Cup) se detectează ca în `footypreds.sports.tennis`.
- `app.py` folosea `stats_scale=8.0` nevalidat și un amestec fix 10% model / 90% piață; acum
  folosește modelul v2 validat, ponderea validată 0.00 și un cache pickle în
  `footypreds/data/tenisPrediction/` (git-ignored), invalidat de dimensiunea și `mtime`-ul
  fișierelor de date.
- Aplicația trimitea ca „preț de piață” probabilitatea pieței `1` a modelului de bază footypreds
  (Elo intern, amestecat cu cotele când există), deci niciodată `null`; cu ponderea 0.00 modelul
  v2 nu schimba nimic pe pagină. Acum clientul trimite probabilitatea fără marjă din cotele reale
  1/2 (sau `null` fără cote), iar fără cote pagina afișează probabilitatea v2.
- Eticheta „selectează” putea însoți o probabilitate care favoriza celălalt jucător (piața
  contrazicea modelul). Răspunsul are acum `pick` / `pick_name` (jucătorul ales de v2),
  `model_decision` (decizia brută v2), iar `decision` este „selectează” doar dacă probabilitatea
  întoarsă favorizează același jucător; pagina o arată doar când recomandarea afișată este exact
  acel jucător.
- Rezolvarea numelor: cheia exactă respectă acum grupul de circuit (ATP/WTA); doi jucători
  omonimi cu id-uri și date de naștere diferite nu mai sunt comasați (numele rămâne ambiguu);
  comasarea se face doar pentru cheia fără id (`name:...`) sau pentru același jucător sub două
  id-uri cu aceeași dată de naștere (±60 de zile).
- Aplicația antrenează modelul într-un fir de fundal la pornire; `/api/health` răspunde imediat
  cu `status: loading`, iar celelalte rute întorc 503 până la final, în loc să blocheze toate
  cererile câteva minute. Clientul trimite și ziua meciului (`day`), deci decăderea la
  inactivitate nu mai depinde de ceasul sistemului.

# v3 (29.09.2026): cote, accidentări/oboseală, parametri pe circuit, calibrare

## Protocol v3

- **Reglare numai pe 2021–2023, confirmare o singură dată pe 2024.** Testul blocat 2025 a fost
  rulat o singură dată, la final, după verificarea independentă și înghețarea configurației
  (vezi „Verificarea finală” și „Testul blocat 2025” mai jos).
- Fiecare ajustare stă în spatele unui parametru și poate fi oprită; cu toate oprite
  (`tour_params={}`, `health_features=()`, `odds_weights=None`, `calib_mode="none"`) benchmark-ul
  reproduce **exact** cifrele v2 (verificat: log-loss/acuratețe/cov@80/`select` identice pe
  2021–2023).
- Referința v2 (2021/2022/2023/2024): log-loss ATP 0.5945/0.5761/0.5965/0.5880, WTA
  0.5953/0.5991/0.5917/0.5942; `select` ATP24 0.820/0.396, WTA24 0.825/0.313; media pieței la
  închidere (AvgW/AvgL) ATP23 0.5832, ATP24 0.5824, WTA23 0.5917, WTA24 0.5892.
- Coloane noi: `select_high` = regula „precizie înaltă” (țintă 85% în aceeași fereastră
  rulantă); `cov@85` = acoperirea maximă la 85% cu prag ales după fapt (plafon optimist, ca
  `cov@80`).
- Ordinea reglării: (3) parametri pe circuit → (2) trăsături de sănătate → (4) calibrare → (1)
  cote; fiecare pas a fost rulat cu benchmark-ul real (~50 s pe rulare), nu cu proxy-ul.

## 1. Cote pre-meci ca intrare (opt-in `--odds`, `odds_weights`) ✅ păstrat

- **Date**: registrele tennis-data.co.uk 2013–2024 (descărcate cu `tennis_eval --download`),
  potrivite cu rândurile TML după perechea de jucători („De Minaur A.” ↔ numele complet TML,
  și nume scrise invers) și o fereastră de date în jurul startului evenimentului; perechile
  întâlnite de două ori în fereastră sau revendicate de două ori rămân nepotrivite. Rata de
  potrivire: ATP 86–93% pe sezon, WTA 80–92% (2020 cel mai slab). Cotele sunt de **închidere**
  (media pieței AvgW/AvgL; PSW/PSL doar rezervă), mai ascuțite decât cotele FlashScore de
  dinaintea meciului. `--odds none` (implicit) lasă totul neschimbat.
- **Marja** (piața singură, 2021–2023, log-loss ATP/WTA): proporțional 0.5876/0.5864, **power
  0.5868/0.5851**, Shin 0.5869/0.5854, aditiv 0.5869/0.5854 → `power`. În benchmark, amestecul
  potrivit cu marja proporțională dă 0.5882/0.5723/0.5884 (ATP) și 0.5790/0.5845/0.5806 (WTA)
  față de 0.5876/0.5720/0.5880 și 0.5787/0.5840/0.5802 cu `power` (motor v2).
- **Amestec**: logit = w_model·z_model + w_piață·logit(p_piață), **fără intercept** (rândurile
  sunt orientate câștigător-primul, iar modelul trebuie să rămână exact simetric; un intercept
  ar învăța orientarea). `odds_weights="fit"` potrivește perechea la fiecare început de sezon,
  numai pe perechile (z_model, z_piață) **în afara eșantionului din sezoanele anterioare** (per
  circuit, cu rezervă comună). Ponderile în vigoare (model, piață):

  | sezon | ATP | WTA |
  |---|---|---|
  | 2017 | (0.03, 0.95) | (−0.01, 0.93) |
  | 2019 | (0.05, 0.91) | (−0.05, 0.99) |
  | 2021 | (0.05, 0.91) | (−0.02, 0.97) |
  | 2022 | (0.07, 0.89) | (−0.02, 0.99) |
  | 2023 | (0.08, 0.88) | (−0.02, 0.98) |
  | 2024 | (0.08, 0.89) | (−0.02, 0.98) |

  (2015/2016 au ponderi instabile — puține perechi, iar cotele ATP 2015 sunt neobișnuit de
  ascuțite, LL 0.549 — irelevante pentru anii evaluați.) Concluzia v2 se confirmă: pe cotele
  de închidere modelul TML adaugă foarte puțin.
- **Fereastra de selecție urmărește probabilitatea folosită** (`odds_track=True`): cu fereastra
  pe modelul pur și decizia pe amestec (motorul v2, 2021–2023) `select` dă ATP 0.827/0.320,
  0.811/0.438, 0.814/0.360 și WTA 0.839/0.305, 0.812/0.390, 0.821/0.335 — precizie bună dar
  acoperire mai mică decât cu fereastra pe amestec (tabelul de mai jos). Păstrat.
- **Aplicația** nu vede cote de închidere, ci cotele FlashScore de dinaintea meciului. Cu zgomot
  N(0, σ) pe logit-ul pieței (2021–2023, motor v2), ponderile potrivite devin: σ=0 → (0.05,
  0.93); σ=0.15 → (0.20, 0.78); σ=0.25 → (0.34, 0.64); σ=0.35 → (0.54, 0.43). Perechea fixă
  **(0.25, 0.70)** pierde ≤ 0.0007 log-loss față de optim pentru σ ≤ 0.25 și doar 0.0007 față
  de ponderile potrivite pe cotele de închidere — este alegerea de producție
  (`PRODUCTION_ODDS_WEIGHTS`, `production_factory`), validată în benchmark cu aceleași ponderi.
  În aplicație clientul trimite cotele reale 1/2, serverul scoate marja (`power`), amestecă,
  iar `pick`, „selectează” și „precizie înaltă” vin din probabilitatea amestecată; modelul de
  producție se antrenează cu cotele de închidere potrivite (când fișierele există), deci
  fereastra rulantă urmărește tot amestecul.

Rezultate 2021–2023 (motor v3 final; „fără cote” = modelul pur, „fit” = ponderi potrivite
walk-forward, „prod” = (0.25, 0.70)):

| grup | variantă | log-loss | acc | select | select_high | cov@80 | cov@85 |
|---|---|---|---|---|---|---|---|
| atp/2021 | fără cote | 0.5936 | 0.677 | 0.817/0.313 | 0.846/0.241 | 0.403 | 0.235 |
| atp/2021 | fit | 0.5873 | 0.688 | 0.804/0.388 | 0.837/0.292 | 0.445 | 0.270 |
| atp/2021 | prod | 0.5869 | 0.688 | 0.808/0.387 | 0.839/0.288 | 0.435 | 0.246 |
| atp/2022 | fără cote | 0.5744 | 0.682 | 0.815/0.426 | 0.847/0.323 | 0.507 | 0.321 |
| atp/2022 | fit | 0.5719 | 0.686 | 0.815/0.439 | 0.849/0.308 | 0.519 | 0.324 |
| atp/2022 | prod | 0.5714 | 0.684 | 0.817/0.438 | 0.846/0.320 | 0.540 | 0.335 |
| atp/2023 | fără cote | 0.5959 | 0.662 | 0.814/0.379 | 0.841/0.264 | 0.407 | 0.269 |
| atp/2023 | fit | 0.5878 | 0.676 | 0.814/0.377 | 0.845/0.283 | 0.425 | 0.281 |
| atp/2023 | prod | 0.5885 | 0.675 | 0.813/0.391 | 0.847/0.282 | 0.424 | 0.276 |
| wta/2021 | fără cote | 0.5932 | 0.686 | 0.826/0.290 | 0.851/0.204 | 0.385 | 0.226 |
| wta/2021 | fit | 0.5785 | 0.688 | 0.823/0.408 | 0.846/0.290 | 0.504 | 0.279 |
| wta/2021 | prod | 0.5799 | 0.688 | 0.829/0.386 | 0.850/0.270 | 0.472 | 0.281 |
| wta/2022 | fără cote | 0.5970 | 0.672 | 0.809/0.373 | 0.836/0.263 | 0.374 | 0.245 |
| wta/2022 | fit | 0.5843 | 0.681 | 0.804/0.422 | 0.845/0.302 | 0.440 | 0.284 |
| wta/2022 | prod | 0.5851 | 0.677 | 0.804/0.414 | 0.845/0.306 | 0.429 | 0.296 |
| wta/2023 | fără cote | 0.5918 | 0.679 | 0.819/0.313 | 0.848/0.239 | 0.364 | 0.220 |
| wta/2023 | fit | 0.5799 | 0.690 | 0.817/0.364 | 0.847/0.288 | 0.439 | 0.283 |
| wta/2023 | prod | 0.5808 | 0.689 | 0.821/0.351 | 0.846/0.279 | 0.438 | 0.273 |

Cu cote, log-loss-ul scade cu ~0.006–0.008 (ATP) și ~0.012–0.015 (WTA), iar `select` păstrează
≥ 0.80 în toate cele 6 celule cu acoperire mai mare cu 5–12 puncte. Marja de precizie este
însă mică (ATP21 0.804–0.808, WTA22 0.804).

## 2. Semnale de accidentare/oboseală (`health_features`) ✅ păstrate parțial

Trăsături (diferențe antisimetrice, citite numai din meciuri deja jucate; ziua meciului este
aproximată din rundă, pentru că `tourney_date` este startul evenimentului): `exit_14`/`exit_30`
(ultima apariție a fost o retragere/walkover dat, ≤ 14/30 zile), `wo_60` (walkovere date în 60
de zile), `load_48`/`load_72` (minute în ultimele ~2/3 zile), `prev_long` (meci anterior lung în
același turneu), `comeback` (1/(1+meciuri de la revenirea după > 60 de zile)), `comeback_away`
(× log(absență/60)), `comeback_time` (exp(−zile de la revenire/30)).

- Ablație pe motorul v2 (2021–2023, media pe 6 celule, rulări reale): toate 9 → 0.5916 față de
  0.5922 de bază; scoaterea trio-ului de revenire pierde 0.00055; scoaterea oricărei alte
  trăsături schimbă ≤ 0.00003 (`load_48`, `load_72`, `wo_60` chiar ușor mai bine fără ele).
- Pe motorul v3 (parametrii pe circuit de la punctul 3): fără sănătate 0.59153; **trio revenire
  0.59098**; trio + `exit_14` + `wo_60` 0.59095; toate 9 0.59099. Diferențele dintre ultimele
  trei sunt zgomot (≤ 0.00004), deci se păstrează doar trio-ul de revenire (cel mai simplu).
  Retragerile, walkoverele date, minutele recente și meciul lung anterior sunt implementate,
  dar oprite: nu au ajutat pe 2021–2023.
- 2024 (confirmare unică, împreună cu punctul 3): ATP mai bun (0.5869 față de 0.5880), WTA
  mai slab (0.5962 față de 0.5942). Nu s-a rulat o a doua configurație pe 2024 pentru a
  atribui diferența WTA (parametri sau sănătate), ca să nu se regleze pe anul de confirmare;
  vezi tabelul final.

## 3. Parametri de motor pe circuit (`tour_params`) ✅ păstrat doar ATP (WTA scos la confirmare)

`FeatureEngine(tour_params={"atp": {...}, "wta": {...}})` suprascrie pe grup de circuit (ATP +
Challenger + calificări împart jucătorii; WTA separat) K-ul Elo (bază/offset/formă), ponderea
suprafeței, MOV, K la Grand Slam, decăderea la inactivitate, ratingul inițial, ratele de
învățare serviciu/retur (general și pe suprafață) și decăderea formei.

- Căutare pe coordonate cu un proxy fidel (trăsăturile celuilalt grup din cache, stiva
  reantrenată pe sezon, selecția rulantă rejucată; 2021–2023): WTA 33 configurații cu un
  parametru, apoi combinații (K-offset 2, fast_k 30, k_shape 0.5 ajută marginal); ATP 24
  configurații (k_shape 0.5 și k_base 200 sunt singurele cu efect).
- Confirmare cu benchmark-ul real, 2021–2023, media log-loss (ATP / WTA / ambele):

  | configurație | ATP | WTA | ambele |
  |---|---|---|---|
  | v2 | 0.58903 | 0.59535 | 0.59219 |
  | WTA F (form 0.95, mov 0, idle 250/0) | 0.58929 | 0.59451 | 0.59190 |
  | WTA G (F + k_offset 2, fast_k 30, k_shape 0.5) | 0.58920 | 0.59425 | 0.59173 |
  | ATP k_shape 0.5 | 0.58846 | 0.59536 | 0.59191 |
  | ATP k_shape 0.5 + k_base 200 | 0.58835 | 0.59538 | 0.59186 |
  | **A = ATP (k_shape 0.5, k_base 200) + WTA F** | 0.58859 | 0.59446 | **0.59153** |
  | B = ATP idem + WTA G | 0.58854 | 0.59423 | 0.59139 |
  | C = ATP idem + WTA F + k_shape 0.5, k_base 200 | 0.58858 | 0.59446 | 0.59152 |

  Schimbarea WTA costă ATP ~0.0003 prin stiva comună. B câștigă doar 0.00014 față de A cu
  trei parametri în plus (zgomot), iar C scade `select` WTA 2022 la 0.797 → **A** (6
  parametri) a fost configurația dusă la confirmarea pe 2024.
- **Decizie după confirmare (regula „confirmă sau renunță”)**: pe 2024 partea ATP a confirmat
  (0.5869 față de 0.5880), partea WTA nu (0.5962 față de 0.5942, `select` egal). Suprascrierea
  WTA a fost **scoasă**; `DEFAULT_TOUR_PARAMS = {"atp": {"k_shape": 0.5, "k_base": 200}}`,
  iar grupul feminin rulează dinamica v2. Nu s-a făcut nicio altă reglare pe 2024; rularea de
  raportare a configurației finale este în „Verificarea finală”.

## 4. Calibrare în S a probabilității afișate (`calib_mode`) ❌ implementată, oprită implicit

Calibratoare antisimetrice pe logit-ul câștigător-primul, potrivite pe fereastra rulantă de
predicții în afara eșantionului (per circuit, refit la 250 de rezultate): temperatură `a·z`,
curbă S `a·z + b·z|z|` (ridge spre identitate), izotonic simetrizat (PAV pe |z|). Rejucare
offline pe fluxul motorului v3 (2021–2023; ΔLL = media pe 6 celule față de brut, în puncte de
bază; gap = abaterea medie încredere − precizie în intervalele 70–90%):

| calibrator | fereastră | ΔLL | gap 70–90 (abs.) |
|---|---|---|---|
| brut | – | 0 | 0.0147 |
| temperatură | 5000 | −0.2 bp | 0.0116 |
| temperatură | 60000, semiviață 2000 | −0.5 bp | 0.0102 |
| curbă S | 2000 | +4.0 bp | 0.0080 |
| curbă S | 60000, semiviață 2000 | +0.9 bp | 0.0091 |
| izotonic | 2000 | +5.6 bp | 0.0081 |
| izotonic | 20000 | +3.5 bp | 0.0097 |

Nicio variantă nu îmbunătățește log-loss-ul (cel mai bun −0.5 bp = zgomot), iar curba S și
izotonicul îl înrăutățesc cu 1–6 bp; fluxul brut este deja aproape calibrat pe 2021–2023
(supraîncrederea observată era specifică ATP 2025). Prin urmare `calib_mode="none"` este
implicit; selecția rămâne pe încrederea brută (`calib_select=False`; cu calibrare pornită,
pragul afișat este imaginea pragului brut prin aceeași curbă monotonă, deci decizia nu se
schimbă). Nu s-a confirmat pe 2024 (opțiune respinsă).

**Profil „precizie înaltă”** (`select_profile="high"`, `select_high`, `decision_high`): al doilea
prag din aceeași fereastră, țintă 85%. 2021–2023 fără cote: ATP 0.846/0.241, 0.847/0.323,
0.841/0.264; WTA 0.851/0.204, 0.836/0.263, 0.848/0.239. 2024: vezi tabelul final.

## Confirmarea 2024 (o singură rulare pe configurație finală)

| grup | model | log-loss | acc | select | select_high | cov@80 | cov@85 |
|---|---|---|---|---|---|---|---|
| atp/2024 | v2 (referință) | 0.5880 | 0.673 | 0.820/0.396 | – | 0.454 | – |
| atp/2024 | v3 fără cote | 0.5869 | 0.674 | 0.818/0.407 | 0.837/0.318 | 0.470 | 0.320 |
| atp/2024 | v3 + cote fit | 0.5847 | 0.688 | 0.807/0.413 | 0.838/0.311 | 0.475 | 0.291 |
| atp/2024 | v3 + cote prod (aplicația) | 0.5845 | 0.685 | 0.807/0.419 | 0.836/0.313 | 0.476 | 0.302 |
| atp/2024 | piața (medie închidere) | 0.5824 | – | – | – | – | – |
| wta/2024 | v2 (referință) | 0.5942 | 0.662 | 0.825/0.313 | – | 0.423 | – |
| wta/2024 | v3 fără cote | 0.5962 | 0.660 | 0.819/0.314 | 0.854/0.218 | 0.411 | 0.228 |
| wta/2024 | v3 + cote fit | 0.5834 | 0.669 | 0.824/0.379 | 0.849/0.293 | 0.464 | 0.282 |
| wta/2024 | v3 + cote prod (aplicația) | 0.5845 | 0.670 | 0.824/0.372 | 0.846/0.276 | 0.461 | 0.284 |
| wta/2024 | piața (medie închidere) | 0.5892 | – | – | – | – | – |

- Fără cote: ATP confirmă (log-loss −0.0011, `select` 0.818 la acoperire +1.1 puncte, cov@80
  +1.6 puncte); WTA nu confirmă în log-loss (+0.0020, `select` 0.819/0.314 ≈ v2, cov@80 −1.2
  puncte). Câștigul de 0.0009 pe WTA 2021–2023 nu se regăsește pe 2024 — diferența este de
  ordinul variației anuale, dar trebuie spusă: pe WTA, v3 fără cote nu este demonstrabil mai
  bun decât v2.
- Cu cotele de închidere: ATP 0.5845–0.5847 (piața singură 0.5824), WTA 0.5834–0.5845 (mai
  bine decât piața singură 0.5892, pentru că logit-ul pieței este re-scalat de ponderi);
  `select` 0.807/0.413–0.419 și 0.824/0.372–0.379; „precizie înaltă” 0.836–0.838/0.31 și
  0.846–0.849/0.28–0.29. ATP `select` 0.807 este sub ținta 0.82, dar peste 0.80.
- Aceste cifre sunt cu cote de **închidere**; în aplicație (cote FlashScore mai timpurii)
  precizia efectivă este de așteptat între „fără cote” și „prod”.

## Rezumat v3

| ajustare | decizie | 2021–2023 | 2024 |
|---|---|---|---|
| 1. cote (fit / prod) | păstrat (opt-in în benchmark; aplicația folosește prod) | LL −0.006…−0.015 | ATP 0.5845, WTA 0.5845 (prod) |
| 2. sănătate | păstrat trio revenire; restul implementat, oprit | −0.00055 (media) | vezi tabel (neatribuibil) |
| 3. parametri pe circuit | păstrat doar ATP (k_shape 0.5, k_base 200); WTA scos | −0.00066 (media, A) | ATP ✓, WTA ✗ (log-loss) → scos |
| 4. calibrare S | implementată, oprită implicit | ≥ 0 bp | nerulat |
| profil precizie înaltă | păstrat (al doilea prag) | 0.84–0.85 la 20–32% | ATP 0.837/0.318, WTA 0.854/0.218 |

Modelul de producție: `TennisModel()` (implicit: parametri ATP + trio revenire, WTA pe dinamica
v2, fără calibrare) antrenat cu `odds="avg"` și `odds_weights=(0.25, 0.70)`; cache
`model_v3.pkl`, cheia include versiunea, parametrii și fișierele de date/cote.

## Verificarea finală (audit independent, 29.09.2026)

Verificare făcută de un al doilea agent, o singură trecere, înainte de testul blocat:

- **Paritate cu v2**: `--param "tour_params={}" --param "health_features=()"` pe 2023 dă exact
  v2: ATP 0.5965, acc 0.6629, cov@80 0.390, `select` 0.815/0.371; WTA 0.5917, 0.6790, 0.386,
  0.816/0.302.
- **Potrivirea cotelor** (rândurile reale ATP/WTA 2019–2024, 27 436 rânduri cu cote): niciun
  rând nu primește cotele altei întâlniri. Perechile întâlnite de două ori în fereastra de 24 de
  zile: 682, din care 615 cu ambele rânduri potrivite, toate pe înregistrări distincte și în
  aceeași ordine a zilelor (0 inversări); întâlnirile duble în același eveniment (grupă + finală
  la Turneul Campionilor) rămân nepotrivite, nu ghicite. Orientarea: rata favoritului câștigător
  pe rândurile potrivite (ATP 0.6801, WTA 0.6737) este egală cu cea din registrele brute (0.6800,
  0.6740); cele 13 rânduri în care câștigătorul TML citește ca „Loser” în tennis-data sunt
  dezacorduri de sursă la retrageri/walkovere (prețul urmează jucătorul, deci corect). Ziua
  tennis-data cade în [−4, +14] zile față de startul evenimentului TML (49 de rânduri la −2…−4:
  evenimente datate luni cu prima rundă sâmbătă; aceeași întâlnire). Neajuns cunoscut: o finală
  de duminică urmată de aceeași pereche în evenimentul de luni poate lăsa ambele rânduri fără
  cote (conflict), niciodată cu cote greșite.
- **Ponderi „fit”**: potrivite doar la începutul sezonului, pe perechile (z_model, z_piață) în
  afara eșantionului din sezoanele anterioare (test `test_online_blend_weights_come_from_earlier_seasons_only`).
- **Ferestre rulante** (selecție, calibrare): primesc logit-ul unui meci numai în `update`,
  după ce `predict` l-a folosit; ziua aproximată a meciului și starea de sănătate se citesc
  doar din meciuri deja trimise, iar `store=False` nu creează stare.
- **Parametri pe circuit**: reglați pe 2021–2023 (tabelul de mai sus), confirmați o dată pe
  2024; WTA scos (vezi punctul 3).
- **Aplicația** (TestClient): cu și fără cote, `probability`, `pick`, `pick_name`, `decision` și
  `decision_high` vin din același număr (test nou `test_predict_endpoint_with_raw_odds_keeps_every_field_on_the_same_player`);
  cheia cache-ului include versiunea și fișierele de cote; `/api/health` răspunde `loading`
  fără să blocheze pornirea.
- Teste noi: `test_odds_join_keeps_each_meeting_on_its_own_record`,
  `test_default_tour_params_keep_the_v2_dynamics_for_wta`.

Configurația finală (parametri ATP + trio revenire, WTA v2), o singură rulare 2021–2024 pentru
raportare (2024 atins a doua oară doar pentru raportarea configurației finale, fără reglare):

| grup | variantă | log-loss (v2) | acc | select | select_high | cov@80 |
|---|---|---|---|---|---|---|
| atp/2021 | fără cote | 0.5935 (0.5945) | 0.677 | 0.814/0.311 | 0.847/0.239 | 0.396 |
| atp/2022 | fără cote | 0.5742 (0.5761) | 0.681 | 0.811/0.435 | 0.846/0.324 | 0.511 |
| atp/2023 | fără cote | 0.5955 (0.5965) | 0.663 | 0.817/0.380 | 0.840/0.275 | 0.404 |
| atp/2024 | fără cote | 0.5867 (0.5880) | 0.673 | 0.822/0.403 | 0.836/0.316 | 0.471 |
| wta/2021 | fără cote | 0.5940 (0.5953) | 0.681 | 0.826/0.277 | 0.848/0.202 | 0.391 |
| wta/2022 | fără cote | 0.5981 (0.5991) | 0.673 | 0.807/0.376 | 0.838/0.259 | 0.392 |
| wta/2023 | fără cote | 0.5922 (0.5917) | 0.683 | 0.820/0.313 | 0.844/0.235 | 0.378 |
| wta/2024 | fără cote | 0.5958 (0.5942) | 0.657 | 0.821/0.308 | 0.863/0.224 | 0.419 |
| atp/2021 | prod (0.25, 0.70) | 0.5866 | 0.689 | 0.809/0.386 | 0.837/0.287 | 0.433 |
| atp/2022 | prod | 0.5713 | 0.682 | 0.814/0.438 | 0.843/0.325 | 0.512 |
| atp/2023 | prod | 0.5883 | 0.675 | 0.814/0.390 | 0.848/0.283 | 0.432 |
| atp/2024 | prod | 0.5844 | 0.685 | 0.809/0.418 | 0.838/0.311 | 0.475 |
| wta/2021 | prod | 0.5801 | 0.688 | 0.824/0.389 | 0.849/0.255 | 0.488 |
| wta/2022 | prod | 0.5854 | 0.680 | 0.807/0.411 | 0.845/0.306 | 0.425 |
| wta/2023 | prod | 0.5810 | 0.692 | 0.818/0.352 | 0.841/0.276 | 0.436 |
| wta/2024 | prod | 0.5846 | 0.671 | 0.826/0.372 | 0.847/0.267 | 0.457 |

Scoaterea suprascrierii WTA nu schimbă concluzia pe WTA 2024 (0.5958 față de 0.5962 cu ea și
0.5942 v2): diferența față de v2 nu venea din parametrii WTA, ci din trio-ul de revenire și/sau
din stiva comună schimbată de parametrii ATP. Nu s-a încercat nicio a treia configurație pe
2024. Pe WTA, v3 fără cote rămâne **nedemonstrat** față de v2 (2021–2023 −0.0006 în medie,
2024 +0.0016); pe ATP v3 este mai bun în toți cei patru ani (−0.0010…−0.0019).

## Testul blocat 2025 (o singură rulare pe variantă, după înghețare)

Rulat cu `--locked-test` o dată pentru `benchmark_factory` (fără cote) și o dată pentru
`production_factory --odds avg` (configurația aplicației; registrele tennis-data 2025 descărcate
înainte, rată de potrivire ATP 2613/2922 = 89.4%, WTA 2483/2604 = 95.4%). Nimic din model nu s-a
schimbat după aceste cifre. **2025 nu este un test curat**: a fost expus deja în v1/v2 (vezi
„Expunerea anului 2025”), iar aici este a doua versiune evaluată pe el.

| grup | model | n | log-loss | brier | acc | ece | cov@80 | thr80 | acc@.72 | cov@.72 | select | select_high |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| atp/2025 | v2 (referință) | 2922 | 0.6048 | – | 0.666 | – | 0.298 | – | – | – | 0.801/0.277 | – |
| atp/2025 | v3 fără cote | 2922 | 0.6036 | 0.2093 | 0.6704 | 0.0247 | 0.2930 | 0.757 | 0.7775 | 0.3737 | 0.812/0.264 | 0.840/0.194 |
| atp/2025 | v3 + cote prod | 2922 | 0.5948 | 0.2056 | 0.6759 | 0.0194 | 0.3036 | 0.744 | 0.7826 | 0.3542 | 0.812/0.280 | 0.858/0.219 |
| wta/2025 | v2 (referință) | 2604 | 0.6113 | – | 0.670 | – | 0.311 | – | – | – | 0.805/0.290 | – |
| wta/2025 | v3 fără cote | 2604 | 0.6115 | 0.2111 | 0.6736 | 0.0182 | 0.2999 | 0.738 | 0.7899 | 0.3418 | 0.805/0.287 | 0.821/0.178 |
| wta/2025 | v3 + cote prod | 2604 | 0.5940 | 0.2048 | 0.6770 | 0.0205 | 0.3959 | 0.696 | 0.8197 | 0.3387 | 0.811/0.354 | 0.830/0.239 |

- Fără cote, față de v2: ATP log-loss −0.0012, acc +0.4 puncte, `select` 0.812/0.264 (precizie
  +1.1 puncte, acoperire −1.3); WTA log-loss +0.0002 (egal), acc +0.4, `select` 0.805/0.287
  (egal). Concluzia de pe 2021–2024 se confirmă: v3 fără cote ajută puțin pe ATP și deloc
  pe WTA.
- Cu cotele de închidere (aplicația): log-loss 0.5948 / 0.5940, `select` 0.812/0.280 și
  0.811/0.354; „precizie înaltă” 0.858/0.219 (ATP, singura celulă peste ținta 85%) și
  0.830/0.239 (WTA). Cotele FlashScore din aplicație sunt mai timpurii, deci cifrele reale sunt de
  așteptat între cele două variante.
- Regula rulantă și-a ridicat singură pragul (thr80 0.74–0.76 fără cote), ca în v2; un prag fix
  0.72 ar fi dat doar 77.8–79.0% fără cote.
