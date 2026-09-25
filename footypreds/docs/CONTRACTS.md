# FootyPreds multi-sport contracts

This file is the definitive contract between the multi-sport core and everything built on it:
recommendations and the ticket generator, live, the simulator and wallet, the basketball and
tennis models, the web UI and the Excel client. It describes the **integrated** code: every
feature router is wired and exercised end to end by `tests/test_e2e_multisport.py`. **Read all
of it before coding.** If code and this document disagree, the tests decide
(`tests/test_sports_*.py`, `tests/test_e2e_multisport.py` and each feature's tests). Then fix
this document in the same change.

## 0. Ground rules

- Do not change football engine `Params` defaults or football 1X2/goals probabilities.
- Feature code lives in **its own module** and exposes an `APIRouter`. It is wired with **one
  line** in `api.py` (see §9). Do not edit `store.py`. A feature that needs tables creates them
  itself with `CREATE TABLE IF NOT EXISTS` through `store.connect()` (see §8).
- Only edit the foundation files `domain.py`, `provider.py`, `store.py`, `competitions.py`,
  `api.py`, `sports/__init__.py`, `sports/settle.py`, `sports/keys.py`, `sports/odds.py` and
  `sports/legs.py` for a bug fix or a strictly additive helper. When you do, add a test and
  update this file.
- Exception: the **internals** of `sports/basketball.py` and `sports/tennis.py` belong to the
  model agents. Their public function and output shape must not change (§5).
- All user-facing text is Romanian with diacritics. Tests are deterministic and network-free:
  use `httpx.MockTransport`, `TestClient` and `tmp_path` databases, and the real payloads in
  `tests/fixtures/flashscore/`. Never call RapidAPI, and never read or print `RAPIDAPI_KEY`.
- Anti-leakage: no result with kickoff at or after `fixture.kickoff - 3h` may reach a
  prediction. Use `sports.common.team_rows`, `HistoryIndex.team(..., cutoff=...)` or
  `HistoryIndex.before(cutoff)`.

## 1. Sports registry: `footypreds/sports/__init__.py`

```python
SPORTS = {"football": {"id": 1, "label": "Fotbal"},
          "basketball": {"id": 3, "label": "Baschet"},
          "tennis": {"id": 2, "label": "Tenis"}}      # id = FlashScore sport_id
SPORT_PATTERN = "^(football|basketball|tennis)$"     # use in Query(pattern=...)
sport_list() -> [{"key", "id", "label"}]             # display order: football, basketball, tennis
analyze_match(fixture, history, threshold=0.85, **kw) -> analysis   # dispatch on fixture.sport
validate_analysis(a) -> a                            # asserts the common shape (§5); use in tests
main_markets(analysis, count=4) -> [{"key","label","probability","fair_odds","odds","ev"}]
headline_tip(analysis) -> market     # the one tip of a board card / export row / Excel row
```

`analyze_match` filters a plain-list `history` to the fixture's sport. A `HistoryIndex` is used
as given, so it **must hold only one sport**. `AnalysisCache` keeps one index per sport. Team
names repeat across sports (for example Real Madrid in football and basketball), so histories
must never mix.

`main_markets` returns the board headline markets:

- football: `1, X, 2, over25`;
- basketball: `1, 2`, the handicap closest to 50%, then the total closest to 50%;
- tennis: `1, 2`, the set total closest to 50%, then the likeliest exact set score.

"Closest to 50%" compares `|p - 0.5|` rounded to 1e-9; on a tie the `over_` / `ah_1_` side wins,
so a line and its complement always give the same headline key.

`headline_tip(analysis)` is the tip used by `BoardItem.tip`, the `.xlsx` export and the Excel
tables:

- football: the likeliest market of the ledger set `engine.markets.SELECTABLE` (so a priced
  extended market such as "Handicap gazde +2.5" at 99% and odds 1.02 is never the headline);
- basketball / tennis: the likeliest selectable market with a real price of at least
  `TIP_MIN_ODDS` (1.08) that cannot be refunded (`can_push`); without one, the likeliest
  selectable market among `main_markets`.

## 2. `Match` (`footypreds/domain.py`, frozen pydantic model)

| field | type | notes |
|---|---|---|
| `id` | str | FlashScore match id, unique across sports |
| `kickoff` | aware datetime | UTC |
| `league`, `country` | str | tennis `league` is e.g. `"ATP - SINGLES: Chengdu (China), hard"`; H2H rows `"Chengdu, hard"`; `country` is `""` for tennis |
| `home`, `away` | str | tennis doubles: `"Peers J./Venus M."` |
| `home_id`, `away_id` | str | FlashScore team ids (doubles: `"id1/id2"`) |
| `status` | str | `scheduled` \| `live` \| `finished` \| `unavailable` |
| `home_goals`, `away_goals` | int\|None | goals (football), points incl. OT (basketball), **sets won** (tennis) |
| `odds` | dict[str, float] | market key → best decimal price (1 < v < 1001) |
| `source` | str | `flashscore` \| `import` \| … |
| `sport` | str | `football` (default, keeps old rows valid) \| `basketball` \| `tennis` |
| `home_participant_id`, `away_participant_id` | str | FlashScore `eventParticipantId`, the keys of `matches/odds` rows |
| `live` | dict | only for `status == "live"`: `{"stage": "2nd Half", "clock": "90+1", "minute": 91\|None, "period": "2H", "red_cards": {"home": 0, "away": 1}}`; `red_cards` is football only |
| `finish_type` | str | `""` \| `aet` \| `penalties` \| `retired` \| `walkover` |
| `home_logo`, `away_logo`, `league_logo` | str\|None | ORIGINAL upstream image URL (team crest / tennis player flag / tournament logo), only `https://static.flashscore.com/...` or `https://flagcdn.com/...` (`domain.image_url`, anything else → None). Never sent to the browser as is: responses show display URLs (§15) |

Score bounds come from `domain.SCORE_LIMITS = {"football": 50, "basketball": 400, "tennis": 5}`.

Period codes (`provider.period_of`): `1H`, `2H`, `HT` (half time), `ET` (football extra time),
`PEN`, `Q1`…`Q4`, `OT` (basketball), `S1`…`S5` (tennis sets), `BREAK`, `INT` (interrupted),
and `""` when unknown.

Never mutate a `Match`; use `match.model_copy(update=...)`.

## 3. Provider (`footypreds/provider.py`, class `FlashScore`)

All methods are `async`. They raise `ProviderError(message, status)`: 503 means no key or bad
key and must propagate; 429 means quota; any other status is an upstream problem. Responses
are cached in the SQLite `cache` table.

| method | returns | cache |
|---|---|---|
| `fixtures(day, sport="football", refresh=False, ttl=None)` | `(matches, cached, rejected)` from `matches/list-by-date?sport_id` | `cache_ttl` or `ttl` |
| `live(sport="football", refresh=False)` | `(live_matches, cached, rejected)`: only `status == "live"`, `Match.live` filled; `home_goals`/`away_goals` = current score | `LIVE_TTL` = 30 s |
| `match_stats(match_id, refresh=False)` | `{period: [{"name","home","away","home_value","away_value"}]}`; periods like `"match"`, `"1st-half"`; `*_value` = first number (`"29%"` → 29.0) | 30 s |
| `head_to_head(match, refresh=False)` | `(rows, cached, rejected)`: finished rows of the match's sport strictly before kickoff (tennis: sets, `RETIRED` → `finish_type="retired"`, `WALKOVER` without score skipped; basketball: points, `AFTER_EXTRA_TIME` → `aet`) | `history_ttl` |
| `standings(match)` | football-style table rows or `[]` (the API skips tennis) | `history_ttl` |
| `match_odds(match, refresh=False)` | `{market_key: {"best": float, "avg": float, "books": int}}` (§4) | `history_ttl` |
| `history(match, start_page=1, pages=1)` | team results (football) | `cache_ttl` |

Pure parsers, easy to test: `normalize_matches(payload, *, results=False, sport="football")`,
`parse_standings`, `parse_stats`, and `sports.odds.parse_odds(payload, sport, home_pid="",
away_pid="", sets=3)`.

Old rows can have negative timestamps. Always use `EPOCH + timedelta(seconds=ts)`.

Football callers keep the historical call form `provider.fixtures(day, refresh=..., ttl=...)`.
Code that may run on test fakes should call `api.fetch_day(provider, day, sport, **kw)`, which
passes `sport=` only when it is not football.

## 4. Market keys, odds and settlement

### 4.1 Keys (`footypreds/sports/keys.py`)

| family | keys | sports | settlement (final score h-a) |
|---|---|---|---|
| result | `1`, `X`, `2` | all (basketball and tennis `1`/`2` = winner incl. OT; `X` is never offered there) | h>a / h=a / h<a |
| total | `over_{line}`, `under_{line}` | all: goals / points / **sets** | vs `h+a`; a whole line equal to the total → push (`None`) |
| team total | `home_over_{line}`, `home_under_{line}`, `away_over_{line}`, `away_under_{line}` | all | vs that side's score; push as above |
| handicap | `ah_1_{signed}`, `ah_2_{signed}` (`ah_1_-1.5`, `ah_2_+4.5`, `ah_1_0`) | all; tennis = **set** handicap | the line is added to that side: `own+line` vs `other`; equal → push. Quarter lines (x.25/x.75) are never mapped and settle `None` |
| draw no bet | `dnb_1`, `dnb_2` | football | draw → void |
| parity | `odd`, `even` | football, basketball | parity of `h+a` |
| exact | `cs_{h}-{a}` (football), `sets_{h}-{a}` (tennis) | | exact score |
| games | `games_over_{line}`, `games_under_{line}` | tennis | **not settleable** (total games is not in a set score): `selectable` must be False |
| football legacy | `engine.markets.FT_MARKETS` keys: `1X`, `X2`, `12`, `over05`…`over45`, `under05`…`under45`, `btts`, `no_btts`, `home_over05`, `home_over15`, `away_over05`, `away_over15`, `home_win_nil`, `away_win_nil`, `btts_over25`, `1_over15`, `2_over15`, `1X_under35`, `X2_under35` | football | `engine.markets.outcome` (unchanged) |
| football display only | `ht_1`, `ht_X`, `ht_2`, `ht_over05`, `ht_over15`, `1/1`…`2/2` (group "Pauză/Final") | football | not settleable (no half-time score) |
| basketball display only | `reg_1`, `reg_X`, `reg_2` (regulation time), `ht_1`, `ht_X`, `ht_2`, `ht_over_{line}`, `ht_under_{line}` (first half) | basketball | not settleable; `selectable` False |
| live only | `next_goal_1`, `next_goal_none`, `next_goal_2` (football), `next_set_1`, `next_set_2` (tennis) | live responses only | not settleable; `selectable` False |

Line text: `keys.fmt_line(2.5) == "2.5"`, `fmt_line(180.0) == "180"`. The signed form is
`keys.fmt_signed(-1.5) == "-1.5"`, `fmt_signed(4.5) == "+4.5"`, `fmt_signed(0) == "0"`.
Builders: `keys.over(line)`, `keys.under(line)`, `keys.handicap(side, line)`, and
`keys.parse(key) -> (family, groups) | None`. `keys.label(sport, key)` gives the Romanian
label, for example "Peste 180.5 puncte", "Handicap gazde -5.5" or "Scor la seturi 2-1".

**Football spelling rule:** when a generic key has a legacy equivalent, football always uses
the legacy key. For example, `over_2.5` is written as `over25` and `home_over_0.5` as
`home_over05` (`keys.FOOTBALL_ALIASES`). Generic football keys are used only for bets with no
legacy key: `over_3`, `over_5.5`, `ah_*`, `dnb_*`, `odd`/`even`, `cs_*`.

### 4.2 Settlement (`footypreds/sports/settle.py`)

```python
settle(sport, key, home_score, away_score, status="finished") -> True | False | None
is_settleable(sport, key) -> bool     # can a final score decide it (possibly as a push)?
can_push(sport, key) -> bool          # can some final score refund it? dnb_*, whole lines
```

`can_push` is True for `dnb_*` and for totals, team totals and handicaps on a whole-number
line (`over_185`, `ah_1_0`, `ah_2_-1`, `home_over_90`). It is False for every football legacy
key and every half line. Recommended tickets, the ticket generator, plans and the simulator
never use a key with `can_push` True. The wallet accepts any `candidate_legs` entry, so a
custom whole-line bet can end void.

- Pass `status = match.finish_type or match.status`.
- `None` means void, push, not final, missing score, or a key that cannot be settled.
- Void statuses: `retired`, `walkover`, `unavailable`, `cancelled`, `postponed`, `abandoned`
  and `awarded`. A tennis retirement or walkover voids every bet.
- Final statuses: `finished`, `aet` and `penalties` (the stored final score is used).
- Any market with `selectable: True` must satisfy `is_settleable(sport, key)`.
- The ledger (`store.settle`) stores `{"won": bool|None, "score": "h-a"}` plus `"void": true`
  when `won` is `None`. `engine.summarize` leaves void rows out of accuracy and Brier score and
  reports them as `metrics["void"]`.

### 4.3 Odds normalization (`footypreds/sports/odds.py`, via `provider.match_odds`)

Only active prices with 1 < v < 1001 count. For each key the result is
`best = max over bookmakers`, `avg = mean` and `books = count`. A key quoted twice by the same
bookmaker is ambiguous and is dropped for that bookmaker.

| sport | scope used | mapped |
|---|---|---|
| football | `FULL_TIME` (+ `FIRST_HALF` for `ht_1/ht_X/ht_2`, `ht_over05`, `ht_over15`) | `HOME_DRAW_AWAY`→`1/X/2`; `DOUBLE_CHANCE` (participant null=`12`, home=`1X`, away=`X2`); `DRAW_NO_BET`→`dnb_*`; `OVER_UNDER`→`overNN`/`underNN` or generic; `BOTH_TEAMS_TO_SCORE`→`btts`/`no_btts`; `ASIAN_HANDICAP`→`ah_*`; `CORRECT_SCORE`→`cs_h-a`; `ODD_OR_EVEN`→`odd`/`even`; `HALF_FULL_TIME`→`1/1`… `EUROPEAN_HANDICAP` is not mapped. |
| basketball | `FULL_TIME_OVER_TIME` (winner incl. OT) | `HOME_AWAY`→`1/2`, `OVER_UNDER`→`over_/under_`, `ASIAN_HANDICAP`→`ah_*`, `ODD_OR_EVEN`. Regulation-time `HOME_DRAW_AWAY FULL_TIME` is **not** mapped. |
| tennis | `FULL_TIME` | `HOME_AWAY`→`1/2`, `CORRECT_SCORE`→`sets_h-a`, `OVER_UNDER` (games)→`games_over_/games_under_`, set handicap `ah_1_±1.5`/`ah_2_±1.5` (below) |

Rules for sides and handicaps:

- **Sides.** `Match.home_participant_id` and `Match.away_participant_id` decide which row is
  home and which is away. When they are missing, the sides are inferred only if the payload has
  exactly two participants; the first participant of the winner group is home.
- **Handicap.** The line is the handicap applied to that participant, verified on the captured
  payloads: football DC and DNB agree with `ah ±0.5` and `ah 0`, and basketball uses opposite
  signs. For each bookmaker, the prices must fall as the line grows, and the two sides of a line
  must form a plausible two-way book (0.98 ≤ Σ1/p ≤ 1.25). If either check fails, that
  bookmaker's handicaps are dropped.
- **Tennis handicap.** Tennis `ASIAN_HANDICAP FULL_TIME` mixes set and game handicaps on the
  same lines. Only the ±1.5 set handicap of a best-of-3 match is kept, and only when the same
  side and line are quoted twice. The set reading is provably the lower price for +1.5 and the
  higher price for -1.5. Every other tennis handicap is skipped. Men's Grand Slam singles are
  best of 5 (`sports.tennis.best_of(match)`), and no set handicap is kept for them.

**Merging into `Match.odds`:** `api.enrich` calls `provider.match_odds`, then
`merge_odds(current, parsed)` = `{**current, **best_prices, **list_by_date_1X2}`. The list-by-date
`1`/`X`/`2` prices stay as they are, because they drive the tuned football market blend. Every
other price is added or refreshed. `store.save_matches` merges odds as a union where new prices
win, so a list-by-date reload never erases those extra prices.

### 4.4 Shared legs and tickets (`footypreds/sports/legs.py`)

```python
leg(match, analysis, market) -> Leg
candidate_legs(match, analysis, now=None) -> [Leg]
    # scheduled, kickoff > now, grade A-C, market selectable, real price (odds > 1)
ticket(legs, target_odds=None, day=None, reason=None) -> Ticket   # [] -> status "unavailable"
settle_leg(leg, match) -> Leg          # unchanged until the match is finished/unavailable
ticket_status(statuses) -> "pending" | "won" | "lost" | "void" | "unavailable"
settled_odds(legs) -> float            # payout multiplier; void legs count as 1.0
```

```jsonc
// Leg
{"match_id": "KnR6QDo1", "sport": "tennis", "kickoff": "2026-09-26T05:00:00+00:00",
 "competition": "Chengdu (China), hard", "competition_id": "tennis:atp - singles|chengdu",
 "home": "Cerundolo J. M.", "away": "Davidovich Fokina A.",
 "key": "2", "label": "Victorie jucătorul 2", "group": "Câștigător",
 "probability": 0.63, "odds": 1.59, "fair_odds": 1.59, "ev": 0.002,
 "grade": "B", "confidence": 61, "status": "pending", "score": null}
// Ticket
{"day": "2026-09-26", "target_odds": 5.0, "total_odds": 5.08, "probability": 0.29,
 "ev": 0.47, "legs": [Leg, ...], "status": "pending", "reason": null}
```

Ticket probability is the product of leg probabilities (an independence approximation), and
`ev = probability * total_odds - 1`. Legs of one ticket must come from **different matches**.
Recommendation legs also carry `"reason"` (Romanian, why this leg). Settled legs have `status`
`won` | `lost` | `void` and `score` `"h-a"`.

## 5. Common analysis shape (every sport)

```jsonc
{
  "version": "basketball-0.1-baseline",
  "sport": "basketball",
  "threshold": 0.85,
  "calibrated": false,
  "grade": "A" | "B" | "C" | "D",
  "confidence": 0-100,
  "quality": "sufficient" (A-C) | "insufficient" (D),
  "expected": {...},        // football {"home","away"} xG; basketball {"home","away","margin_sd","total_sd"};
                            // tennis {"home_win","best_of","set_win","surface"}
  "markets": [{"key","label","group","probability","fair_odds","odds","ev","selectable"}],
                            // unique keys; odds = Match.odds.get(key); ev = probability*odds-1 or null
  "selection": market | null,   // selectable, probability >= threshold, grade A-C; else null
  "reason": "…",
  "tips": [{"category","key","label","probability", "odds"?, "ev"?}],
  "summary": "Romanian sentence",
  "insights": ["Romanian strings"],
  "form": {"home": Form, "away": Form},
  "h2h": {"played", "home_wins", "draws", "away_wins", "matches": [{"id","date","competition","home","away","score"}]},
  "sample": {"home", "away", "h2h", ...},
  "components": {...}       // free-form model internals
}
```

`Form` = `{"sequence": "WWLWD", "last": [{"id","date","competition","venue": "A"|"D",
"opponent","score","result": "W"|"D"|"L"}], "last5", "last10", "days_since_last",
"available"}`. Football keeps its extra keys (`expected_goals`, `scores`, `score_grid`,
`goal_distribution`, `htft`, and the football-specific form fields). Football `h2h` omits the
win counts when `played == 0`.

Sport-specific extras (all additive):

- **Football markets.** Besides `FT_MARKETS`, the analysis always has `dnb_1`/`dnb_2`,
  `ah_1_±1.5`/`ah_2_±1.5` and `odd`/`even`, plus every generic key that has a quoted price
  (Asian handicap half and whole lines, `over_{line}`, team totals, `cs_h-a`). `selectable`
  means "can be a ticket leg": the ledger set `SELECTABLE` is always selectable, every other
  settleable key only with a real price. Markets that can refund carry `"push": p_push`, and
  their `probability` is P(win | not refunded), so `1/probability` is the fair price.
  `selection` (the ledger pick) still comes only from `SELECTABLE`. Extra prices never change
  an existing probability.
- **Basketball `expected`:** `home`, `away` (final-score points incl. overtime), `margin_sd`,
  `total_sd`, `margin`, `total`, `overtime` (P(regulation tie)) and `minutes` (48 for NBA and
  G League, 40 elsewhere). Quoted whole lines report P(win | no push) without a `push` key.
- **Tennis `expected`:** `home_win`, `best_of`, `set_win` (per-set probability), `surface` and
  `games` (expected total games). With prices, the winner probability is the margin-free
  market price (validated market weight 1.0); Elo decides only unpriced matches.
- The basketball (`Params`) and tennis (`TennisParams`) analyzers accept `params=...` for tests
  and tuning. Production calls never pass it.

**Model agents (basketball/tennis):** keep `analyze(fixture, history, threshold=0.85, **_)` in
`sports/basketball.py` and `sports/tennis.py`. `history` is a list or a single-sport
`HistoryIndex`. Keep `VERSION`, `SPORT`, and `tennis.best_of` and `tennis.surface_of` (the
provider uses `best_of`). Every selectable key must be settleable. Every test must still pass
`validate_analysis`. Shared helpers are in `sports/common.py`: `team_rows`, `team_form`,
`head_to_head`, `two_way`, `market`, `confidence_of`, `choose`, `pick` and `value_tip`.

## 6. Competitions (`footypreds/competitions.py`)

- `competition_id(league, country="", sport="football")` and `match_competition(match)`.
  Football ids are **unchanged**, for example `"england|premier league"`. Basketball ids look
  like `"basketball:usa|nba"`. Tennis ids look like `"tennis:atp - singles|chengdu"`, built
  from the category before `:` plus the tournament without `(Country), surface`.
- `POPULAR` (football, unchanged), `POPULAR_BY_SPORT[sport]` and `popular_ids(sport)`.
- `catalog(matches, demo=False, sport="football")` returns
  `[{"id","name","country","count","popular"}]`.
- `priority(match)` is the board sort key:
  - football: unchanged;
  - basketball: popular, youth, women, no odds, minor country;
  - tennis: popular, singles before doubles, then ATP/WTA before Challenger before ITF before
    the rest, then no odds.

## 7. API objects (`app.state`) for feature routers

Feature routers read shared objects from `request.app.state` and never import `create_app`.

| name | what |
|---|---|
| `store` | `Store` (§8) |
| `provider` | `FlashScore` (§3) |
| `cache` | `AnalysisCache`: `get(match, threshold=0.85) -> analysis` (sport-aware, cached by fixture + store version); `history(sport="football") -> HistoryIndex` of that sport only; `snapshot(sport)` |
| `settings` | `Settings` (`api_key` must never be exposed) |
| `enrich` | `async enrich(match, refresh=False) -> (warnings, standings)`: H2H, standings (not tennis) and `match_odds` merged into the stored match. **Re-read the match afterwards:** `store.match(match.id)` |
| `day_fixtures` | `async day_fixtures(day, sport="football", refresh=False) -> (matches, cached, rejected, settled)`: fetches, saves and settles the ledger |
| `ledger_threshold` | 0.85 |
| `excel_*` | legacy aliases for the Excel client (`excel_day_fixtures(day, refresh)` is football only) |
| `live_caches` | created by `live_api` on first use: `{"list": TTLCache (30 s per sport), "stats": TTLCache (60 s per match)}` |
| `sim_benchmark_dir`, `sim_cache_dir`, `sim_workers` | optional overrides read by `sim_api` (tests set `tmp_path` dirs and 1 worker); defaults `data/benchmark`, `data/sim_cache`, CPU count − 1 (max 8) |

Analyses are CPU work. From async endpoints, call them with
`await run_in_threadpool(cache.get, match, threshold)`.

## 8. Store (`footypreds/store.py`)

`matches(sport=None)`, `matches_on(day, sport=None)`, `match(id)`,
`save_matches(matches) -> changed`, `settle(matches) -> n`, `snapshot(match, compact(prediction),
now)`, `predictions()`, `get_cache(key)`, `put_cache(key, payload, ttl)`,
`synced_days(sport="football") -> {"YYYY-MM-DD"}`, `mark_synced(day, count, sport="football")`,
`connect()`, and `version` (bumps on every real write).

`save_matches` keeps a finished row finished. It keeps `home_id`, `away_id`, `country`,
the participant ids and the three logo fields when the new row lacks them, and it merges `odds` as a union.

**Feature tables:** create them lazily in your module:

```python
def ensure_tables(store):
    with store.connect() as db:
        db.executescript("CREATE TABLE IF NOT EXISTS wallet_bets (id TEXT PRIMARY KEY, ...);")
```

Prefix table names with the feature name (`wallet_*`, `sim_*`, `reco_*`). Tables in use:

| table | module | content |
|---|---|---|
| `reco_sets` | `recommend.py` | per (day, sports): singles, analyzed, enriched, warnings, generated_at |
| `reco_tickets` | `recommend.py` | per (day, sports, target): the stored ticket JSON and its status |
| `reco_enriched` | `recommend.py` | match ids already enriched by recommendations (budget memory) |
| `wallet_ledger` | `wallet.py` | deposit / bet / payout / reset rows with the running balance |
| `wallet_bets` | `wallet.py` | bets with locked legs, stake, total odds, status and payout |

The simulator keeps no tables: its prediction and parsed-dataset caches are JSON files under
`data/sim_cache/` (git-ignored).

## 9. Router wiring convention

In your module:

```python
from fastapi import APIRouter, Request
router = APIRouter(prefix="/api", tags=["live"])

@router.get("/live")
async def live(request: Request, sport: Sport = "football"): ...
```

In `api.py`, add **one** line directly under the marker comment
`# Feature routers: ONE line each, here, before the static mounts at "/".`:

```python
    app.include_router(excel_router)  # excel: /api/excel/* flat CSV/TSV tables
    # Feature routers: ONE line each, here, before the static mounts at "/".
    app.include_router(live_router)  # live: /api/live, /api/live/{match_id}
    app.include_router(recommend_router)  # recommendations: /api/recommendations*, tickets
    app.include_router(sim_router)  # simulator: /api/simulate*, /api/wallet*
```

The wallet router is included inside `sim_api.router`. Router imports sit with the other
`footypreds.*` imports in sorted order (ruff `I001`). `test_e2e_multisport.py` checks that every
feature route is registered before the first static mount. Declare a sport
parameter with `Annotated[str, Query(pattern=SPORT_PATTERN)]`, so an unknown sport returns 422.
Error details are Romanian: `HTTPException(status, "…")`. The `ProviderError` handler already
maps provider failures to JSON.

## 10. Endpoints

All examples below are real responses from `tests/test_e2e_multisport.py`'s setup (captured
FlashScore payloads, frozen clock 2026-09-25T20:00Z, day 2026-09-26), trimmed with `…` and
rounded. Money values are floats in the wallet currency (RON). Probabilities are 0–1. Every
error is `{"detail": "<Romanian message>"}`. Validation errors (422) from FastAPI models use the
app's generic Romanian message `"Parametri invalizi. Verifică data, ID-ul și pragul."`. A
provider failure uses the `ProviderError` handler (503 no key or bad key, 429 quota).
Recommendations, tickets, plans and the simulator **never** take a "minimum probability": the
optimizer chooses the likeliest legs for the requested odds.

