# tenisPrediction v3 — predicții de tenis fără leakage

Model walk-forward pentru ATP, WTA și Challenger, antrenat pe fișierele TML locale
(`tml-data/`, 1990 → prezent, toate circuitele). Istoricul și experimentele sunt în
[`EXPERIMENTS.md`](EXPERIMENTS.md).

## Pe scurt, cinstit

- **Peste 80% pe toate meciurile nu este posibil**: modelul are ~66–68% acuratețe generală,
  aproape de cotele de închidere (~68–70%).
- Ce se poate: o **selecție** a meciurilor în care modelul este sigur. Regula validată alege
  ~30–40% din meciurile ATP/WTA cu ~81–82% precizie (validare 2023/2024) și ~11% din meciurile
  Challenger. Cifrele 2023/2024 sunt în eșantion (regula a fost aleasă pe acești ani); estimarea
  cea mai curată sunt anii nereglați 2021/2022: 80.9% / 81.4% (ATP), 82.2% / 80.0% (WTA).
- Pe testul 2025 (v3, fără cote): ~26–29% din meciuri, cu 81.2% (ATP) și 80.5% (WTA) —
  **estimări punctuale** pe ~770 / ~750 de meciuri selectate (interval Wilson 95% aproximativ
  ±2.8 puncte), deci un an nou poate ieși și sub 80%. Cu cotele de închidere: 81.2% la 28%
  (ATP) și 81.1% la 35% (WTA). În plus, 2025 **nu este un test curat**: un experiment inițial a
  afișat rezultate pe 2025 înainte de proiectarea modelului, iar testul a fost rulat pentru v1,
  v2 și v3 (vezi EXPERIMENTS.md, „Expunerea anului 2025” și „Testul blocat 2025”).
- Pe ATP 2025 probabilitățile mari au fost supraîncrezătoare (~5 puncte la favoriții de ~85%);
  selecția compensează prin pragul rulant, dar procentele afișate pentru favoriți clari pot fi
  prea mari. v3 are un calibrator opțional (`calib_mode`), dar pe 2021–2023 nicio variantă nu a
  ajutat, deci rămâne oprit.
- Modelul **nu bate piața**: față de cota medie de închidere (tennis-data.co.uk) fără marjă
  adaugă foarte puțin (ponderea potrivită a modelului în amestec ≈ 0.05–0.08 pe ATP, ≈ 0 pe
  WTA). v3 folosește cotele ca intrare când există. Nu este o sursă de profit garantat.

## Cum funcționează

- `engine.py`: starea jucătorilor, cheiată după id-ul TML — Elo 538 general și pe suprafață,
  Elo rapid, Elo pe game-uri și pe puncte, serviciu/retur ajustat după adversar transformat în
  probabilitate de meci printr-un lanț Markov exact (punct → game → tiebreak → set → meci), rang,
  puncte, vârstă, înălțime, intrare (Q/WC/LL/PR), formă, volatilitate, oboseală, H2H. Scorul,
  minutele și statisticile unui meci intră în stare numai după ce meciul a fost prezis.
  - v3: dinamica (K, pondere suprafață, MOV, decădere la inactivitate, rate de învățare la
    serviciu, formă) poate fi suprascrisă pe grup de circuit (`tour_params={"atp": ..., "wta":
    ...}`); implicit doar ATP are un K care scade mai repede. Candidatul WTA (memorie de formă
    mai lungă, fără bonus de victorie în seturi directe, decădere la inactivitate din prima zi)
    nu s-a confirmat pe 2024 și a fost scos: grupul feminin rulează dinamica v2.
  - v3: semnale de accidentare/oboseală (`health_features`): retragere/walkover dat recent,
    walkovere date în 60 de zile, minute jucate în ultimele 48/72 h (ziua meciului aproximată
    din rundă, numai din meciuri deja jucate), meci lung anterior în același turneu, revenire
    după o absență de peste 60 de zile. Implicit sunt active doar cele trei trăsături de
    revenire (singurele care au ajutat pe 2021–2023).
- `odds.py` (v3): citirea registrelor tennis-data.co.uk, potrivirea lor cu rândurile TML după
  perechea de jucători și dată, scoaterea marjei (`proportional`, `power`, `shin`, `additive`)
  și amestecul model/piață pe scara logit. Cotele sunt de **închidere** (media pieței
  AvgW/AvgL; PSW/PSL doar ca rezervă), deci mai ascuțite decât cotele FlashScore de dinaintea
  meciului.
- `calibration.py` (v3): calibrare antisimetrică a probabilității afișate (temperatură, curbă S
  `a·z + b·z|z|`, izotonic simetrizat) potrivită pe fereastra rulantă de predicții în afara
  eșantionului. Implicit **oprită**: nicio variantă nu a îmbunătățit log-loss-ul pe 2021–2023.
