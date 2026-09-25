# Migrare

## V6 (Excel/VBA) → V7/V8 (Python)

Versiunile Excel/VBA vechi (`V6 predictions.xlsm`, `FootyPreds.rar`, textele VBA V2–V6,
exemplele de endpoint-uri) nu mai sunt în repo. Pe 25 septembrie 2026 au fost **mutate,
nu șterse**, în `..\FootyPreds-legacy-backup\legacy\` (lângă folderul proiectului).
Arhiva și workbook-ul există și în istoricul Git (commit `d2c7a7b`).

Atenție: fișierele vechi conțin chei RapidAPI în clar. Cheile expuse trebuie revocate
în RapidAPI; mutarea fișierelor nu le invalidează și nu curăță istoricul Git.

## V7 → V8

| V7 | V8 |
|---|---|
| `app/`, `web/`, `evaluation/`, `tests/`, `scripts/`, `docs/` la rădăcină | totul în `footypreds/` |
| `python -m uvicorn app.main:app` | `python -m uvicorn footypreds.api:app` (sau `start.ps1`) |
| `python -m app.cli …` | `python -m footypreds.cli …` |
| `data/footypreds.sqlite3` | `footypreds/data/footypreds.sqlite3` (`DATABASE_PATH` relativ la `footypreds/`) |
| model doar pe aceeași competiție, refuz sub 8 meciuri | toate competițiile, notă A–D, fără refuz |
| `app/model.py` | `footypreds/engine/` (+ `evaluation/baseline_v7.py` pentru comparație) |
| Ticket Lab / Match center | Predicții zilnice, pagină de meci, Bilete, Track record, Metodologie |
| export Excel inexistent | `/api/export.xlsx` și clientul Excel `footypreds/excel_client/` |

Baza SQLite existentă a fost mutată în noul folder; tabelele noi (`synced_days`) se creează
automat. Selecțiile V7 deja salvate rămân în jurnal.