### 10.1 Core endpoints (`api.py`, sport-aware)

- `GET /api/sports` returns `{"sports": [{"key": "football", "id": 1, "label": "Fotbal"},
  {"key": "basketball", "id": 3, "label": "Baschet"}, {"key": "tennis", "id": 2, "label":
  "Tenis"}]}`.
- `GET /api/health` returns the old keys plus `"sports": [...]` and
  `"history_by_sport": {sport: finished_count}`. `history_matches` counts football only.
- `GET /api/matches?day&sport=football&refresh` returns
  `{"matches","cached","rejected","settled","sport","source"}`.
- `GET /api/predictions?day&sport=football&competition&limit&offset&refresh&demo` returns
  `{"day","sport","total","offset","items":[BoardItem],"competitions","cached","rejected","source"}`.
  The demo is football only; for another sport it returns empty `items`.
  - `BoardItem` = `{"sport","match","competition","competition_id","probabilities":{key:p},
    "main":[main market],"expected","tip":{"key","label","probability"},"tips","grade",
    "confidence","sample","form":{"home","away"},"summary","selection", "result"?:{"score",
    "tip_won": bool|null}}`. `tip` is `headline_tip` (§1); `tip_won` is `null` for a void or
    push.
  - Football also has `expected_goals`, `score`, `scores` and `htft`, and its `probabilities`
    keep the historical 16 keys.

  ```jsonc
  // GET /api/predictions?day=2026-09-26&sport=tennis&limit=2 → items[0] (without match/sample/tips)
  {"sport": "tennis", "competition": "Seoul (South Korea), hard",
   "competition_id": "tennis:wta - singles|seoul",
   "probabilities": {"1": 0.3829, "2": 0.6171, "over_2.5": 0.4006, "sets_0-2": 0.3943},
   "main": [{"key": "1", "label": "Victorie jucătorul 1", "probability": 0.3829,
             "fair_odds": 2.6118, "odds": 2.45, "ev": -0.062}, …],
   "expected": {"home_win": 0.3829, "best_of": 3, "set_win": 0.3859, "surface": "hard",
                "games": 22.6251},
   "tip": {"key": "2", "label": "Victorie jucătorul 2", "probability": 0.6171},
   "grade": "D", "confidence": 20, "form": {"home": "", "away": ""},
   "summary": "Modelul favorizează Ruse G. (62%). Cel mai probabil scor la seturi: 0-2; …",
   "selection": null}
  ```
