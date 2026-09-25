"""FootyPreds prediction engine: ratings, form, H2H, market blend and derived markets."""

from footypreds.engine.analyzer import PARAMS, VERSION, Params, analyze, fit_history, with_params
from footypreds.engine.backtest import backtest, summarize, walk_forward, wilson
from footypreds.engine.history import HistoryIndex, canonical
from footypreds.engine.markets import LABELS, SELECTABLE, outcome, score_matrix

__all__ = [
    "LABELS",
    "PARAMS",
    "SELECTABLE",
    "VERSION",
    "HistoryIndex",
    "Params",
    "analyze",
    "backtest",
    "canonical",
    "fit_history",
    "outcome",
    "score_matrix",
    "summarize",
    "walk_forward",
    "wilson",
    "with_params",
]
