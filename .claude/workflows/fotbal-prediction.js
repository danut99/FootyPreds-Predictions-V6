export const meta = {
  name: 'fotbal-prediction',
  description: 'Build FotbalPrediction: football-data.co.uk model (1X2, DC, goals, BTTS, AH, score, corners, cards), leak-free benchmark and a site like TenisPrediction',
  whenToUse: 'Build or rebuild the separate football prediction site (port 8020) with its own CSV-trained model, mirroring tenisPrediction.',
  phases: [
    { title: 'Scout', detail: 'data coverage, core API/odds/settlement, reusable engine code' },
    { title: 'Harness', detail: 'data downloader + walk-forward multi-market benchmark' },
    { title: 'Explore', detail: 'parallel candidate models, validation seasons only' },
    { title: 'Verify', detail: 'adversarial leak/overfit audit per candidate' },
    { title: 'Integrate', detail: 'production model, app on port 8020, web UI, docs, one locked test' },
    { title: 'Review', detail: 'leakage/repro, code/tests, UI' },
    { title: 'Fix', detail: 'address review findings' },
  ],
}

// ---------------------------------------------------------------------------------------------
// Shared context
// ---------------------------------------------------------------------------------------------
const REPO = 'E:\\Personal\\money\\acisefacbani\\FootyPreds-Predictions-V6'
const PKG = 'fotbalPrediction'

const CONTEXT = `
Repo: ${REPO} (git). Work on the CURRENT branch; do NOT commit, push, stash, reset or switch branches.
Python: system "python" 3.11 (numpy, scipy, scikit-learn, fastapi, httpx, pytest, ruff, openpyxl installed; no .venv). Use PYTHONIOENCODING=utf-8 when printing (Windows console is cp1252).
Project rules (AGENTS.md): Python 4 spaces, snake_case, ruff line length 100; JS 2 spaces, 'use strict', escape every interpolated value with esc(); the web CSP forbids inline scripts and inline style attributes; user-facing text is Romanian with diacritics; tests deterministic and network-free (httpx.MockTransport, TestClient, tmp_path, tiny synthetic CSVs — never download or train on full data inside unit tests); never print keys or raw provider responses; runtime data lives under footypreds/data/ (git-ignored) — never commit data.

GOAL: a new package ${PKG}/ that mirrors tenisPrediction/ (read it as the reference implementation: benchmark.py, engine.py, model.py, odds.py, calibration.py, train.py, app.py, web/, EXPERIMENTS.md, README.md) but for football:
- a model trained walk-forward on football-data.co.uk CSVs (results, shots, corners, cards, referee where present, and odds),
- markets: 1X2, double chance (1X/X2/12), goals over/under 0.5-4.5 (match), team goals over/under, BTTS (GG/NG), asian handicap (±0.5..±2.5, quarter lines if the model supports them) and most likely exact scores, half-time 1X2/goals if data allows, corners over/under (match + team) and corners handicap, cards over/under (yellow=1, red=2 booking points AND plain card counts — pick what can be settled), and anything else the data supports well (e.g. shots on target totals) — only markets that can be backtested and settled from data may be "selectable",
- a leak-free benchmark and a per-market selection rule targeting >=80% accuracy on selected picks (plus a stricter 85% mode), with honest reporting of coverage,
- a separate FastAPI app on port 8020 with a web UI modelled on tenisPrediction/web (daily board, date strip, finished days show won/lost, filters: status, market type, minimum chance >=75/80/85%, "sigur + cotă", singles/doubles; LIVE tab via the core live API), mounting the existing FootyPreds core app under /core exactly like tenisPrediction/app.py does.

HONESTY: overall 1X2 accuracy is capped near ~50-55% and the market is hard to beat. High accuracy (>=80%) is only reachable on selected picks in "safe" markets (double chance, over 0.5/1.5, under 4.5, heavy favourites) which have short odds. Always report accuracy WITH coverage and the average fair odds of the selected picks; never call a selection profitable without an ROI test against real odds.

PROTOCOL (strict, football seasons as football-data codes, e.g. 2324 = 2023-24):
- history/burn-in: everything up to 2122; TUNE only on 2223 and 2324; CONFIRM once on 2425;
- LOCKED TEST 2526: run exactly once per model version, at the very end, by the integrator only;
- 2627 is the running season (partial) — production only, never for tuning;
- predictions for a match may only use rows with an earlier Date (same-date rows are fed AFTER all same-date predictions); never use the predicted match's goals, shots, corners, cards or closing odds except when an explicit --odds variant is benchmarked (odds are pre-match information, but football-data "closing" columns (PSCH, AvgCH, ...) are closing prices — label them as such).
`