- `GET /api/competitions?day&sport&refresh&demo` returns `{"competitions","sport","source"}`.
- `POST /api/analyze/{id}?sport=` with body `{"threshold","enrich","refresh"}`, and
  `GET /api/analysis/{id}?sport=&threshold`, return
  `{"match","prediction","saved","retrospective","standings","warnings"}`. The optional `sport`
  must match the stored match, otherwise the response is 404. Enrichment merges `match_odds`
  prices into the stored match (§4.3).
- `POST /api/history/sync` with body `{"days": 1..90, "sports": ["football", ...]}` (default
  `["football"]`) returns 202. `GET /api/history/sync` returns the sync state.
- `GET /api/export.xlsx?day&sport&competition&enriched_only` exports football in the historical
  workbook. Other sports get the sheets `Predicții` and `Piețe`.
- Plans (legacy weekly tickets, `tickets.py`): `POST /api/plans` takes `{"mode": "week" |
  "custom", "start_date", "target_odds": 1.2..100, "max_legs": 1..5, "diverse_leagues",
  "demo", "competitions", "sports": ["football", ...]}`. `min_probability` is still accepted
  (0.35–0.9) but **ignored**; the plan uses the same optimizer as the recommendations
  (`recommend.optimize`, window 0.93–1.12 × target). A plan ticket status is `pending` | `won`
  | `lost` | `void`. `GET /api/plans`, `GET /api/plans/{id}`, `POST /api/plans/{id}/refresh`
  are unchanged.
