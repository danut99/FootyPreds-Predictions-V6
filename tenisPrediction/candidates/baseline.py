"""Adaptor pentru modelul actual `CompactTennisModel` (referința benchmark-ului).

Modelul nu este antrenat în avans: învață online prin `update(row)`, exact ca `fit_files`
(inclusiv walkover-urile, pe care `fit_files` le trimite și el). Singura diferență de plumbing
este cheia jucătorului: se folosește `ctx.first_key` (id TML, stabil) în locul numelui, iar
potrivirea aproximativă a numelor (gândită pentru FlashScore) este ocolită, pentru că aici
cheile sunt deja exacte.
"""

from __future__ import annotations

from tenisPrediction.model import CompactTennisModel, _key, _number, serve_record


class BaselineModel:
    def __init__(
        self,
        k_factor: float = 28.0,
        stats_scale: float = 0.0,
        threshold: float = 0.72,
        min_history: int = 5,
        skip_walkovers: bool = False,
    ):
        self.model = CompactTennisModel(k_factor=k_factor, stats_scale=stats_scale)
        # Cheile sunt id-uri exacte: identitatea (după normalizare) înlocuiește căutarea fuzzy.
        self.model.resolve_player = _key
        self.threshold = threshold
        self.min_history = min_history
        self.skip_walkovers = skip_walkovers

    def predict(self, ctx) -> float:
        return self.model.probability(
            ctx.first_key,
            ctx.second_key,
            ctx.surface or "Hard",
            _number(ctx.first_rank),
            _number(ctx.second_rank),
        )

    def select(self, ctx, p: float) -> bool:
        played = self.model.played
        history = min(played.get(_key(ctx.first_key), 0), played.get(_key(ctx.second_key), 0))
        return max(p, 1 - p) >= self.threshold and history >= self.min_history

    def update(self, row) -> None:
        if self.skip_walkovers and row["is_walkover"]:
            return
        self.model.update(
            row["winner_key"],
            row["loser_key"],
            row["surface"] or "Hard",
            row["date"],
            _number(row["winner_rank"]),
            _number(row["loser_rank"]),
            serve_record(row, "w"),
            serve_record(row, "l"),
        )


def factory(**params) -> BaselineModel:
    return BaselineModel(**params)