// ---------------------------------------------------------------------------------------------
// Schemas
// ---------------------------------------------------------------------------------------------
const SCOUT_SCHEMA = {
  type: 'object',
  properties: {
    summary: { type: 'string' },
    facts: { type: 'string', description: 'concrete, verified facts with file paths / URLs / column names' },
    recommendations: { type: 'string' },
    risks: { type: 'string' },
  },
  required: ['summary', 'facts', 'recommendations', 'risks'],
}

const HARNESS_SCHEMA = {
  type: 'object',
  properties: {
    summary: { type: 'string' },
    protocol_doc: { type: 'string', description: 'exact Model protocol, ctx fields, market keys and metric definitions other agents must use' },
    cli_examples: { type: 'string' },
    baseline_metrics: { type: 'string', description: 'per market and season: n, log-loss, Brier, accuracy, cov@80, cov@85, select acc/cov, avg fair odds of selected' },
    data_coverage: { type: 'string', description: 'leagues x seasons x columns actually available (corners/cards/referee/closing odds)' },
    runtime_seconds: { type: 'number' },
    tests_passed: { type: 'boolean' },
    files_changed: { type: 'array', items: { type: 'string' } },
  },
  required: ['summary', 'protocol_doc', 'cli_examples', 'baseline_metrics', 'data_coverage', 'tests_passed', 'files_changed'],
}

const MARKET_METRICS = {
  type: 'object',
  properties: {
    log_loss: { type: 'number' },
    accuracy: { type: 'number' },
    cov_at_80: { type: 'number' },
    select_acc: { type: 'number' },
    select_cov: { type: 'number' },
  },
  required: ['log_loss', 'accuracy', 'cov_at_80'],
}

const CANDIDATE_SCHEMA = {
  type: 'object',
  properties: {
    name: { type: 'string' },
    module: { type: 'string' },
    idea: { type: 'string' },
    command: { type: 'string' },
    markets_covered: { type: 'array', items: { type: 'string' } },
    results_2223_2324: { type: 'string', description: 'per market: baseline vs candidate (log-loss, Brier, accuracy, cov@80, cov@85, select acc/cov, avg fair odds of selected)' },
    confirm_2425: { type: 'string', description: 'same metrics on 2425, run once after freezing' },
    x1x2_2425: MARKET_METRICS,
    ou25_2425: MARKET_METRICS,
    beats_baseline: { type: 'boolean' },
    tuning_protocol: { type: 'string' },
    leakage_self_check: { type: 'string' },
    notes: { type: 'string' },
  },
  required: ['name', 'module', 'idea', 'command', 'markets_covered', 'results_2223_2324', 'confirm_2425', 'beats_baseline', 'tuning_protocol', 'leakage_self_check'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    leak_free: { type: 'boolean' },
    metrics_reproduced: { type: 'boolean' },
    protocol_respected: { type: 'boolean' },
    overfit_risk: { type: 'string', enum: ['low', 'medium', 'high'] },
    issues: { type: 'array', items: { type: 'string' } },
    reproduced_numbers: { type: 'string' },
    worth_integrating: { type: 'string' },
  },
  required: ['leak_free', 'metrics_reproduced', 'protocol_respected', 'overfit_risk', 'issues', 'reproduced_numbers', 'worth_integrating'],
}

const FINAL_SCHEMA = {
  type: 'object',
  properties: {
    summary: { type: 'string' },
    files_changed: { type: 'array', items: { type: 'string' } },
    markets: { type: 'string', description: 'every market on the site, whether selectable, how it is settled' },
    validation: { type: 'string', description: '2223/2324 tune + 2425 confirm, per market' },
    locked_test_2526: { type: 'string', description: 'single run, verbatim, per market, with and without odds' },
    selection_rule: { type: 'string' },
    how_to_run: { type: 'string' },
    tests_passed: { type: 'boolean' },
    notes: { type: 'string' },
  },
  required: ['summary', 'files_changed', 'markets', 'validation', 'locked_test_2526', 'selection_rule', 'how_to_run', 'tests_passed'],
}