- `model.py`:
  - `TennisModel`: regresie logistică simetrică fără intercept (p(A,B) = 1 − p(B,A) exact),
    reantrenată la începutul fiecărui sezon pe toate meciurile anterioare. Cu cote
    (`odds_weights`): logit = w_model·z_model + w_piață·logit(p_piață), ponderi fixe sau
    potrivite walk-forward (`"fit"`) numai pe perechile în afara eșantionului din sezoanele
    anterioare; fără cote rămâne modelul pur. Regula de selecție „selectează” / „fără pariu”:
    pentru fiecare circuit, fereastra ultimelor 1000 de predicții în afara eșantionului (cu
    amestecul de piață când există); pragul este cea mai mică încredere la care felia de sus a
    ferestrei are precizia ≥ 82% (83% la Challenger). Din aceeași fereastră vine și pragul
    „precizie înaltă” (țintă 85%, `select_high` / `decision_high`).
  - `TennisPredictor`: antrenare + cache (`model_v3.pkl`, cheie = versiune + parametri +
    fișiere de date și de cote), rezolvarea numelor FlashScore („De Minaur A.”,
    „Cerundolo J. M.”, „Auger-Aliassime F.”; ambiguitatea reală → jucător necunoscut),
    predicții după nume cu ultimul rang, vârsta, înălțimea și mâna cunoscute, cu sau fără preț
    de piață.
- `legacy.py`: modelul compact v1 (`CompactTennisModel`), păstrat doar ca referință.

## Comenzi

```powershell
# antrenează o dată (~2 min) și salvează în footypreds/data/tenisPrediction/ (git-ignored);
# cache-ul se reface automat când se schimbă fișierele din tml-data/ sau cotele
python -m tenisPrediction.train

# cotele de închidere tennis-data.co.uk (2013+; git-ignored, footypreds/data/benchmark/tennis/raw)
python -m footypreds.evaluation.tennis_eval --download

# aplicația separată pe portul 8010
python -m uvicorn tenisPrediction.app:app --host 127.0.0.1 --port 8010

# evaluare pe validare (2023, 2024; ATP+WTA), înveliș peste benchmark.py
python -m tenisPrediction.evaluate
python -m tenisPrediction.evaluate --baseline                   # modelul v1
python -m tenisPrediction.evaluate --tours atp,wta,challenger --json out.json

# benchmark-ul complet (protocolul este în docstring-ul din benchmark.py)
python -m tenisPrediction.benchmark --model tenisPrediction.model:benchmark_factory --years 2021,2022,2023 --tours atp,wta
# cu cotele de închidere în context (--odds none, implicit, dă exact cifrele fără cote)
python -m tenisPrediction.benchmark --model tenisPrediction.model:odds_factory --years 2021,2022,2023 --tours atp,wta --odds avg
python -m tenisPrediction.benchmark --model tenisPrediction.model:production_factory --years 2024 --tours atp,wta --odds avg
# v2 exact: --param "tour_params={}" --param "health_features=()"
```

Tuning numai pe 2021–2023, confirmare o singură dată pe 2024. Anul 2025 este testul blocat:
benchmark-ul îl refuză fără `--locked-test`, care se rulează o singură dată pe versiune de model
(pentru v3 a fost rulat o singură dată, la final; rezultatele sunt mai jos).

Utilizare din cod:

```python
from tenisPrediction import TennisPredictor

model = TennisPredictor.load_or_train(odds="avg")  # cotele de închidere, dacă există local
model.predict("Sinner J.", "De Minaur A.", "Hard", tour="atp", market_probability=0.7).as_dict()
# footypreds.domain.Match de la FlashScore (cotele 1/2 intră în amestec)
model.predict_api_match(match)
```

`predict_api_match` ia suprafața din sufixul FlashScore („..., clay”) cu
`footypreds.sports.tennis.surface_of`, circuitul (ATP/WTA/Challenger), `best_of` (5 doar la
Grand Slam masculin) și nivelul (Grand Slam, Davis Cup) din numele ligii.

În aplicație, pagina trimite cotele reale 1/2 (niciodată probabilitatea modelului de bază);
serverul scoate marja (metoda „power”) și amestecă pe scara logit cu ponderile de producție
(0.25 model, 0.70 piață). Ponderile potrivite pe cotele de închidere sunt ~(0.1, 0.9), dar
cotele FlashScore de dinaintea meciului sunt mai timpurii și mai zgomotoase, iar cu zgomot pe
logit-ul pieței optimul se mută spre (0.25–0.35, 0.6–0.7); perechea fixă a fost validată în
benchmark pe 2021–2023 și confirmată pe 2024. Fără cote, probabilitatea afișată este cea a
modelului v3. `pick`, „selectează” și „precizie înaltă” vin din probabilitatea amestecată (cea
afișată), deci eticheta nu poate însoți jucătorul pe care numărul nu îl favorizează. Pentru
ITF, WTA 125 și dublu regula nu a fost validată, deci decizia este mereu „fără pariu”. La prima
pornire modelul se antrenează în fundal (câteva minute); între timp pagina afișează modelul de
bază și „Modelul v3 se antrenează…”.

## Rezultate

