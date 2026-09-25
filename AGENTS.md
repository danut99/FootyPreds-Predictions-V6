# Repository Guidelines

## Project Structure & Module Organization

FootyPreds V8 is a local prediction app for football, basketball and tennis. Everything lives in the single folder `footypreds/`:

- `engine/`: football prediction engine (score matrix and markets, ratings fit, history index, form/H2H, analyzer, walk-forward backtest).
- `sports/`: multi-sport core: registry and `analyze_match` dispatch, market keys, universal settlement, odds normalization, shared legs/tickets, and the basketball and tennis analyzers. The binding contract for every feature is `docs/CONTRACTS.md`.
- `api.py` (FastAPI app factory) and `excel_api.py` (flat CSV/TSV tables for Excel).
- `provider.py` (FlashScore RapidAPI client), `store.py` (SQLite), `tickets.py`, `competitions.py`, `excel.py` (.xlsx export), `cli.py`.
- Feature modules, each with its own router wired by one line in `api.py`: `recommend.py` + `recommend_api.py` (daily AI tickets x2/x5/x10/x100, safest singles, one-click ticket generator), `live.py` + `live_api.py` (in-play probabilities), `simulator.py` + `sim_api.py` (blind walk-forward bankroll simulator) and `wallet.py` (virtual wallet, routed through `sim_api.py`).
- `web/`: vanilla HTML/CSS/JS SPA. The CSP forbids inline scripts and inline style attributes.
- `excel_client/`: VBA module and Power Query client for the same API.
- `evaluation/`: reproducible benchmark with a locked test season.
- `tests/` (pytest), `scripts/`, `docs/`.
- `data/`: runtime SQLite and benchmark files. It is git-ignored; never commit it.

Root files: `README.md`, `start.ps1`, `pyproject.toml`, `requirements*.txt`, `.env.example`, `.github/workflows/tests.yml`.

## Build, Test, and Development Commands

Windows, Python 3.11+, virtualenv in `.venv`:

- `.\start.ps1`: install requirements and run the server on http://127.0.0.1:8000.
- `.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`: install dev tools.
- `.\.venv\Scripts\python.exe -m pytest -q`: run the full suite. It is network-free.
- `.\.venv\Scripts\python.exe -m ruff check footypreds` and `-m ruff format --check footypreds`: lint and format check.
- `node --check footypreds/web/app.js`: check frontend syntax.
- `footypreds/scripts/ui_smoke.py`: browser smoke test against a running server.
- `python -m footypreds.evaluation.run --validate`: evaluate the validation season. The locked test (`run` without flags) is run once per model version.
- `python -m footypreds.evaluation.tennis_eval --download` / `--validate`: fetch tennis-data.co.uk workbooks / evaluate the tennis model (the 2025 test is locked per VERSION).
- `python -m footypreds.evaluation.sim_datasets --download`: fetch the extra football-data.co.uk leagues for the simulator.
- `python -m footypreds.simulator --dataset football|football-plus|tennis --warm`: precompute the simulator prediction cache (`data/sim_cache/`). Any script that calls `simulate()` needs an `if __name__ == "__main__":` guard (process pool on Windows).

## Coding Style & Naming Conventions

- Python: 4 spaces, `snake_case`, `UPPER_CASE` constants, ruff with line length 100.
- JavaScript: 2 spaces and `'use strict'`. Escape every interpolated value with `esc()`.
- VBA in `excel_client/`: `Option Explicit`, late binding only, ASCII-only source.
- User-facing text is Romanian with diacritics.
- Do not change engine `Params` defaults without re-running tuning on the validation season.

## Testing Guidelines

- Add tests with every behaviour change, and a regression test for every bug fix.
- Tests must be deterministic and must not call RapidAPI. Use `httpx.MockTransport`, `TestClient` and `tmp_path` databases.
- Keep the anti-leakage tests: no result at or after kickoff − 3h may reach a prediction.

## Commit & Pull Request Guidelines

- Use short imperative subjects, e.g. `Add Excel TSV endpoints`.
- PRs describe the behaviour change, affected modules, validation performed and model/benchmark impact.
- Include screenshots for UI changes.

## Security & Configuration

- Keep `RAPIDAPI_KEY` only in `.env`, which is git-ignored.
- Never print keys or raw provider responses.
- The server is for localhost only.
- Legacy VBA files with historical keys were moved out of the repo to `..\FootyPreds-legacy-backup\`. Those keys must be revoked.