const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    issues: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string', enum: ['blocker', 'major', 'minor'] },
          file: { type: 'string' },
          description: { type: 'string' },
        },
        required: ['severity', 'description'],
      },
    },
    evidence: { type: 'string' },
  },
  required: ['ok', 'issues', 'evidence'],
}

// ---------------------------------------------------------------------------------------------
// Phase 1 — Scout (read-only, parallel)
// ---------------------------------------------------------------------------------------------
const SCOUTS = [
  { key: 'data', prompt: `Map the football-data.co.uk data. Read footypreds/evaluation/dataset.py and sim_datasets.py (existing downloaders, FD_URL pattern https://www.football-data.co.uk/mmz4281/{season}/{league}.csv, notes on Avg* columns). Fetch https://www.football-data.co.uk/notes.txt and the data pages (data.php, all_new_data.php) and verify: which leagues exist (main: E0-E3, EC, SC0-SC3, D1, D2, I1, I2, SP1, SP2, F1, F2, N1, B1, P1, T1, G1; "new" extra leagues via https://www.football-data.co.uk/new/{CODE}.csv such as ROU, BRA, ARG, USA, JPN, AUT, SWZ, POL, DNK, NOR, SWE, FIN, IRL, MEX, CHN), from which season each column group exists (FTHG/FTAG, HTHG/HTAG, HS/AS, HST/AST, HC/AC corners, HY/AY/HR/AR cards, Referee, B365/PS/Avg/Max 1X2, >2.5/<2.5 odds, AH lines and odds, closing columns *C*). You MAY download a small sample of files into footypreds/data/fotbal_scout/ (git-ignored) to check — keep it light. Report a coverage table (league x first season with corners/cards/odds/closing) and parsing pitfalls (date formats dd/mm/yy vs dd/mm/yyyy, encoding, trailing empty columns, team renames).` },
  { key: 'core', prompt: `Map how the separate tenisPrediction app plugs into the FootyPreds core, so a football twin can do the same. Read tenisPrediction/app.py and tenisPrediction/web/{index.html,app.js,style.css}, footypreds/api.py (/api/predictions, board_item, sports), footypreds/live.py + live_api.py (football live), footypreds/sports/{keys.py,settle.py,odds.py,legs.py}, footypreds/provider.py (FlashScore RapidAPI: which endpoints give odds and which markets — 1X2, O/U, BTTS, AH, corners, cards? — and whether finished-match statistics (corners, yellow/red cards) are available and how many API calls they cost; never call the real API). Report: the exact response shape of /core/api/predictions?sport=football (fields, odds keys, result fields), which market keys already exist and settle from the final score, what would be needed to settle corners/cards (stats endpoint + caching in store.py), and how FlashScore team names / league names look vs football-data.co.uk names (propose a team-name resolver like tenisPrediction's resolve_player, with overrides).` },
  { key: 'engine', prompt: `Map the existing football model to decide what to reuse. Read footypreds/engine/{analyzer.py,ratings.py,markets.py,form.py,history.py,backtest.py}, footypreds/evaluation/{run.py,tune.py,calibration.py,baseline_v7.py,protocol.json}, docs/CONTRACTS.md (football parts) and EXPERIMENTS/benchmark outputs if any. Report: model type (Poisson/Dixon-Coles? rating fit? time decay?), how the score matrix and markets are built (footypreds/engine/markets.py FT_MARKETS etc.), the latest published benchmark numbers (1X2 log-loss/accuracy, O/U 2.5, BTTS) if present, which functions can be imported as-is by ${PKG} (score matrix -> markets, market keys, settlement), and weaknesses a CSV-trained model could fix (more history, shots/xG proxy, corners/cards, league strength across tiers, promoted teams).` },
]