- `/api/excel/*` (Excel client) stays **football only**: a basketball or tennis id returns 404.

### 10.2 Feature endpoints

#### Recommendations (`recommend.py`, router `recommend_api.py`)

**`GET /api/recommendations?day=YYYY-MM-DD&sports=football,basketball,tennis&targets=2,5,10,100&refresh=false`**

- `sports`: comma list of known sports (default all three, returned in registry order).
  Unknown sport → 422 `"Sport necunoscut."`.
- `targets`: 1–8 comma values, each 1.2–1000 (default `2,5,10,100`), otherwise 422.
- The first call (or `refresh=true`) collects the pool: `day_fixtures` for every sport, the
  stored matches of that UTC day sorted by `competitions.priority`, at most `ANALYSIS_LIMIT`
  (150) analyses per sport, and at most `ENRICH_BUDGET` (8) enrichments per sport per request
  (H2H, standings and `matches/odds` of shortlisted games not yet enriched, best grade first).
  A 429 stops enrichment with a warning; a 503 propagates.
- The result is **stored** (`reco_*` tables). Later calls return the stored set without
  provider calls, except `day_fixtures` for a sport that has a started pending leg (to settle
  it). A target not stored yet is added using the budget left. On `refresh`, a ticket with any
  started or settled leg is **locked** (kept as is), so the track record cannot be rewritten.