Validare (1990–2024 trimise online, fără antrenare în avans). `select` = regula proprie, aplicată
în timp real; `select_high` = profilul „precizie înaltă” (țintă 85%); `cov@80` = acoperirea
maximă la 80% cu prag ales după fapt (plafon optimist). v3 a fost reglat pe 2021–2023 și
confirmat o singură dată pe 2024; detaliile și toate variantele sunt în EXPERIMENTS.md, „v3”.

Fără cote (modelul pur), v3 față de v2:

Configurația finală (parametri ATP + trio revenire; suprascrierea WTA a fost scoasă pentru că
nu s-a confirmat pe 2024):

| grup | log-loss v3 (v2) | acc | select v3 (v2) | select_high | cov@80 v3 (v2) |
|---|---|---|---|---|---|
| ATP 2021 | 0.5935 (0.5945) | 0.677 | 0.814 / 0.311 (0.809 / 0.333) | 0.847 / 0.239 | 0.396 (0.395) |
| ATP 2022 | 0.5742 (0.5761) | 0.681 | 0.811 / 0.435 (0.814 / 0.417) | 0.846 / 0.324 | 0.511 (0.499) |
| ATP 2023 | 0.5955 (0.5965) | 0.663 | 0.817 / 0.380 (0.815 / 0.371) | 0.840 / 0.275 | 0.404 (0.390) |
| ATP 2024 | 0.5867 (0.5880) | 0.673 | 0.822 / 0.403 (0.820 / 0.396) | 0.836 / 0.316 | 0.471 (0.454) |
| WTA 2021 | 0.5940 (0.5953) | 0.681 | 0.826 / 0.277 (0.822 / 0.266) | 0.848 / 0.202 | 0.391 (0.372) |
| WTA 2022 | 0.5981 (0.5991) | 0.673 | 0.807 / 0.376 (0.800 / 0.366) | 0.838 / 0.259 | 0.392 (0.364) |
| WTA 2023 | 0.5922 (0.5917) | 0.683 | 0.820 / 0.313 (0.816 / 0.302) | 0.844 / 0.235 | 0.378 (0.386) |
| WTA 2024 | 0.5958 (0.5942) | 0.657 | 0.821 / 0.308 (0.825 / 0.313) | 0.863 / 0.224 | 0.419 (0.423) |

Pe ATP v3 este mai bun în toți anii; pe WTA câștigul mic din 2021–2023 **nu se regăsește pe
2024** (log-loss +0.0016, selecție egală), deci pe WTA v3 fără cote nu este demonstrabil mai bun
decât v2. Cu cotele de închidere tennis-data.co.uk și ponderile de producție (0.25, 0.70),
2024: ATP log-loss 0.5844, `select` 0.809 / 0.418, precizie înaltă 0.838 / 0.311; WTA 0.5846,
0.826 / 0.372, 0.847 / 0.267 (piața singură: 0.5824 / 0.5892). În aplicație cotele sunt cele
FlashScore, mai timpurii, deci cifrele reale sunt de așteptat între „fără cote” și acestea.

Testul 2025 (o singură rulare pe versiune, după înghețarea modelului; vezi rezervele de mai sus
despre expunerea lui — 2025 a fost deja expus în v1/v2, deci nu este un test curat):

| grup | model | log-loss | acc | cov@80 | select | select_high |
|---|---|---|---|---|---|---|
| ATP 2025 | v2 | 0.6048 | 0.666 | 0.298 | 0.801 / 0.277 | – |
| ATP 2025 | v3 fără cote | 0.6036 | 0.670 | 0.293 | 0.812 / 0.264 | 0.840 / 0.194 |
| ATP 2025 | v3 + cote prod (aplicația) | 0.5948 | 0.676 | 0.304 | 0.812 / 0.280 | 0.858 / 0.219 |
| WTA 2025 | v2 | 0.6113 | 0.670 | 0.311 | 0.805 / 0.290 | – |
| WTA 2025 | v3 fără cote | 0.6115 | 0.674 | 0.300 | 0.805 / 0.287 | 0.821 / 0.178 |
| WTA 2025 | v3 + cote prod (aplicația) | 0.5940 | 0.677 | 0.396 | 0.811 / 0.354 | 0.830 / 0.239 |

Fără cote, v3 aduce pe 2025 puțin față de v2 (ATP log-loss −0.0012, `select` 0.812 / 0.264 la
acoperire cu 1.3 puncte mai mică; WTA log-loss egal, `select` egal 0.805 / 0.287). Cu cotele de
închidere (rată de potrivire ATP 89%, WTA 95%) log-loss-ul scade la 0.5948 / 0.5940 și `select`
ține 0.81 la acoperire 28% / 35%. Profilul „precizie înaltă” nu atinge ținta de 85% pe 2025
decât pe ATP cu cote (0.858 / 0.219); fără cote dă 0.840 (ATP) și 0.821 (WTA). 2025 a fost mai
greu de prezis; regula rulantă a păstrat precizia peste 80% ridicându-și singură pragul, în timp
ce un prag fix de 0.72 ar fi dat doar 77.8% pe ATP.