phase('Scout')
const scouts = await parallel(SCOUTS.map(s => () =>
  agent(CONTEXT + '\nYOUR TASK (read-only scout: do NOT edit repo files):\n' + s.prompt, { label: `scout:${s.key}`, phase: 'Scout', schema: SCOUT_SCHEMA })
    .then(r => r && { key: s.key, ...r })))
const scoutReport = scouts.filter(Boolean)
if (scoutReport.length < SCOUTS.length) log(`Atenție: ${SCOUTS.length - scoutReport.length} scout(i) fără rezultat.`)
const SCOUT_INFO = '\nSCOUT FINDINGS:\n' + JSON.stringify(scoutReport, null, 1)

// ---------------------------------------------------------------------------------------------
// Phase 2 — Harness (data + benchmark)
// ---------------------------------------------------------------------------------------------
phase('Harness')
const harness = await agent(CONTEXT + SCOUT_INFO + `
YOUR TASK: build the shared data layer and walk-forward benchmark for ${PKG}.
1. ${PKG}/__init__.py and ${PKG}/data.py: downloader for football-data.co.uk (main leagues all seasons from 0506 or the earliest with corners/cards — decide from the scout findings; plus the "new" extra leagues) into footypreds/data/fotbal/raw/ with a manifest (url, sha256, downloaded_at), polite rate limiting, and --refresh for the running season 2627. Parser -> normalised rows (league, season, date, home, away, FT/HT goals, shots, shots on target, corners, yellows, reds, referee, odds dict with source+closing flags), robust to the pitfalls the scout found; cached parse keyed by file mtimes. CLI: python -m ${PKG}.data --download [--leagues ...] [--refresh].
2. ${PKG}/markets.py: the market catalogue (keys compatible with footypreds.sports.keys where a key already exists), outcome/settlement functions from a finished row for every market (goals, BTTS, DC, AH incl. push/half-win handling for quarter lines, exact score, corners, cards), and helpers to derive market probabilities from a score matrix (reuse footypreds.engine.markets where possible) and from count distributions (corners/cards).
3. ${PKG}/benchmark.py: Model protocol (factory() -> model with predict(ctx) -> {market_key: probability} and update(row); optional select(ctx, market_key, p)), ctx with ONLY pre-match fields (league, season, date, teams, referee if known pre-match — football-data lists it but treat it as pre-match only if it is announced before kickoff; document the choice; odds only when --odds is passed). Chronological feeding by Date with same-date predictions before same-date updates. Metrics per market group and per season: n, log-loss (multi-class for 1X2), Brier, accuracy of the argmax pick, ECE, cov@80/cov@85 (hindsight), transfer_80 (threshold from previous season), select()-rule accuracy/coverage, average fair odds and, when odds exist, flat-stake ROI of selected picks at market-average odds. CLI mirroring tenisPrediction.benchmark (--model, --seasons 2223,2324, --leagues, --markets, --odds none|avg|max|ps|closing, --json, --records); refuse 2526 unless --locked-test.
4. ${PKG}/candidates/{__init__.py,baseline.py}: baseline = a solid independent Poisson/Dixon-Coles with time decay per league (goals markets) + league-average count model for corners/cards; also an adapter that runs the existing footypreds engine (footypreds.engine.analyze) on the same fixtures if feasible, so v8 core is a second reference.
5. Tests in footypreds/tests/test_fotbal_benchmark.py (tiny synthetic CSVs in tmp_path): parsing, ordering, no-leak (predict never sees the row's result), settlement of every market incl. AH quarter lines and pushes, locked-season refusal.
Run the baseline(s) on 2223,2324 (all leagues with data) and report numbers per market.`, { label: 'harness', phase: 'Harness', schema: HARNESS_SCHEMA })
if (!harness) throw new Error('harness agent failed')
log(`Harness gata. ${harness.summary.slice(0, 200)}`)

const HARNESS_INFO = `
SHARED HARNESS (do NOT edit ${PKG}/benchmark.py, data.py, markets.py or candidates/baseline.py; report harness bugs in notes):
${harness.protocol_doc}
CLI: ${harness.cli_examples}
Data coverage: ${harness.data_coverage}
Baseline numbers: ${harness.baseline_metrics}
Write your candidate ONLY in ${PKG}/candidates/<your_name>.py (+ optional <your_name>_tune.py). Other agents work in parallel in the same folder — never touch their files. Tune on 2223,2324; confirm ONCE on 2425; never 2526.
`

