"""Reference adapter: the FootyPreds V8 core engine (``footypreds.engine.analyze``).

Runs the core analyzer on the same fixtures, exactly like ``footypreds.evaluation.run``:
one ``HistoryIndex`` per league, league ratings refitted once per date with
``fit_history`` at the horizon "day start - cutoff_hours", then ``analyze`` on a blind fixture
(no score; prices only when the benchmark passes ``ctx.odds``, and then only 1X2 and
over/under 2.5, the prices the core was built for).

The core only prices full-time goal markets (plus its fixed-share half-time display markets
ht_1/ht_X/ht_2/ht_over05/ht_over15, which fotbalPrediction CAN settle from HTHG/HTAG). It
has no corners, cards or shots model, so those keys are simply absent.

Caveat: the V8 ``Params`` defaults were tuned on 2425 under the old protocol (the CONFIRM
season here) and the goals calibration was motivated after seeing 2526. This adapter is a
reference row only, never a tuning target.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from fotbalPrediction import markets as mk

CORE_ODDS = ("1", "X", "2", "over25", "under25")


class V8CoreModel:
    def __init__(self, threshold: float = 0.80, threshold_high: float = 0.85, **params):
        from footypreds.engine.analyzer import with_params

        self.params = with_params(**params)
        self.threshold = threshold
        self.threshold_high = threshold_high
        self.history: dict = {}
        self.ratings: dict = {}
        self.fit_day: dict = {}
        self.grade: dict[str, str] = {}

    def _fixture(self, ctx):
        from footypreds.domain import Match

        kickoff = datetime.combine(ctx.date, time.min, timezone.utc)
        odds = {}
        if ctx.odds:
            odds = {k: float(v) for k, v in ctx.odds.items() if k in CORE_ODDS}
        return Match(
            id=ctx.match_id[:120],
            kickoff=kickoff,
            league=ctx.league,
            home=ctx.home[:120],
            away=ctx.away[:120],
            status="scheduled",
            odds=odds,
            source="football-data.co.uk",
        )

    def predict(self, ctx) -> dict[str, float]:
        from footypreds.engine.analyzer import analyze, fit_history
        from footypreds.engine.history import HistoryIndex

        index = self.history.setdefault(ctx.league, HistoryIndex())
        start = datetime.combine(ctx.date, time.min, timezone.utc)
        if self.fit_day.get(ctx.league) != ctx.date:
            horizon = start - timedelta(hours=self.params.cutoff_hours)
            oldest = start - timedelta(days=self.params.max_days)
            past = index.before(horizon, oldest)
            self.ratings[ctx.league] = fit_history(
                past, start, self.params, init=self.ratings.get(ctx.league)
            )
            self.fit_day[ctx.league] = ctx.date
        analysis = analyze(
            self._fixture(ctx), index, 0.85, params=self.params, ratings=self.ratings[ctx.league]
        )
        self.grade[ctx.match_id] = analysis["grade"]
        return {
            m["key"]: min(1.0, max(0.0, float(m["probability"])))
            for m in analysis["markets"]
            if m["key"] in mk.CATALOGUE
        }

    def select(self, ctx, key: str, p: float) -> bool:
        return p >= self.threshold and self.grade.get(ctx.match_id, "D") != "D"

    def select_high(self, ctx, key: str, p: float) -> bool:
        return p >= self.threshold_high and self.grade.get(ctx.match_id, "D") != "D"

    def update(self, row) -> None:
        from footypreds.domain import Match

        self.grade.pop(row.id, None)
        index = self.history.get(row.league)
        if index is None:
            from footypreds.engine.history import HistoryIndex

            index = self.history[row.league] = HistoryIndex()
        index.extend(
            [
                Match(
                    id=row.id[:120],
                    kickoff=datetime.combine(row.date, time.min, timezone.utc),
                    league=row.league,
                    home=row.home[:120],
                    away=row.away[:120],
                    status="finished",
                    home_goals=row.home_goals,
                    away_goals=row.away_goals,
                    source="football-data.co.uk",
                )
            ]
        )


def factory(**params) -> V8CoreModel:
    return V8CoreModel(**params)