- Leg eligibility (`recommend.eligible_legs`): `candidate_legs` (scheduled, kickoff after now,
  grade A–C, selectable, real price) and then `can_push` False, odds in `LEG_ODDS` 1.08–4.0 and
  `probability × odds ≥ MIN_VALUE` 0.95.
- Ticket (`recommend.optimize`, exact branch and bound): maximizes the product of leg
  probabilities with `total_odds` in `ODDS_WINDOW` [0.93, 1.12] × target, at most one leg per
  match and never the same team twice, at most `MAX_LEGS` legs (2 → 3, 5 → 5, 10 → 7,
  100 → 14; other targets interpolated on a log scale; hard limit 15). Ties go to the higher
  total odds, then to fewer legs. Sports are mixed automatically, and the result is
  deterministic for the same stored data.
- `singles`: up to 10 legs, one per match, odds ≥ 1.2, sorted by probability (descending).

```jsonc
{"day": "2026-09-26", "sports": ["football", "basketball", "tennis"], "targets": [2, 5, 10, 100],
 "tickets": [                                       // one per target, same order
  {"day": "2026-09-26", "target_odds": 5.0, "total_odds": 4.9856, "probability": 0.4295,
   "ev": 1.1415, "status": "pending", "reason": null,
   "legs": [
    {"match_id": "KnR6QDo1", "sport": "tennis", "kickoff": "2026-09-26T05:00:00+00:00",
     "competition": "Chengdu (China), hard", "competition_id": "tennis:atp - singles|chengdu",
     "home": "Cerundolo J. M.", "away": "Davidovich Fokina A.",
     "key": "ah_1_+1.5", "label": "Handicap jucătorul 1 +1.5", "group": "Handicap seturi",
     "probability": 0.6259, "odds": 1.52, "fair_odds": 1.5978, "ev": -0.0487,
     "grade": "A", "confidence": 84, "status": "pending", "score": null,
     "reason": "Probabilitate estimată 63%, față de 66% implicit în cota 1.52. Suprafață: hard; meci în cel mult 3 seturi."},
    {"match_id": "fb0", "sport": "football", "key": "1X", "label": "Gazde sau egal",
     "group": "Șansă dublă", "probability": 0.848, "odds": 1.64, …}, …],
   "target": 5.0, "max_legs": 5, "window": [4.65, 5.6],
   "assumption": "Probabilitatea biletului este produsul probabilităților selecțiilor (presupune că meciurile sunt independente).",
   "expected_value": 1.1415,
   "rationale": "3 selecții (fotbal, tenis), alese automat pentru cea mai mare probabilitate combinată la cota țintă 5 (interval acceptat 4.65–5.60). Cotă totală 4.99, probabilitate estimată 43.0%."
   /* "payout_odds": 4.99 once won or void (void legs count 1.0) */},
  …],
 "singles": [Leg + "reason", …],                    // ≤ 10, probability descending
 "analyzed": {"football": 3, "basketball": 18, "tennis": 21},
 "enriched": {"football": 3, "basketball": 8, "tennis": 8},
 "warnings": [], "generated_at": "2026-09-25T20:00:00+00:00",
 "disclaimer": "Estimări statistice, nu garanții. 18+."}
```

An impossible target is still in the list with `"status": "unavailable"`, `"legs": []`,
`total_odds`/`probability`/`ev`/`expected_value` `null` and a Romanian `reason` (the same text
as `rationale`), for example `"Nu există selecții eligibile: e nevoie de meciuri viitoare cu cote
reale și date suficiente (grad A–C). Alege altă zi sau mai multe sporturi."`.

**`GET /api/recommendations/history?sports=football,basketball,tennis&days=60`** (days 1–365)

```jsonc
{"sports": ["football", "basketball", "tennis"],
 "days": [{"day": "2026-09-24", "tickets": [
   {"target": 2.0, "status": "won", "total_odds": 2.05, "probability": 0.71, "legs": 2,
    "payout_odds": 2.05}, …]}],                     // newest first; days ≤ today (UTC)
 "summary": [{"target": 2.0, "tickets": 12, "won": 7, "lost": 4, "void": 0, "pending": 1,
              "unavailable": 0, "profit": 1.9, "hit_rate": 0.636, "roi": 0.173}, …],
 "disclaimer": "Estimări statistice, nu garanții. 18+.",
 "note": "Profit calculat la o miză de 1 unitate pe fiecare bilet decis."}
```

`profit` is per 1 unit on every decided ticket (won → payout_odds − 1, lost → −1, void → 0).
`hit_rate` = won / (won + lost), `roi` = profit / (won + lost + void). Both are `null` without
data.

**`POST /api/tickets/generate`**

Request (unknown fields are refused with 422, so an old `min_probability` fails):

```jsonc
{"day": "2026-09-26", "target_odds": 3,          // 1.2..1000
 "sports": ["football", "basketball", "tennis"],  // optional, default all three, 1..3 items
 "max_legs": null,                                // optional 1..15, null = auto (as above)
 "exclude_match_ids": []}                          // optional, ≤ 300 ids
```

Response 200 (also when impossible: `ticket.status == "unavailable"` with a `reason`):

```jsonc
{"ticket": Ticket,                 // same shape as a recommendation ticket
 "alternatives": [Ticket, …],      // 0..2, each on completely different matches
 "analyzed": {"football": 3, "basketball": 18, "tennis": 21},
 "enriched": {"football": 0, "basketball": 0, "tennis": 0},
 "candidates": 57,                 // eligible legs after exclude_match_ids
 "warnings": [], "disclaimer": "Estimări statistice, nu garanții. 18+."}
```

The generator builds the same pool as the recommendations, but only enriches what is left of the
day's per-sport budget (8, shared with the recommendations through `reco_enriched`), so repeated
generations never spend more than that.

#### Live (`live.py`, router `live_api.py`)

**`GET /api/live?sport=football&refresh=false`** (unknown sport → 422; no key → 503)