// ---------------------------------------------------------------------------------------------
// Phase 3+4 — Explore candidates, each adversarially verified as soon as it finishes
// ---------------------------------------------------------------------------------------------
const DIRECTIONS = [
  { key: 'goals_model', prompt: `Direction: best goals model for the score matrix (drives 1X2, DC, O/U, team totals, BTTS, AH, exact score, HT markets). Try Dixon-Coles with exponential time decay, bivariate/diagonal-inflated Poisson, and a dynamic rating model (e.g. state-space / Elo-like attack & defence updated match by match), with league-specific home advantage, promoted-team priors, cross-league strength (teams moving between divisions), and shots/shots-on-target as an xG proxy (e.g. blend goals with 0.1-0.3*SoT-based expected goals). Fit online or with periodic refits on earlier data only.` },
  { key: 'rating_stack', prompt: `Direction: rating features + ML stack for 1X2 / DC / O/U / BTTS. Online features: several Elo variants (goal-difference-weighted, home/away specific), pi-ratings, rolling form, rolling shots/SoT/corners for and against, rest days, promoted flag, league tier, table position proxies. Fit an ordinal/multinomial logistic model for 1X2 and binary models for O/U lines and BTTS, refit per season on earlier data only (strict time order); try HistGradientBoosting too. Compare to the harness baseline on the same markets and check calibration.` },
  { key: 'corners_cards', prompt: `Direction: corners and cards. Model per-team corners-for/against and cards-for/against with time-decayed Poisson / negative-binomial (over-dispersion matters for cards), home effect, league effects, opponent strength (favourites win more corners; underdogs collect more cards), and referee strictness where Referee is available and known pre-match (document how you justify it as pre-match information; provide a variant without referee). Produce match and team totals O/U, corners handicap, and booking-points / card-count O/U. Report which lines are well calibrated enough to be selectable.` },
  { key: 'odds_blend', prompt: `Direction: market odds as input. Using the harness --odds option (market-average pre-closing Avg*, and separately the closing *C* columns labelled as closing), remove the margin (proportional vs power vs Shin) for 1X2, O/U 2.5 and AH where present, and fit per-market logit blends model+market with weights learned on earlier seasons only (walk-forward). Measure model alone vs market alone vs blend, and flat-stake ROI of selected picks. Recommend a conservative production weight for FlashScore pre-match odds (earlier/noisier than football-data prices) and explain.` },
  { key: 'selector', prompt: `Direction: calibration and the selection policy — this drives "80%+ on selected picks". Per market family, learn calibration maps (Platt / isotonic / beta, symmetrised where relevant) on the rolling out-of-sample stream of earlier predictions, then design select() rules maximising coverage while accuracy >=80% (and a strict 85% mode): per-market and per-league thresholds, minimum team history, avoid early-season rounds, agreement between two signals. Choose rules on 2223 only, report out-of-sample on 2324, then confirm on 2425. Report accuracy, coverage AND average fair odds of selected picks per market (be explicit that safe markets have short odds).` },
]

phase('Explore')
const verified = await pipeline(
  DIRECTIONS,
  d => agent(CONTEXT + SCOUT_INFO + HARNESS_INFO + `
YOUR TASK (candidate name: ${d.key}):
${d.prompt}
Iterate seriously (implement -> benchmark -> analyse errors -> improve), respect the protocol and report honest numbers per market.`, { label: `explore:${d.key}`, phase: 'Explore', schema: CANDIDATE_SCHEMA }),
  (cand, d) => cand && agent(CONTEXT + HARNESS_INFO + `
YOUR TASK: adversarially audit candidate "${d.key}" (module ${cand.module}). Claimed tune results: ${cand.results_2223_2324}. Confirm 2425: ${cand.confirm_2425}. Idea: ${cand.idea}. Tuning: ${cand.tuning_protocol}.
Try hard to REFUTE it: read the code line by line for leakage (the predicted match's goals/shots/corners/cards/closing odds, same-date rows, season aggregates that include later matches, refits including eval-season data, referee used as if known pre-match without justification, thresholds chosen on the reported season), protocol violations (2425 used for tuning, any 2526 run) and overfitting (knobs vs samples, gap between 2223 and 2324). Re-run the exact command (${cand.command}) and check the numbers reproduce. Do not modify the candidate files. Default leak_free=false if you find any real leak.`, { label: `verify:${d.key}`, phase: 'Verify', schema: VERDICT_SCHEMA })
    .then(v => ({ key: d.key, cand, verdict: v })),
)

