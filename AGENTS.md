# Repository Guidelines

## Project Structure & Module Organization

This is a Windows Excel/VBA football prediction project. Tracked deliverables are `V6 predictions.xlsm`, the `FootyPreds.rar` distribution archive, and `README.md`.

The currently untracked extraction at `FootyPreds/FootyPreds - inbunatit/` contains VBA text modules, historical workbooks, endpoint examples, JSON statistics, and a screenshot. The V6 source is `FLASHSCORE_PREDICTOR_PRO_V6.txt`; older versions and the live predictor are separate implementations. There are no dedicated source, test, or asset directories. Add intended source changes explicitly; avoid committing the entire extraction accidentally.

## Build, Test, and Development Commands

There is no CLI build, package manager, or automated test command. Use desktop Excel on Windows with macros enabled for a trusted working copy.

- Open `V6 predictions.xlsm`, press `Alt+F11`, and edit the existing module or paste the V6 text into a standard module. Avoid duplicate procedure definitions.
- Run **Debug > Compile VBAProject** to check compilation.
- Run `Setup` through `Alt+F8` to create the dashboard.
- Run `LoadMatchesByDate`, `AnalyzeMatch`, and `PredictAllMatches` to exercise the main workflow.
- Save macro-bearing workbooks as `.xlsm`; `.xlsx` does not preserve VBA.

## Coding Style & Naming Conventions

Follow the existing VBA style: `Option Explicit`, four-space indentation, PascalCase procedures, camelCase local variables, `m_` module fields, and uppercase underscore-separated constants such as `LOCAL_DATA_PATH`. Declare types explicitly and keep helper procedures private. Preserve worksheet names referenced by code. No formatter or linter is configured; review formatting manually.

## Testing Guidelines

No automated framework or coverage threshold is configured. Validate changes on a workbook copy. Compile, load a known match date, analyze one match, and generate all predictions. Inspect `DATA`, `ANALYSIS`, `PREDICTIONS`, `SCORES`, and `VALUE`; exercise `InitResultsSheet` for tracking changes. Check missing statistics and empty match lists. Record the Excel version, input date, and observed results in the PR.

## Commit & Pull Request Guidelines

History uses short descriptive subjects such as `Revise README for FlashScore Predictor Pro V6`; no formal commit convention is established. Use imperative, specific subjects. PRs should explain changed behavior, affected modules/workbooks, manual validation, and related issues when applicable. Include screenshots for dashboard changes and readable VBA changes alongside binary workbook updates.

## Security & Configuration

Keep RapidAPI keys and personal data paths local. Remove credentials from source, workbooks, and API examples before committing. Configure `LOCAL_DATA_PATH` for optional JSON enrichment; preserve filenames expected by the loader.