```jsonc
{"sport": "football", "cached": false, "provider_cached": false,
 "updated_at": "2026-09-25T17:12:29.851798+00:00", "updated": "…same…",
 "count": 29, "rejected": 0, "ttl": 30,
 "matches": [LiveItem, …],          // ordered by competitions.priority
 "odds_note": "Cotele din lista FlashScore sunt de dinainte de meci (nu există cote live). Afișăm cota corectă: cota minimă la care pariul merită jucat.",
 "disclaimer": "Estimări statistice, nu garanții. 18+."}
```

```jsonc
// LiveItem (football)
{"match": {"id": "jclv1hV8", "kickoff": "2026-09-25T16:00:00Z",
           "league": "EUROPE: UEFA Nations League - League B", "home": "Georgia",
           "away": "Northern Ireland", "status": "live", "home_goals": 0, "away_goals": 0,
           "odds": {"1": 1.77, "X": 3.4, "2": 4.8}, "sport": "football",
           "live": {"stage": "1st Half", "clock": "4", "minute": 4, "period": "1H",
                    "red_cards": {"home": 0, "away": 0}}, …},
 "sport": "football", "competition": "UEFA Nations League - League B",
 "competition_id": "europe|uefa nations league - league b",
 "minute": 4, "period": "1H", "stage": "1st Half", "clock": "4",
 "score": {"home": 0, "away": 0},
 "markets": [{"key": "1", "label": "Victorie gazde", "group": "Rezultat final",
              "probability": 0.5365, "fair_odds": 1.864, "odds": null, "ev": null,
              "selectable": true, "reliable": true,
              "why": "Georgia câștigă. Scor 0-0, minutul 4; mai estimăm 1.56 goluri pentru Georgia și 0.87 pentru Northern Ireland până la final."}, …],
 "probabilities": {"1": 0.5365, "X": 0.2542, "2": 0.2093, …},
 "suggestions": [{"key": "1X", "label": "Gazde sau egal", "group": "Șansă dublă",
                  "probability": 0.79, "fair_odds": 1.265, "min_odds": 1.27,
                  "kind": "sigur", "why": "Opțiune sigură: 79%. Merită doar la o cotă de cel puțin 1.27. …"}],
 "summary": "Georgia - Northern Ireland, scor 0-0. Georgia are 54% șanse să câștige.",
 "pre_match": {"source": "odds", "expected": {"home": 1.6269, "away": 0.906},
               "odds": {"1": 1.77, "X": 3.4, "2": 4.8}, "grade": null},
 "notes": ["Cotele din lista FlashScore sunt de dinainte de meci …"],
 "model": {"remaining": {"home": 1.5646, "away": 0.8713, "share": 0.9617},
           "adjustments": {"red_cards": {"home": 1.0, "away": 1.0},
                           "momentum": {"home": 1.0, "away": 1.0}, "observed": null}},
 "version": "live-0.1"}
```

- `markets` are in-play probabilities of **final-result** markets, with the same keys as
  pre-match (settled on the final score), plus the live-only `next_goal_*` / `next_set_*`
  (`selectable` False). `odds` and `ev` are always `null`: FlashScore list odds are pre-match,
  so the UI shows `fair_odds` and `min_odds`, never a live price. Markets already decided are
  dropped. In extra time or penalties the list is empty and a note explains why.
- `reliable` False (no pre-match information: football first half, basketball before half
  time, tennis always) means the market is displayed but never suggested.
- `suggestions`: at most `MAX_SUGGESTIONS` (3), selectable and reliable only, one per group,
  never a near-certain market (fair price under 1.03); `kind` is
  `"sigur"` (probability 0.60–0.97) or `"echilibrat"` (0.40–0.72); `min_odds` = fair odds
  rounded up.
- `pre_match.source`: `"analysis"` (stored pre-match analysis with grade A–C, at most
  `ANALYSIS_BUDGET` 40 per list request), `"odds"` (margin-free list odds) or `"default"`.
- Football markets: `1 X 2`, `1X X2 12`, `dnb_1 dnb_2`, over/under at the current total + 0.5,
  + 1.5 and + 2.5 (legacy keys such as `over25`; generic `over_5.5` above 4.5), `btts`/`no_btts`,
  `home_win_nil`/`away_win_nil`, `next_goal_*`. Basketball: `1 2`, `ah_*`, `over_`/`under_`
  (totals are never suggested when the pace is uncertain). Tennis: `1 2`, `sets_h-a`, set total,
  `ah_1_±1.5` / `ah_1_±2.5` set handicaps, `next_set_*`.
- The feed has no minute for basketball/tennis and no in-set game score for tennis; the
  item's `notes` say how the model placed the game.

**`GET /api/live/{match_id}?sport=football&refresh=false`** returns a `LiveItem` plus
`"stats": {period: [{"name","home","away","home_value","away_value"}]}` (`{}` when not
available; a stats failure becomes the first note), `updated_at`, `updated` and
`disclaimer`. From minute 15, live xG (or shots on target) nudges the remaining football goal
rates within 0.8–1.25×. It returns 404 `"Meciul nu este live acum."` when the game is not live
in that sport.

```jsonc
"stats": {"match": [{"name": "Ball possession", "home": "29%", "away": "71%",
                     "home_value": 29.0, "away_value": 71.0}, …],
          "1st-half": […]}
```

Caches: list 30 s per sport (`app.state.live_caches["list"]`) on top of the provider's own
30 s cache; stats 60 s per match. `refresh=true` bypasses both.

#### Simulator (`simulator.py`, `evaluation/sim_datasets.py`, router `sim_api.py`)

**`GET /api/simulate/datasets`**

```jsonc
{"datasets": [
  {"id": "football", "sport": "football", "label": "Fotbal – 5 ligi de top (2021–2026)",
   "matches": 240, "bettable": 240, "start": "2024-06-30", "end": "2025-05-03",
   "source": "football-data.co.uk (cote medii de piață)", "available": true,
   "hint": "python -m footypreds.evaluation.dataset"},
  {"id": "tennis", "sport": "tennis", "label": "Tenis ATP/WTA", "matches": 0, "bettable": 0,
   "start": null, "end": null, "source": "tennis-data.co.uk", "available": false,
   "hint": "Arhivele tennis-data.co.uk nu sunt descărcate. python -m footypreds.evaluation.tennis_eval --download"},
  …],
 "disclaimer": "Simulare cu bani virtuali. 18+."}
```

Dataset ids, always listed in this order (`sim_datasets.DATASET_IDS`):

| id | sport | source |
|---|---|---|
| `football` | football | `data/benchmark/` (football-data.co.uk, 5 leagues; `python -m footypreds.evaluation.dataset`) |
| `football-plus` | football | benchmark + 11 more leagues in `data/benchmark/sim/raw` (`python -m footypreds.evaluation.sim_datasets --download`) |
| `tennis` | tennis | `data/benchmark/tennis/raw` (tennis-data.co.uk ATP+WTA 2013–2026; `python -m footypreds.evaluation.tennis_eval --download`) |
| `local-football`, `local-basketball`, `local-tennis` | that sport | finished matches in the app's store; only those with saved pre-match prices are bettable |

`hint` never contains an OS error or a local path. `bettable` counts matches with prices.
`available` is False when nothing can be bet.

**`POST /api/simulate`**