const results = verified.filter(Boolean).filter(r => r.cand)
for (const r of results) log(`${r.key}: leak_free=${r.verdict?.leak_free} repro=${r.verdict?.metrics_reproduced} overfit=${r.verdict?.overfit_risk}`)
const dropped = DIRECTIONS.map(d => d.key).filter(k => !results.some(r => r.key === k))
if (dropped.length) log(`Candidați fără rezultat: ${dropped.join(', ')}`)

// ---------------------------------------------------------------------------------------------
// Phase 5 — Integrate: model + app + UI + docs + one locked test
// ---------------------------------------------------------------------------------------------
phase('Integrate')
const final = await agent(CONTEXT + SCOUT_INFO + HARNESS_INFO.replace('Write your candidate ONLY', 'Explorers wrote candidates') + `
YOUR TASK: integrator. Candidate reports and adversarial audits:
${JSON.stringify(results.map(r => ({ key: r.key, candidate: r.cand, audit: r.verdict })), null, 1)}

1. Production model ${PKG}/model.py (+ engine.py if useful): combine ONLY ideas audited leak_free=true that reproduce and help on 2223/2324; prefer the simplest combination; re-validate on 2223/2324 and confirm once on 2425. Expose benchmark_factory and a FootballPredictor for the app (predict from FlashScore team/league names with a team-name resolver + overrides file; returns every market with probability, fair odds, pick, selectable flag, decision "selectează"/"fără pariu" and decision_high for the 85% mode). Leagues/teams the model cannot resolve fall back to the core engine's probabilities (clearly flagged) or are shown as "fără model".
2. ${PKG}/train.py: train through the latest data (incl. 2627 partial) and pickle to footypreds/data/fotbalPrediction/ with a cache key = VERSION + params + data file signatures; background training at app startup (never block requests; /api/health says "loading").
3. ${PKG}/app.py: FastAPI on port 8020 mirroring tenisPrediction/app.py (static /assets, /, /api/health, /api/teams, /api/predict, POST /api/fotbal-probabilities for the day's FlashScore matches incl. their odds, core app mounted at /core). Market blend with odds uses the validated weights (odds_blend findings) — decision and displayed probability must refer to the same outcome.
4. ${PKG}/web/{index.html,app.js,style.css}: same look & structure as tenisPrediction/web (reuse its CSS approach; CSP: no inline scripts/styles), Romanian text. Daily board from /core/api/predictions?sport=football, enhanced with /api/fotbal-probabilities; filters: stare meci, format (meciuri / 2 combinate), șansă minimă (toate / ≥75 / ≥80 / ≥85 / sigur + cotă), tip pariu (Toate, 1X2, Șansă dublă, Goluri, Echipă goluri, GG/NG, Handicap, Scor exact, Cornere, Cartonașe); finished days show won/lost per pick; corners/cards are settled only when statistics are available (use the core provider's statistics with caching if the scout found a cheap way; otherwise show them as "nedecontat" and never count them in won/lost); LIVE tab via /core/api/live?sport=football like the tennis LIVE tab. Escape every interpolated value with esc().
5. Tests: footypreds/tests/test_fotbal_model.py and test_fotbal_app.py (tiny injected model, TestClient, no network, fixed dates). Run python -m pytest -q (pre-existing unrelated failures, if any, must be listed and shown to fail on HEAD too), python -m ruff check footypreds ${PKG}, python -m ruff format --check footypreds ${PKG}, node --check ${PKG}/web/app.js.
6. Docs in Romanian: ${PKG}/README.md (how to download data, train, run: python -m uvicorn ${PKG}.app:app --host 127.0.0.1 --port 8020; honest metrics) and ${PKG}/EXPERIMENTS.md (every candidate, audit verdict, what was integrated, per-market tables). Update the root README.md/AGENTS.md project-structure section briefly to mention ${PKG}/.
7. ONLY AT THE VERY END, after the model and thresholds are frozen: run the locked test on 2526 ONCE (python -m ${PKG}.benchmark ... --seasons 2526 --locked-test, without odds and with the production odds variant) and record it verbatim in EXPERIMENTS.md. Change nothing after seeing 2526. Do not commit.`, { label: 'integrator', phase: 'Integrate', schema: FINAL_SCHEMA })
if (!final) throw new Error('integrator failed')
log(`Integrare gata. Test 2526: ${final.locked_test_2526.slice(0, 300)}`)

