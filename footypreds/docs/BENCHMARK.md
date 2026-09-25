# Benchmark 8.1-calibrated-goals — sezonul 2526

Generat: 2026-09-25T17:53:46.044194+00:00. Meciuri evaluate: 1752 (2025-08-15 — 2026-05-24).

Sursă: [Football-Data](https://www.football-data.co.uk/data.php), 5 ligi de top. Scorul meciului evaluat nu există în obiectul transmis modelului; rezultatele din aceeași zi sunt invizibile. Parametrii au fost aleși numai pe sezonul de validare 2425.

Calibrarea golurilor (hartă Platt, pondere peste/sub 2.5, plafonul de valoare al recomandărilor) a fost potrivită numai pe predicțiile sezonului de validare 2425 din 16 ligi (`python -m footypreds.evaluation.tune --totals`). Problema totalurilor (8.0 prea încrezător la peste/sub 2.5) a fost observată întâi pe sezonul de test 2025-26: rezultatul de mai jos confirmă o corecție motivată de test, cu parametri potriviți numai pe validare.

## 1X2 — toate meciurile

| Model | Meciuri | Acuratețe | Log loss ↓ | Brier/3 ↓ |
|---|---:|---:|---:|---:|
| V8 model (fără cote) | 1752 | 51.5% | 0.9944 | 0.1977 |
| V8 model + piață (1X2, peste/sub 2.5) | 1752 | 52.9% | 0.9791 | 0.1943 |
| V7 (vechi) | 1752 | 51.2% | 1.0062 | 0.2003 |
| Frecvența ligii | 1752 | 44.0% | 1.0717 | 0.2160 |

Același subset, cu cotele caselor:

| Model | Acuratețe | Log loss ↓ |
|---|---:|---:|
| V8 model (fără cote) | 51.5% | 0.9944 |
| V8 model + piață (1X2, peste/sub 2.5) | 52.9% | 0.9791 |
| V7 (vechi) | 51.2% | 1.0062 |
| Case de pariuri | 53.4% | 0.9784 |

## Goluri

| Model | Peste 2.5: acuratețe | Peste 2.5: log loss ↓ | GG: acuratețe | GG: log loss ↓ |
|---|---:|---:|---:|---:|
| V8 model (fără cote) | 56.1% | 0.6844 | 55.3% | 0.6875 |
| V8 model + piață (1X2, peste/sub 2.5) | 57.8% | 0.6761 | 56.1% | 0.6833 |
| V7 (vechi) | 54.7% | 0.6926 | 53.6% | 0.6935 |

Peste/sub 2.5 față de case (1752 meciuri): model 56.1% / log loss 0.6844; model+piață 57.8% / 0.6761; case 57.8% / 0.6761.

## Calibrarea piețelor de goluri

Fără cote, P(peste 2.5) trece prin harta Platt potrivită pe sezonul de validare; cu cotă peste/sub 2.5, probabilitatea este cea a pieței fără marjă (pondere validată 1). Ambele rate de goluri se rescalează ca toată matricea de scoruri să fie de acord; 1X2 nu se schimbă.

| Piață | Variantă | Prezis (medie) | Observat | Log loss ↓ | ECE ↓ |
|---|---|---:|---:|---:|---:|
| Peste 1.5 goluri | V8 model (fără cote) | 77.5% | 76.5% | 0.5397 | 0.0113 |
| Peste 1.5 goluri | V8 model + piață (1X2, peste/sub 2.5) | 77.1% | 76.5% | 0.5329 | 0.0083 |
| Peste 2.5 goluri | V8 model (fără cote) | 52.3% | 53.0% | 0.6844 | 0.0146 |
| Peste 2.5 goluri | V8 model + piață (1X2, peste/sub 2.5) | 52.4% | 53.0% | 0.6761 | 0.0174 |
| Peste 3.5 goluri | V8 model (fără cote) | 30.6% | 29.7% | 0.5982 | 0.0147 |
| Peste 3.5 goluri | V8 model + piață (1X2, peste/sub 2.5) | 30.5% | 29.7% | 0.5912 | 0.0097 |
| Ambele marchează | V8 model (fără cote) | 54.1% | 53.9% | 0.6875 | 0.0071 |
| Ambele marchează | V8 model + piață (1X2, peste/sub 2.5) | 53.3% | 53.9% | 0.6833 | 0.0088 |

Peste 2.5 pe benzi de probabilitate (1752 meciuri cu cote peste/sub; prezis / observat):

| Bandă | V8 fără cote | V8 + piață | Case de pariuri |
|---|---|---|---|
| 20%–30% | 2: 29.0% / 0.0% | 5: 29.2% / 20.0% | 5: 29.2% / 20.0% |
| 30%–40% | 89: 37.0% / 46.1% | 122: 37.2% / 44.3% | 120: 37.2% / 43.3% |
| 40%–50% | 580: 45.7% / 46.2% | 600: 45.5% / 44.5% | 593: 45.4% / 44.5% |
| 50%–60% | 808: 54.8% / 55.4% | 668: 54.5% / 56.0% | 676: 54.5% / 55.9% |
| 60%–70% | 248: 63.4% / 60.9% | 295: 63.9% / 62.7% | 296: 63.9% / 62.8% |
| 70%–80% | 24: 73.2% / 83.3% | 60: 73.5% / 76.7% | 60: 73.5% / 76.7% |
| 80%–90% | 1: 80.5% / 100.0% | 2: 82.2% / 100.0% | 2: 82.2% / 100.0% |

## Selecții la prag fix 85%

| Model | Selecții | Reușite | Acuratețe | Acoperire | Wilson 95% |
|---|---:|---:|---:|---:|---|
| V8 model (fără cote) | 331 | 293 | 88.5% | 18.9% | 84.6%–91.5% |
| V8 model + piață (1X2, peste/sub 2.5) | 401 | 367 | 91.5% | 22.9% | 88.4%–93.9% |
| V7 (vechi) | 343 | 280 | 81.6% | 19.6% | 77.2%–85.4% |

Selecțiile provin din piețe diferite (șansă dublă, goluri, GG); procentul nu este acuratețea 1X2 și nu este rezultatul unor bilete combinate.

## Pe ligi (V8 model + piață)

| Ligă | Meciuri | 1X2 V8 | 1X2 V7 | Selecții | Acuratețe selecții |
|---|---:|---:|---:|---:|---:|
| Bundesliga | 306 | 54.9% | 55.6% | 94 | 87.2% |
| La Liga | 380 | 53.7% | 52.6% | 89 | 96.6% |
| Ligue 1 | 306 | 53.3% | 49.3% | 65 | 89.2% |
| Premier League | 380 | 49.2% | 48.2% | 66 | 95.5% |
| Serie A | 380 | 53.9% | 50.8% | 87 | 89.7% |

## Parametri

```json
{
  "half_life": 540.0,
  "prior": 8.0,
  "max_days": 730,
  "rho": -0.12,
  "form_window": 6,
  "form_prior": 3.0,
  "form_weight": 0.15,
  "h2h_prior": 6.0,
  "h2h_weight": 0.1,
  "market_weight": 0.9,
  "totals_intercept": 0.0556,
  "totals_slope": 0.7241,
  "totals_market_weight": 1.0,
  "friendly_weight": 0.5,
  "cutoff_hours": 3.0,
  "max_fit_matches": 6000
}
```

## Limite

- 1X2 nu este calibrat separat (amestec cu piața); piețele de goluri sunt calibrate pe sezonul de validare (docs/MODEL.md). Tabelele complete sunt în `report.json`.
- Cotele de referință sunt medii de piață; momentul exact al capturării nu este garantat.
- Bootstrap-ul pe zile tratează corelația din aceeași zi, nu toate dependențele.
- Nicio evaluare istorică nu garantează rezultate viitoare.
- SHA256 dataset: `81a491f058fa117167e8a58c8ce536ae4a5237e057834f37e52e4c909345f23a`; protocol: `87a3f76c80b142f94e6487b5098bccca0c24981be3ceb990b5f10e6c86a8008a`.
