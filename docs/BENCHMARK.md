# V7 — evaluare strictă pe date reale

Model: `7.0-poisson-shrinkage`. Generat: 2026-09-21T20:33:14.808824+00:00.

Dataset: 7156 meciuri; istoric inițial: 5404; holdout: 1752.
Perioadă de test: 2024-08-15 — 2025-05-25.

Sursă: [Football-Data](https://www.football-data.co.uk/data.php). Scorurile testate nu sunt prezente în obiectul transmis modelului; toate rezultatele din aceeași zi sunt excluse din istoric.

## Predicții 1X2 — toate meciurile din test

| Model | Meciuri | Acuratețe | Log loss ↓ | Brier/3 ↓ |
|---|---:|---:|---:|---:|
| poisson | 1752 | 52.1% | 0.9934 | 0.1975 |
| league_frequency | 1752 | 42.0% | 1.0786 | 0.2179 |

Referință suplimentară pe același subset cu cote:

| Model | Meciuri | Acuratețe | Log loss ↓ |
|---|---:|---:|---:|
| poisson | 1752 | 52.1% | 0.9934 |
| bookmaker | 1752 | 53.6% | 0.9640 |

## Selecții la prag fix de 85%

**428/477 reușite (89.7%), acoperire 27.2%.**

Wilson 95%: [0.8667790184519615, 0.9214223351746798]. Bootstrap pe zile 95%: [0.8669201520912547, 0.9239373601789709].

Selecțiile provin din piețe diferite; acest procent nu reprezintă acuratețea 1X2.

## Pe ligi

| Ligă | Meciuri | 1X2 | Selecții | Acuratețe selecții |
|---|---:|---:|---:|---:|
| Bundesliga | 306 | 48.4% | 121 | 84.3% |
| La Liga | 380 | 51.8% | 114 | 94.7% |
| Ligue 1 | 306 | 54.6% | 59 | 89.8% |
| Premier League | 380 | 52.4% | 110 | 90.9% |
| Serie A | 380 | 53.2% | 73 | 89.0% |

## Istoric bogat

1596 meciuri au minimum 20 observații pentru fiecare echipă. 1X2: 52.3%; selecții: 89.8%, acoperire 28.1%.

## Limite și reproductibilitate

- Model necalibrat; parametrii și pragul au rămas fixați înainte de test.
- Nu ajusta modelul pe acest holdout după consultarea raportului. O versiune nouă necesită un test ulterior neatins.
- Cotele sunt numai referință externă: ora lor exactă de capturare nu este garantată.
- Bootstrap-ul pe zile tratează corelația din aceeași zi, nu toate dependențele dintre echipe de-a lungul sezonului.
- Rezultatele pe piețe sunt diagnostice multiple, nu motive pentru alegerea retroactivă a celei mai bune piețe.
- SHA256 dataset: `451c3b8e6751cd7b887fbb4eef7a235d39282b622d81d88bcc5e3fd07628ff8d`.
- SHA256 protocol: `4b8ed6f1e6b115a08d932776eb32bca4a11b2670b3cfe2f59baf9653b69698ab`.
- Predicțiile individuale și proveniența fișierelor sunt păstrate în `data/benchmark/`.