```jsonc
{"dataset": "football",          // a dataset id; default "football"
 "sport": null,                  // optional; alone it picks that sport's default dataset
                                 // (football → football, tennis → tennis,
                                 //  basketball → local-basketball); must match the dataset
 "bankroll": 1000,               // 0 < x ≤ 10_000_000
 // Contract form: strategy = staking method, target_odds switches to one daily ticket
 "strategy": "flat",             // flat | percent | kelly   (or long form: singles | ticket | value)
 "staking": null,                // long form: flat | percent | kelly (default flat)
 "mode": null,                   // singles | ticket | value (overrides the strategy's mode)
 "stake": 10,                    // flat: amount (0 < x ≤ bankroll; default 1% of bankroll)
                                 // percent: 0.001–0.2 (default 0.01); kelly: 0.1–1 (default 0.25)
 "kelly_fraction": null, "kelly_cap": null,   // kelly aliases; cap default 0.1 of bankroll
 "target_odds": null,            // 1.2..100, required for mode "ticket"
 "start": null, "end": null,     // default: the last 365 days of the dataset; max 3 years
 "max_bets_per_day": 3,          // 1..20 (alias picks_per_day); singles and value only
 "seed": null}                   // echoed back; the simulation is deterministic
```

- Modes: `singles` (the K safest legs of the day, one per match, odds ≥ 1.2), `ticket` (one
  daily ticket near `target_odds`, same optimizer and window as the recommendations) and
  `value` (singles with EV ≥ 0.02 and probability ≥ 0.35). Legs follow the recommendation rules
  (`rules.source == "recommend"`): grade A–C, odds 1.08–4.0, `probability × odds ≥ 0.95`,
  `can_push` False.
