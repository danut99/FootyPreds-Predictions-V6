# Protocolul de evaluare V8

Fișiere: `footypreds/evaluation/protocol.json`, `dataset.py`, `run.py`, `tune.py`,
`baseline_v7.py` (V7 înghețat, doar pentru comparație).

## Date

Arhivele publice [Football-Data](https://www.football-data.co.uk/data.php) pentru
Premier League, La Liga, Bundesliga, Serie A și Ligue 1, sezoanele 2021–22 … 2025–26.
Fișierele brute, checksum-urile SHA256 și manifestul sunt în `footypreds/data/benchmark/`
(excluse din Git). Setul complet este validat înainte de fiecare rulare.

```powershell
.\.venv\Scripts\python.exe -m footypreds.evaluation.dataset       # descărcare (fără RapidAPI)
.\.venv\Scripts\python.exe -m footypreds.evaluation.run --validate # sezonul de validare
.\.venv\Scripts\python.exe -m footypreds.evaluation.tune           # căutare parametri (validare)
.\.venv\Scripts\python.exe -m footypreds.evaluation.run            # testul blocat, o singură dată
```

## Separarea datelor

| Rol | Sezoane | Folosire |
|---|---|---|
| Istoric inițial | 2021–22, 2022–23, 2023–24 | doar ca istoric |
| Validare | 2024–25 (fostul test V7) | alegerea parametrilor |
| Test blocat | 2025–26 | rulat o dată, cu parametrii înghețați |

La validare, sezonul de test nu este nici măcar încărcat. O versiune nouă de model
care schimbă parametrii după ce a văzut testul are nevoie de un sezon de test nou.

## Reguli anti-scurgere

- Fiecare meci evaluat primește un obiect `BlindFixture` fără scor; cotele sunt prezente
  doar în varianta „model + piață”.
- Predicțiile pentru o zi se fac înainte ca vreun rezultat din acea zi să intre în istoric.
- Duplicatele de ID sau de meci și suprapunerile temporale opresc evaluarea.
- Testele din `footypreds/tests/test_evaluation.py` verifică aceste reguli cu spioni
  și date otrăvite.

## Metrici

Principală: log loss 1X2 pe toate meciurile. Secundare: acuratețe și Brier 1X2, log loss
peste/sub 2,5 și GG, acuratețea selecțiilor ≥ 85% cu interval Wilson și bootstrap pe zile,
acoperire, calibrare pe piețe și pe ligi. Comparații: V8 fără cote, V8 + piață,
V7, frecvența ligii și casele de pariuri (medii de piață, momentul capturării nespecificat).