// ---------------------------------------------------------------------------------------------
// Phase 6 — Review (parallel lenses), then one fix pass
// ---------------------------------------------------------------------------------------------
phase('Review')
const LENSES = [
  { key: 'leak_repro', prompt: `Audit ${PKG} (model, benchmark, data, markets) for look-ahead leakage and protocol violations (2425/2526 used for tuning, same-date ordering, referee/odds timing, cache trained through the test season used in the benchmark), verify settlement correctness of every market (AH quarter lines, pushes, cards/booking points), and independently re-run the validation commands to check: ${final.validation}. Do NOT run 2526 again.` },
  { key: 'code', prompt: `Review the whole diff (git status + git diff + new files) for correctness bugs, team-name resolution and fallbacks, app behaviour with and without odds (displayed probability, pick and decision refer to the same outcome), background training/caching, test determinism, repo rules (Romanian text, ruff clean, no data committed, no secrets). Run python -m pytest -q, ruff check/format --check, node --check ${PKG}/web/app.js.` },
  { key: 'ui', prompt: `Review the web UI in ${PKG}/web against tenisPrediction/web: CSP compliance (no inline scripts/styles), esc() on every interpolated value, filters and markets work as described, finished days show won/lost correctly and never count unsettled corners/cards, LIVE tab, mobile layout. Start the app with a tiny injected model if possible (TestClient or a local uvicorn with the core mocked) — never call the real RapidAPI. If footypreds/scripts/ui_smoke.py can be adapted, run it; otherwise reason from the code.` },
]
const reviews = (await parallel(LENSES.map(l => () => agent(CONTEXT + `
Integrator report: ${JSON.stringify(final)}
YOUR TASK (review lens ${l.key}): ${l.prompt} Do not modify files. Report blocker/major issues only with concrete evidence.`, { label: `review:${l.key}`, phase: 'Review', schema: REVIEW_SCHEMA })))).filter(Boolean)

const issues = reviews.flatMap(r => r.issues.filter(i => i.severity !== 'minor'))
const minors = reviews.flatMap(r => r.issues.filter(i => i.severity === 'minor'))
let fix = null
if (issues.length || minors.length) {
  phase('Fix')
  fix = await agent(CONTEXT + `
YOUR TASK: fix these ${PKG} review findings in the working tree (verify each first; skip wrong ones with a justification):
Blocker/major: ${JSON.stringify(issues, null, 1)}
Minor (fix if cheap): ${JSON.stringify(minors, null, 1)}
Do NOT retune on 2425/2526 and do NOT run 2526 again. If a fix changes model predictions, re-run the 2223/2324 validation and report. Run pytest, ruff and node --check at the end. Do not commit.`, {
    label: 'fixer',
    phase: 'Fix',
    schema: {
      type: 'object',
      properties: {
        summary: { type: 'string' },
        fixed: { type: 'array', items: { type: 'string' } },
        skipped: { type: 'array', items: { type: 'string' } },
        tests_passed: { type: 'boolean' },
      },
      required: ['summary', 'fixed', 'skipped', 'tests_passed'],
    },
  })
}

return {
  scouts: scoutReport.map(s => ({ key: s.key, summary: s.summary })),
  harness: { baseline: harness.baseline_metrics, coverage: harness.data_coverage, tests_passed: harness.tests_passed },
  candidates: results.map(r => ({ key: r.key, confirm_2425: r.cand.confirm_2425, audit: r.verdict })),
  final,
  reviews,
  fix,
}