- **Blind rule:** walk-forward by day. For day D, the model sees only results of days before
  D (and the analyzers' own kickoff − 3h cutoff). The fixture it receives is rebuilt from
  pre-match fields (teams, kickoff, competition, prices): no score, status, finish type or live
  data. Stakes are fixed first, and only then are that day's results revealed and settled at
  the historical price. Void (tennis retirement, push) refunds the stake. The run stops when the
  bankroll is spent.
- Errors: unknown dataset → 422; dataset not available → 404 with the Romanian reason and hint;
  sport mismatch or an invalid value (`SimulationError`) → 422 with a Romanian message.
- A first run on a dataset computes predictions in a process pool and caches them under
  `data/sim_cache/`; `cache` reports `units`, `computed` and `seconds`.

```jsonc
{"dataset": {…same as the datasets entry…}, "sport": "football",
 "start": "2024-08-03", "end": "2025-05-03", "mode": "ticket", "strategy": "flat",
 "staking": "flat", "stake": 10.0, "target_odds": 2.0, "max_bets_per_day": 1,
 "initial": 1000.0, "final": 967.47, "profit": -32.53, "staked": 350.0, "roi": -0.0929,
 "yield": -0.0929, "growth": -0.0325, "bets": 35, "won": 16, "lost": 19, "void": 0,
 "hit_rate": 0.4571, "max_drawdown": 0.0785, "peak": 1009.06, "longest_losing_streak": 5,
 "avg_odds": 2.0032, "betting_days": 35, "days": 40, "stopped": null,
 "summary": {"start": 1000.0, "final": 967.47, "profit": -32.53, "roi": -0.0929,
             "yield": -0.0929, "growth": -0.0325, "bets": 35, "won": 16, "lost": 19,
             "void": 0, "hit_rate": 0.4571, "max_drawdown": 0.0785,
             "longest_losing_streak": 5, "avg_odds": 2.0032},
 "history": [{"date": "2024-08-03", "bankroll": 990.0}, …],   // equity curve
 "equity": […same as history…],
 "rows": [{"date": "2024-08-03", "stake": 10.0, "odds": 2.09, "probability": 0.4985,
           "result": "lost", "payout": 0.0, "return": 0.0,
           "bankroll_before": 1000.0, "bankroll_after": 990.0, "bankroll": 990.0,
           "match_id": "fd-T1-2024-08-03-Gamma-Delta", "home": "Gamma", "away": "Delta",
           "competition": "Test League", "key": "under25", "label": "Sub 2.5 goluri",
           "legs": [Leg with "status": "lost", "score": "2-3"]}, …],   // last 500
 "rows_total": 35,
 "baseline": {"label": "Favoritul casei de pariuri (aceeași miză)", "initial": 1000.0,
              "final": 943.03, "profit": -56.97, "staked": 200.0, "roi": -0.2848, "bets": 20,
              "won": 7, "lost": 13, "void": 0, "hit_rate": 0.35, "max_drawdown": 0.066,
              "peak": 1009.66, "longest_losing_streak": 5, "avg_odds": 2.0228,
              "growth": -0.057, "betting_days": 20, "stopped": null, "staking": "flat",
              "rows_total": 20},
 "method": "Walk-forward orb: pentru fiecare zi, modelul vede doar rezultatele din zilele anterioare; …",
 "rules": {"source": "recommend", "leg_odds": [1.08, 4.0], "min_value": 0.95,
           "single_min_odds": 1.2, "window": [0.93, 1.12], "max_legs": 3},
 "warnings": ["Cotele istorice sunt medii de piață; la o casă reală prețul obținut putea fi altul.", …],
 "warning": "…the warnings joined in one sentence…",
 "disclaimer": "Simulare cu bani virtuali pe meciuri din trecut. Estimări statistice, nu garanții. 18+.",
 "cache": {"units": 1, "computed": 1, "seconds": 0.24}, "seed": null}
```

For a ticket row, the top-level `match_id`/`home`/`key`/… fields describe the first leg; use
`legs`. `baseline` bets the bookmaker favourite with the same staking and bet count.

#### Wallet (`wallet.py`, routed through `sim_api.py`)

Paper trading on upcoming matches. Bets settle automatically on every wallet read from stored
results (`settle_leg` and `settled_odds`): won pays `stake × settled odds` (void legs count
1.0), an all-void bet refunds the stake.

- `GET /api/wallet` returns the wallet (below).
- `POST /api/wallet/deposit` `{"amount": 0 < x ≤ 1_000_000}` returns the wallet (422 otherwise).
- `POST /api/wallet/reset` clears bets and balance, and returns the empty wallet.
- `POST /api/wallet/bet` takes either custom legs or the day's AI ticket:
  - `{"stake": 10, "legs": [{"match_id": "fb0", "key": "ah_1_+1.5"}], "label": "…"?}` with 1..20
    legs, at most one per match. Each leg must be a current `candidate_legs` entry (scheduled,
    not started, grade A–C, real price); the price is locked from `Match.odds` at that moment.
  - `{"stake": 20, "day": "2026-09-26", "target": 2, "sports": [...]?}` bets the stored AI
    ticket for that target (`recommend.recommendations`); label
    `"Bilet AI cota 2 (2026-09-26)"`.
  - Errors: 422 invalid stake or target, or neither form; 404 unknown match; 400 leg no longer
    bettable, two legs on one match, no AI ticket (its Romanian reason), or insufficient balance
    (`"Sold insuficient în portofelul virtual: ai 70.00 RON, miza este 1000.00 RON."`).

```jsonc
{"currency": "RON", "balance": 70.0, "deposited": 100.0, "staked_open": 30.0, "profit": 0.0,
 "open": 2, "won": 0, "lost": 0, "void": 0,
 "bets": [{"id": "84a2956d1fd3", "created": "2026-09-25T20:00:00+00:00",
           "label": "Bilet AI cota 2 (2026-09-26)", "stake": 20.0, "total_odds": 2.0,
           "legs": [Leg, …], "status": "pending", "payout": null, "settled": null,
           "source": "ai"},                    // "custom" | "ai"
          …],                                  // newest first
 "history": [{"at": "2026-09-25T20:00:00+00:00", "type": "bet", "amount": -20.0,
              "balance": 70.0, "bet_id": "84a2956d1fd3"}, …],   // deposit | bet | payout | reset; newest first, ≤ 500
 "notice": "Portofel virtual pentru simulare: bani fictivi, nu se pariază bani reali. Estimări statistice, nu garanții. 18+.",
 "disclaimer": "…same as notice…"}
```

`profit` = Σ(payout − stake) over closed bets. A bet status is `pending` | `won` | `lost` |
`void`.

## 11. Web UI notes

- The CSP forbids inline scripts and inline `style` attributes. Use classes in CSS files.
- Images: `img-src 'self' data:` only. Use the `home_logo` / `away_logo` / `league_logo`
  display URLs of the responses (§15) as `<img src>`; when a value is `null` or the image
  answers 404, show an initials badge.
- Escape every interpolated value with `esc()`.
- Sports come from `/api/sports`. Card data is `BoardItem.main` plus `tip`, `grade` and `form`.
  Legs and tickets are the shapes in §4.4 and §10.2.
- Home page: `GET /api/recommendations?day=…` (tickets x2/x5/x10/x100 + singles); a
  "Regenerează" button adds `&refresh=true`. Easy ticket: only the target odds (and optionally
  sports) → `POST /api/tickets/generate`. There is **no** minimum-probability input anywhere.
- Show `warnings[]` (simulator, recommendations) prominently, and render ticket/leg/bet status
  `void` as "anulat" and `unavailable` with its `reason`.
- Live: never present `fair_odds`/`min_odds` as bookmaker prices; show `notes` and `odds_note`.
- Romanian with diacritics everywhere, including a visible 18+ and "nu garanții" disclaimer on
  recommendation, ticket, live, simulator and wallet pages (every response has `disclaimer`).
- The current SPA (`web/app.js`) still has the old pages; the plan form no longer sends
  `min_probability`, and void results show "anulat".

## 12. Tests you can reuse

- `tests/test_e2e_multisport.py`: the whole app end to end (every sport and feature) with a
  `Fake` FlashScore serving the captured payloads, frozen clocks (`recommend.utcnow`,
  `wallet.utcnow`) and a `tmp_path` simulator benchmark. Start here for UI or Excel contract
  tests.
- `tests/test_sports_provider.py` shows payload loading, `run_provider` and MockTransport
  patterns.
- `tests/test_sports_api.py` has a `Fake` FlashScore that serves every sport from the fixture
  files through `TestClient`.
- `tests/test_sports_analysis.py` has the synthetic basketball/tennis histories and the
  anti-leakage test pattern.
- `tests/test_sports_settle.py` and `tests/test_sports_legs.py` cover settlement tables and
  tickets; `tests/test_integration_fixes.py` covers `can_push`, `headline_tip` and the
  `main_markets` tie-break.
- Captured payloads live in `tests/fixtures/flashscore/`. In `odds_*.json`, the home and away
  participant ids are: football `QsL3TXzh`/`CbJBRB54`, basketball `tM1zGJSk`/`EZarEcc2`, and
  tennis `j1EyxKUg`/`bRHqzba6`. `list_tennis` match `KnR6QDo1` and `list_basketball` match
  `KMHepeEM` map to those odds and H2H files.

- `tests/test_mock_server.py` boots `scripts/mock_server.py`'s app through `TestClient`
  (`mock.build_app(tmp_path, seed, today, now)`), with every sport, live, images and simulator
  datasets offline. `tests/test_media*.py` cover logos and the image proxy.

## 13. Commands (datasets and caches; never RapidAPI)

```powershell
.\.venv\Scripts\python.exe -m footypreds.evaluation.dataset                 # football benchmark
.\.venv\Scripts\python.exe -m footypreds.evaluation.sim_datasets --download # 11 extra leagues
.\.venv\Scripts\python.exe -m footypreds.evaluation.tennis_eval --download  # tennis workbooks
.\.venv\Scripts\python.exe -m footypreds.simulator --dataset football --warm # prediction cache
.\.venv\Scripts\python.exe -m footypreds.simulator --dataset football --start 2024-08-01 --end 2025-06-30 --mode ticket --target 2 --stake 10
```

Offline demo (no key, no network, temporary database; the real database is never opened):

```powershell
.\.venv\Scripts\python.exe footypreds/scripts/mock_server.py --port 8765 [--seed 7] [--today 2026-09-26]
```

It prints `FootyPreds mock: http://127.0.0.1:8765 …` when ready; Ctrl+C stops it and deletes
its temporary folder. A busy port is a one-line error (exit code 1). Today's games start after
"now", live games are in play, past days are finished with results and 1X2 prices (history
sync and the simulator's `recent` days), and `/api/simulate` has synthetic `football` and
`football-plus` datasets ending yesterday.

A script that calls `simulator.simulate()` needs an `if __name__ == "__main__":` guard, because
the prediction step uses a process pool on Windows.

## 14. Known limitations (measured, not hidden)

- The simulator shows the AI tickets and singles **losing money** on every football dataset
  tested, and doing worse than simply betting the bookmaker favourite. The "safest" football
  legs are mostly over/under 2.5, where the model is overconfident (2025-26, 16 leagues: under
  2.5 at 0.63 predicted vs 0.52 observed). Fixing it needs a model change (blend totals with
  market prices or calibrate on the validation season), not a UI change.
- Tennis winner probabilities equal the margin-free market price when prices exist; the model
  has no edge on the winner market (validation ROI of "value" bets −32%).
- The basketball market weight (0.85) is a conservative default: there is no basketball odds
  dataset to tune it on.
- Live odds are not available from FlashScore lists; live markets show fair odds only.
- Recommendations use the UTC day (`store.matches_on`): for Romania (UTC+3) a game at 00:30
  local time belongs to the previous UTC day.
- Ticket probability assumes independent legs.
- The Excel client and the demo are football only.

## 15. Media: logos, flags and the image proxy (`footypreds/media.py`)

- **Parsing** (`provider.normalize_matches`): `home_logo`/`away_logo` from the team object's
  `small_image_path`, then `smaill_image_path` (typo of the live feed), then `image_path`
  (H2H rows); tennis doubles use the first player's flag; `league_logo` is the tournament
  group's `image_path` (nested groups inherit it). A refused URL falls back to the next field
  and never rejects the fixture.
- **Display URLs**: `media.display_url(url) = "/api/img?u=" + quote(url, safe="")` or `None`.
  `media.public_match(match)` is `match.model_dump(mode="json")` with the three fields replaced
  by display URLs; `media.match_media(match)` returns only the three fields. Every response
  that serializes a match or a leg carries them:
  - `/api/matches` `matches[]`, `/api/predictions` `items[].match` (and the same three keys at
    the item's top level), `/api/analysis/{id}` and `/api/analyze/{id}` `match`, `/api/demo`;
  - `/api/live` items (`match` and the item's top level) and `/api/live/{id}`;
  - every leg built by `sports.legs.leg` (recommendation tickets and singles, the ticket
    generator, wallet bets). `recommend_api` fills legs stored before this change from the
    stored match (`media.with_leg_media(payload, store)`); other features can call it too.
- **`GET /api/img?u=<upstream url>`**: 200 with the image bytes, `Content-Type` sniffed from the
  bytes (`image/png|jpeg|gif|webp`) and `Cache-Control: public, max-age=604800`; otherwise 404
  `{"detail": "Imaginea nu este disponibilă."}`. Rules: `u` must pass `domain.image_url`
  (https, host exactly `static.flashscore.com` or `flagcdn.com`, no userinfo/port/query/
  fragment, no dot segments, ≤ 300 chars); redirects are followed only to URLs passing the
  same check (≤ 3 hops); upstream must answer 200 with a declared AND sniffed raster image type
  (SVG refused), ≤ 512 KB, within 10 s. Images are cached on disk in
  `<database folder>/img_cache/<sha256(url)>.img` (`footypreds/data/img_cache/` by default,
  30 days); failures are remembered for 10 minutes. The usual security middleware applies
  (cross-site requests → 403).
- **app.state**: `img_transport` (httpx transport for the proxy; `create_app(settings,
  transport, image_transport)`), `img_cache_dir`, `img_proxy` (created on first use). Tests
  and the mock server never reach the image hosts.
- `footypreds.api:app` is created lazily (module `__getattr__`), so importing `footypreds.api`
  no longer opens the real database or reads `.env`.
