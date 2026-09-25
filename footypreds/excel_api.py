"""Flat CSV/TSV tables for the Excel client (VBA macros in excel_client/ and Power Query).

Every endpoint under /api/excel returns ONE table: a single header row with the stable
column names below, then data rows. Conventions (the VBA module and the Power Query guide
rely on them, and footypreds/tests/test_excel_client.py enforces them):

* ``?format=csv`` (default) or ``?format=tsv``; UTF-8, ``charset=utf-8`` in Content-Type.
  CSV starts with a UTF-8 BOM (Excel opens it with the right encoding) and is RFC 4180
  quoted; TSV never contains tabs, CR or LF inside values (they become spaces).
* Numbers always use ``.`` as decimal separator and never an exponent; probabilities are
  0..1 floats; booleans are ``1``/``0``; a missing value is an empty cell.
* Errors are a one-row table with the columns ``error`` and ``status`` AND the matching
  HTTP status code, so VBA can show the message and Power Query fails loudly.
* /predictions sends ``X-Total-Count``: the fixtures left after the filters, before
  ``offset``/``limit``, so the client can say when a day was truncated.
* Demo fixtures (``demo-next-N`` from the demo board) are never stored; /match and
  /analyze rebuild them from the seeded demo data instead of answering 404. The demo is
  football only: another sport with ``demo=1`` gives a header-only table.
* ``sport`` (football | basketball | tennis, default football) selects the sport of
  /predictions, /competitions, /match, /analyze and /value. Football keeps its historical
  columns; basketball and tennis tables have their own columns (``prediction_columns``). A
  match of another sport than ``sport`` is a 404, and football-only sections (scores, grid,
  htft) are a 422 for the other sports.
* The product tables (/recommendations, /live, /simulate*, /wallet) flatten the JSON API of
  the web app (docs/CONTRACTS.md §10.2): they call it in-process and never change its rules.
  Logo columns (``home_logo``, ``away_logo``, ``league_logo``) are absolute URLs of the local
  image proxy (``/api/img?u=...``); Excel shows them as text.
"""

import csv
import io
import json
import logging
import math
import time
from datetime import date, datetime, timezone
from typing import Annotated

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from footypreds.competitions import catalog, competition_name, match_competition, priority
from footypreds.demo import demo_data
from footypreds.domain import Match
from footypreds.engine import VERSION, HistoryIndex, analyze, canonical, outcome, summarize
from footypreds.engine.backtest import compact
from footypreds.media import DISPLAY_PREFIX, display_url
from footypreds.provider import ProviderError
from footypreds.sports import SPORT_PATTERN, SPORTS, headline_tip, main_markets
from footypreds.sports.settle import settle

logger = logging.getLogger(__name__)

EXCEL_API_VERSION = "2"
FORMATS = {
    "csv": "text/csv; charset=utf-8",
    "tsv": "text/tab-separated-values; charset=utf-8",
}
BOM = "﻿"
GRADES = "ABCD"
# Fallback only: create_app exposes the web ledger threshold as app.state.excel_ledger_threshold.
LEDGER_THRESHOLD = 0.85
# Characters that would break a TSV line or a Power Query row.
BREAKS = str.maketrans({c: " " for c in "\t\r\n\x0b\x0c\x1c\x1d\x1e\x85  "})
# Text that Excel would evaluate as a formula when a CSV is opened directly.
FORMULA_START = ("=", "+", "-", "@")

ERROR_COLUMNS = ("error", "status")
HEALTH_COLUMNS = (
    "status",
    "excel_api",
    "version",
    "api_configured",
    "history_matches",
    "synced_days",
    "server_time_utc",
    "server_time_local",
    "utc_offset_minutes",
    "sports",
)
COMPETITION_COLUMNS = ("competition_id", "competition", "country", "matches", "popular")
# One row per fixture on the daily board.
PREDICTION_COLUMNS = (
    "match_id",
    "source",
    "date_utc",
    "time_utc",
    "kickoff_utc",
    "date_local",
    "time_local",
    "country",
    "competition",
    "competition_id",
    "home",
    "away",
    "status",
    "upcoming",
    "grade",
    "confidence",
    "p_1",
    "p_x",
    "p_2",
    "p_1x",
    "p_x2",
    "p_12",
    "p_over15",
    "p_under15",
    "p_over25",
    "p_under25",
    "p_over35",
    "p_under35",
    "p_btts",
    "p_no_btts",
    "p_ht_1",
    "p_ht_x",
    "p_ht_2",
    "xg_home",
    "xg_away",
    "score_1",
    "p_score_1",
    "score_2",
    "p_score_2",
    "score_3",
    "p_score_3",
    "htft_1",
    "htft_label_1",
    "p_htft_1",
    "tip_key",
    "tip_label",
    "tip_p",
    "selection_key",
    "selection_label",
    "selection_p",
    "odds_1",
    "odds_x",
    "odds_2",
    "fair_1",
    "fair_x",
    "fair_2",
    "value_key",
    "value_label",
    "value_odds",
    "value_ev",
    "form_home",
    "form_away",
    "ppg_home",
    "ppg_away",
    "win_rate_home",
    "win_rate_away",
    "sample_home",
    "sample_away",
    "sample_h2h",
    "result",
    "tip_won",
    "selection_won",
    "summary",
    "sport",
    "home_logo",
    "away_logo",
    "league_logo",
)
# /match/{id}?section=summary: the board row plus the details of the match page.
SUMMARY_COLUMNS = PREDICTION_COLUMNS + (
    "p_over05",
    "p_over45",
    "p_home_over05",
    "p_away_over05",
    "p_home_over15",
    "p_away_over15",
    "p_ht_over05",
    "p_ht_over15",
    "days_since_home",
    "days_since_away",
    "h2h_played",
    "h2h_home_wins",
    "h2h_draws",
    "h2h_away_wins",
    "h2h_goals_avg",
    "h2h_over25",
    "h2h_btts",
    "retrospective",
    "threshold",
    "reason",
    "version",
)
# POST /analyze/{id}: the summary after the FlashScore enrichment.
ANALYZE_COLUMNS = SUMMARY_COLUMNS + ("saved", "warnings")
MARKET_COLUMNS = (
    "key",
    "label",
    "group",
    "probability",
    "fair_odds",
    "odds",
    "ev",
    "selectable",
    "is_tip",
    "is_selection",
)
SCORE_COLUMNS = ("rank", "score", "home_goals", "away_goals", "probability", "fair_odds")
GRID_SIZE = 6
GRID_COLUMNS = ("home_goals",) + tuple(f"away_{n}" for n in range(GRID_SIZE))
HTFT_COLUMNS = ("rank", "key", "label", "probability", "fair_odds")
FORM_COLUMNS = (
    "side",
    "team",
    "n",
    "date",
    "competition",
    "venue",
    "opponent",
    "score",
    "goals_for",
    "goals_against",
    "result",
)
FORMSTATS_COLUMNS = (
    "side",
    "team",
    "window",
    "played",
    "wins",
    "draws",
    "losses",
    "points_per_game",
    "win_rate",
    "scored_avg",
    "conceded_avg",
    "over15",
    "over25",
    "btts",
    "clean_sheets",
    "failed_to_score",
    "sequence",
    "streak_wins",
    "streak_unbeaten",
    "streak_winless",
    "days_since_last",
    "matches_last_30_days",
)
H2H_COLUMNS = (
    "date",
    "competition",
    "home",
    "away",
    "score",
    "home_goals",
    "away_goals",
    "result",
)
INSIGHT_COLUMNS = ("n", "text")
STANDING_COLUMNS = (
    "position",
    "team",
    "played",
    "wins",
    "draws",
    "losses",
    "scored",
    "conceded",
    "goal_diff",
    "points",
    "role",
)
SECTIONS = {
    "summary": SUMMARY_COLUMNS,
    "markets": MARKET_COLUMNS,
    "scores": SCORE_COLUMNS,
    "grid": GRID_COLUMNS,
    "htft": HTFT_COLUMNS,
    "form": FORM_COLUMNS,
    "formstats": FORMSTATS_COLUMNS,
    "h2h": H2H_COLUMNS,
    "insights": INSIGHT_COLUMNS,
    "standings": STANDING_COLUMNS,
}
RECORD_COLUMNS = (
    "match_id",
    "created_utc",
    "date_utc",
    "time_utc",
    "competition",
    "home",
    "away",
    "selection_key",
    "selection_label",
    "probability",
    "fair_odds",
    "grade",
    "confidence",
    "status",
    "score",
    "won",
    "sport",
)
METRIC_COLUMNS = (
    "total_matches",
    "selected",
    "settled",
    "wins",
    "pending",
    "coverage",
    "accuracy",
    "ci_low",
    "ci_high",
    "brier",
    "target_supported",
)
CALIBRATION_COLUMNS = ("range", "count", "predicted", "actual")
RECORD_SECTIONS = {
    "rows": RECORD_COLUMNS,
    "metrics": METRIC_COLUMNS,
    "calibration": CALIBRATION_COLUMNS,
}
VALUE_COLUMNS = (
    "match_id",
    "date_local",
    "time_local",
    "competition",
    "home",
    "away",
    "grade",
    "confidence",
    "market_key",
    "market_label",
    "probability",
    "fair_odds",
    "odds",
    "edge",
    "ev",
    "sport",
)
LOGO_COLUMNS = ("home_logo", "away_logo", "league_logo")
# Basketball and tennis board rows: the headline markets of the card (sports.main_markets:
# 1, 2, then handicap + total for basketball, set total + likeliest set score for tennis).
SPORT_HEAD_COLUMNS = (
    "match_id",
    "sport",
    "source",
    "date_utc",
    "time_utc",
    "kickoff_utc",
    "date_local",
    "time_local",
    "country",
    "competition",
    "competition_id",
    "home",
    "away",
    "status",
    "upcoming",
    "grade",
    "confidence",
    "p_1",
    "p_2",
    "odds_1",
    "odds_2",
    "fair_1",
    "fair_2",
    "main_3_key",
    "main_3_label",
    "main_3_p",
    "main_3_odds",
    "main_4_key",
    "main_4_label",
    "main_4_p",
    "main_4_odds",
)
# analysis["expected"] of each sport, flattened.
EXPECTED_COLUMNS = {
    "basketball": ("exp_home", "exp_away", "exp_total", "exp_margin", "p_overtime"),
    "tennis": ("best_of", "set_win", "surface", "exp_games"),
}
SPORT_TAIL_COLUMNS = (
    "tip_key",
    "tip_label",
    "tip_p",
    "tip_odds",
    "selection_key",
    "selection_label",
    "selection_p",
    "value_key",
    "value_label",
    "value_odds",
    "value_ev",
    "form_home",
    "form_away",
    "sample_home",
    "sample_away",
    "sample_h2h",
    "result",
    "tip_won",
    "selection_won",
    "summary",
) + LOGO_COLUMNS
SPORT_SUMMARY_EXTRA = (
    "days_since_home",
    "days_since_away",
    "h2h_played",
    "h2h_home_wins",
    "h2h_away_wins",
    "retrospective",
    "threshold",
    "reason",
    "version",
)
PREDICTION_COLUMNS_BY_SPORT = {
    "football": PREDICTION_COLUMNS,
    **{
        sport: SPORT_HEAD_COLUMNS + expected + SPORT_TAIL_COLUMNS
        for sport, expected in EXPECTED_COLUMNS.items()
    },
}
SUMMARY_COLUMNS_BY_SPORT = {
    "football": SUMMARY_COLUMNS,
    **{
        sport: PREDICTION_COLUMNS_BY_SPORT[sport] + SPORT_SUMMARY_EXTRA
        for sport in EXPECTED_COLUMNS
    },
}
ANALYZE_COLUMNS_BY_SPORT = {
    sport: columns + ("saved", "warnings") for sport, columns in SUMMARY_COLUMNS_BY_SPORT.items()
}
# Sections that need the football score matrix.
FOOTBALL_SECTIONS = ("scores", "grid", "htft")

# ---- product tables (flattened JSON API) ------------------------------------------------
# One leg of a ticket, a single, a simulated day or a wallet bet.
LEG_COLUMNS = (
    "match_id",
    "sport",
    "kickoff_utc",
    "date_local",
    "time_local",
    "competition",
    "competition_id",
    "home",
    "away",
    "key",
    "label",
    "group",
    "probability",
    "odds",
    "fair_odds",
    "ev",
    "grade",
    "confidence",
    "status",
    "score",
    "reason",
) + LOGO_COLUMNS
RECO_LEG_COLUMNS = (
    "day",
    "target",
    "ticket_status",
    "ticket_total_odds",
    "ticket_probability",
    "ticket_ev",
    "legs_count",
    "leg",
) + LEG_COLUMNS
RECO_TICKET_COLUMNS = (
    "day",
    "target",
    "status",
    "total_odds",
    "probability",
    "ev",
    "legs",
    "max_legs",
    "window_low",
    "window_high",
    "payout_odds",
    "sports",
    "selections",
    "rationale",
    "reason",
    "warnings",
    "generated_at",
    "disclaimer",
)
RECO_SINGLE_COLUMNS = ("rank",) + LEG_COLUMNS
RECO_SECTIONS = {
    "legs": RECO_LEG_COLUMNS,
    "tickets": RECO_TICKET_COLUMNS,
    "singles": RECO_SINGLE_COLUMNS,
}
LIVE_COLUMNS = (
    "match_id",
    "sport",
    "kickoff_utc",
    "competition",
    "competition_id",
    "home",
    "away",
    "status",
    "score_home",
    "score_away",
    "minute",
    "period",
    "stage",
    "clock",
    "p_1",
    "p_x",
    "p_2",
    "suggestion",
    "suggestion_key",
    "suggestion_p",
    "suggestion_fair_odds",
    "suggestion_min_odds",
    "suggestion_kind",
    "suggestion_why",
    "suggestions",
    "summary",
    "pre_match_source",
    "pre_match_odds_1",
    "pre_match_odds_x",
    "pre_match_odds_2",
    "notes",
    "updated_at",
    "odds_note",
    "disclaimer",
) + LOGO_COLUMNS
LIVE_MARKET_COLUMNS = (
    "match_id",
    "home",
    "away",
    "minute",
    "score",
    "key",
    "label",
    "group",
    "probability",
    "fair_odds",
    "selectable",
    "reliable",
    "why",
)
LIVE_SECTIONS = {"matches": LIVE_COLUMNS, "markets": LIVE_MARKET_COLUMNS}
SIM_SUMMARY_COLUMNS = (
    "dataset",
    "dataset_label",
    "sport",
    "strategy",
    "mode",
    "staking",
    "target_odds",
    "start",
    "end",
    "initial",
    "final",
    "profit",
    "staked",
    "roi",
    "growth",
    "bets",
    "won",
    "lost",
    "void",
    "hit_rate",
    "max_drawdown",
    "peak",
    "longest_losing_streak",
    "avg_odds",
    "days",
    "betting_days",
    "stopped",
    "reinvest",
    "restart_on_loss",
    "max_days",
    "first_run_days",
    "first_run_peak",
    "first_run_status",
    "longest_streak",
    "longest_streak_peak",
    "best_peak",
    "restarts",
    "lost_ladders",
    "cashed_ladders",
    "total_invested",
    "total_returned",
    "net",
    "days_without_ticket",
    "ladders",
    "baseline_label",
    "baseline_final",
    "baseline_profit",
    "baseline_roi",
    "baseline_hit_rate",
    "warnings",
    "method",
    "disclaimer",
)
# One simulated day (ladder) or one bet (other strategies).
SIM_DAY_COLUMNS = (
    "n",
    "date",
    "result",
    "stake",
    "odds",
    "probability",
    "payout",
    "bankroll_before",
    "bankroll_after",
    "ladder_index",
    "streak_day",
    "legs_count",
    "selections",
    "reason",
)
SIM_LEG_COLUMNS = ("date", "n", "ladder_index", "leg") + LEG_COLUMNS
SIM_LADDER_COLUMNS = (
    "n",
    "start",
    "end",
    "days",
    "tickets",
    "won",
    "void",
    "invested",
    "peak",
    "final",
    "status",
)
SIM_EQUITY_COLUMNS = ("date", "bankroll")
SIM_SECTIONS = {
    "days": SIM_DAY_COLUMNS,
    "summary": SIM_SUMMARY_COLUMNS,
    "legs": SIM_LEG_COLUMNS,
    "ladders": SIM_LADDER_COLUMNS,
    "equity": SIM_EQUITY_COLUMNS,
}
SIM_DATASET_COLUMNS = (
    "id",
    "sport",
    "label",
    "matches",
    "bettable",
    "start",
    "end",
    "source",
    "available",
    "hint",
)
RECENT_COLUMNS = (
    "status",
    "done",
    "total",
    "days_loaded",
    "days_total",
    "matches",
    "loaded_matches",
    "requests",
    "message",
)
WALLET_SUMMARY_COLUMNS = (
    "currency",
    "balance",
    "deposited",
    "staked_open",
    "profit",
    "open",
    "won",
    "lost",
    "void",
    "bets",
    "notice",
)
WALLET_BET_COLUMNS = (
    "id",
    "created_utc",
    "label",
    "source",
    "stake",
    "total_odds",
    "status",
    "payout",
    "settled_utc",
    "legs_count",
    "selections",
)
WALLET_LEG_COLUMNS = ("bet_id", "leg") + LEG_COLUMNS
WALLET_HISTORY_COLUMNS = ("at_utc", "type", "amount", "balance", "bet_id")
WALLET_SECTIONS = {
    "summary": WALLET_SUMMARY_COLUMNS,
    "bets": WALLET_BET_COLUMNS,
    "legs": WALLET_LEG_COLUMNS,
    "history": WALLET_HISTORY_COLUMNS,
}
# Every table the Excel client can request: path template -> columns (per section).
TABLES = {
    "/api/excel/health": HEALTH_COLUMNS,
    "/api/excel/predictions": PREDICTION_COLUMNS,
    "/api/excel/competitions": COMPETITION_COLUMNS,
    "/api/excel/match/{match_id}": SECTIONS,
    "/api/excel/analyze/{match_id}": ANALYZE_COLUMNS,
    "/api/excel/record": RECORD_SECTIONS,
    "/api/excel/value": VALUE_COLUMNS,
    "/api/excel/recommendations": RECO_SECTIONS,
    "/api/excel/live": LIVE_SECTIONS,
    "/api/excel/simulate": SIM_SECTIONS,
    "/api/excel/simulate/datasets": SIM_DATASET_COLUMNS,
    "/api/excel/simulate/recent": RECENT_COLUMNS,
    "/api/excel/wallet": WALLET_SECTIONS,
}
# Basketball/tennis variants of the sport-aware tables (football is in TABLES above).
SPORT_TABLES = {
    "/api/excel/predictions": PREDICTION_COLUMNS_BY_SPORT,
    "/api/excel/match/{match_id}?section=summary": SUMMARY_COLUMNS_BY_SPORT,
    "/api/excel/analyze/{match_id}": ANALYZE_COLUMNS_BY_SPORT,
}

Threshold = Annotated[float, Query(ge=0.5, le=0.99)]
Grade = Annotated[str, Query(pattern="^[ABCDabcd]$")]
Day = Annotated[date, Query()]
Sport = Annotated[str, Query(pattern=SPORT_PATTERN)]
SportList = Annotated[str, Query(max_length=60)]
ALL_SPORTS = ",".join(SPORTS)


# ---------------------------------------------------------------------------- rendering


def cell(value):
    """One value as text: '.' decimals, no exponent, 1/0 booleans, '' for missing."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return "0" if text in ("", "-0") else text
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def clean(text):
    return text.translate(BREAKS)


def formula_safe(value, text):
    """CSV opened by double-click: never let a team name become an Excel formula."""
    if isinstance(value, str) and text.startswith(FORMULA_START):
        return "'" + text
    return text


def render(columns, rows, fmt):
    if fmt == "tsv":
        lines = ["\t".join(columns)]
        lines += ["\t".join(clean(cell(row.get(c))) for c in columns) for row in rows]
        return "\r\n".join(lines) + "\r\n"
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        values = [row.get(c) for c in columns]
        writer.writerow([formula_safe(v, clean(cell(v))) for v in values])
    return BOM + buffer.getvalue()


def output_format(request):
    fmt = request.query_params.get("format", "csv").strip().lower() or "csv"
    return fmt if fmt in FORMATS else None


def table(request, columns, rows, status=200, headers=None):
    fmt = output_format(request) or "csv"
    return Response(
        render(columns, rows, fmt).encode("utf-8"),
        status_code=status,
        media_type=FORMATS[fmt],
        headers={"Cache-Control": "no-store", **(headers or {})},
    )


def error_table(request, message, status):
    return table(request, ERROR_COLUMNS, [{"error": str(message), "status": status}], status)


def validation_message(exc):
    names = []
    for error in exc.errors():
        location = [str(part) for part in error.get("loc", ()) if part not in ("query", "path")]
        if location and location[-1] not in names:
            names.append(location[-1])
    detail = ", ".join(names) or "cerere"
    return f"Parametri invalizi ({detail}). Verifică data (AAAA-LL-ZZ), ID-ul și pragul."


class ExcelRoute(APIRoute):
    """Every failure under /api/excel becomes a one-row error table with a real status."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def route(request):
            if output_format(request) is None:
                return error_table(request, "Format necunoscut. Folosește format=csv sau tsv.", 422)
            try:
                return await handler(request)
            except RequestValidationError as exc:
                return error_table(request, validation_message(exc), 422)
            except HTTPException as exc:
                return error_table(request, exc.detail, exc.status_code)
            except ProviderError as exc:
                return error_table(request, exc, exc.status)
            except Exception:
                logger.exception("Excel API failure on %s", request.url.path)
                return error_table(
                    request, "Eroare internă a serverului. Verifică fereastra start.ps1.", 500
                )

        return route


router = APIRouter(prefix="/api/excel", route_class=ExcelRoute, tags=["excel"])


# ---------------------------------------------------------------------------- helpers


def state(request):
    """Objects shared with the web API, exposed by create_app as app.state.excel_*."""
    return request.app.state


def iso_utc(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local(moment):
    """The server runs on the user's PC, so its local zone is the Excel user's zone."""
    try:
        return moment.astimezone()
    except (OSError, OverflowError, ValueError):
        return moment.astimezone(timezone.utc)


def rate(part, whole):
    return part / whole if whole else None


def grade_allowed(grade, min_grade):
    return GRADES.index(grade) <= GRADES.index(min_grade.upper())


def column_key(key):
    return "p_" + key.lower()


def is_upcoming(match, now):
    return match.status == "scheduled" and match.kickoff > now


def logo_link(value, base=""):
    """Absolute URL of a logo through the local proxy (/api/img?u=...), or None.

    `value` is either a display URL from the JSON API or an upstream URL of a Match; anything
    that is not on an allowed image host is dropped (media.display_url).
    """
    if not isinstance(value, str) or not value:
        return None
    if value.startswith(DISPLAY_PREFIX):
        return base + value
    shown = display_url(value)
    return base + shown if shown else None


def match_logos(match, base=""):
    return {field: logo_link(getattr(match, field, None), base) for field in LOGO_COLUMNS}


def base_url(request):
    return str(request.base_url).rstrip("/")


def kickoff_fields(match):
    kickoff = local(match.kickoff)
    utc = match.kickoff.astimezone(timezone.utc)
    return {
        "date_utc": utc.date().isoformat(),
        "time_utc": utc.strftime("%H:%M"),
        "kickoff_utc": iso_utc(match.kickoff),
        "date_local": kickoff.date().isoformat(),
        "time_local": kickoff.strftime("%H:%M"),
    }


def best_value(markets):
    return max(
        (m for m in markets if m["ev"] is not None and m["ev"] > 0),
        key=lambda m: m["ev"],
        default=None,
    )


def prediction_row(match, analysis, now, base=""):
    if match.sport != "football":
        return sport_prediction_row(match, analysis, now, base)
    markets = analysis["markets"]
    by_key = {m["key"]: m for m in markets}
    tip = headline_tip(analysis)
    selection = analysis["selection"]
    value = best_value(markets)
    scores = analysis["scores"]
    htft = analysis["htft"][0]
    form = analysis["form"]
    row = {
        "match_id": match.id,
        "source": match.source,
        **kickoff_fields(match),
        "country": match.country,
        "competition": competition_name(match.league),
        "competition_id": match_competition(match),
        "home": match.home,
        "away": match.away,
        "status": match.status,
        "upcoming": is_upcoming(match, now),
        "grade": analysis["grade"],
        "confidence": analysis["confidence"],
        "xg_home": analysis["expected_goals"]["home"],
        "xg_away": analysis["expected_goals"]["away"],
        "htft_1": htft["key"],
        "htft_label_1": htft["label"],
        "p_htft_1": htft["probability"],
        "tip_key": tip["key"],
        "tip_label": tip["label"],
        "tip_p": tip["probability"],
        "selection_key": selection["key"] if selection else None,
        "selection_label": selection["label"] if selection else None,
        "selection_p": selection["probability"] if selection else None,
        "value_key": value["key"] if value else None,
        "value_label": value["label"] if value else None,
        "value_odds": value["odds"] if value else None,
        "value_ev": value["ev"] if value else None,
        "sample_home": analysis["sample"]["home"],
        "sample_away": analysis["sample"]["away"],
        "sample_h2h": analysis["sample"]["h2h"],
        "summary": analysis["summary"],
        "sport": match.sport,
        **match_logos(match, base),
    }
    for key, market in by_key.items():
        row[column_key(key)] = market["probability"]
    for n, score in enumerate(scores[:3], 1):
        row[f"score_{n}"] = score["score"]
        row[f"p_score_{n}"] = score["probability"]
    for key in ("1", "X", "2"):
        row[f"odds_{key.lower()}"] = match.odds.get(key)
        row[f"fair_{key.lower()}"] = by_key[key]["fair_odds"]
    for side in ("home", "away"):
        last10 = form[side]["last10"] or {}
        row[f"form_{side}"] = form[side]["sequence"]
        row[f"ppg_{side}"] = last10.get("points_per_game")
        row[f"win_rate_{side}"] = rate(last10.get("wins", 0), last10.get("played", 0))
    if match.status == "finished":
        row["result"] = f"{match.home_goals}-{match.away_goals}"
        row["tip_won"] = outcome(tip["key"], match.home_goals, match.away_goals)
        if selection:
            row["selection_won"] = outcome(selection["key"], match.home_goals, match.away_goals)
    return {k: v for k, v in row.items() if k in PREDICTION_COLUMNS}


def expected_fields(sport, expected):
    expected = expected or {}
    if sport == "basketball":
        return {
            "exp_home": expected.get("home"),
            "exp_away": expected.get("away"),
            "exp_total": expected.get("total"),
            "exp_margin": expected.get("margin"),
            "p_overtime": expected.get("overtime"),
        }
    return {
        "best_of": expected.get("best_of"),
        "set_win": expected.get("set_win"),
        "surface": expected.get("surface"),
        "exp_games": expected.get("games"),
    }


def result_of(match, key):
    """True/False once the match is final, None for a void, a push or an open game."""
    return settle(
        match.sport, key, match.home_goals, match.away_goals, match.finish_type or match.status
    )


def sport_prediction_row(match, analysis, now, base=""):
    """Basketball / tennis board row: the card's headline markets, tip, value and form."""
    sport = match.sport
    by_key = {m["key"]: m for m in analysis["markets"]}
    main = main_markets(analysis)
    tip = headline_tip(analysis)
    selection = analysis["selection"]
    value = best_value(analysis["markets"])
    form = analysis["form"]
    row = {
        "match_id": match.id,
        "sport": sport,
        "source": match.source,
        **kickoff_fields(match),
        "country": match.country,
        "competition": competition_name(match.league),
        "competition_id": match_competition(match),
        "home": match.home,
        "away": match.away,
        "status": match.status,
        "upcoming": is_upcoming(match, now),
        "grade": analysis["grade"],
        "confidence": analysis["confidence"],
        **expected_fields(sport, analysis.get("expected")),
        "tip_key": tip["key"],
        "tip_label": tip["label"],
        "tip_p": tip["probability"],
        "tip_odds": tip.get("odds"),
        "selection_key": selection["key"] if selection else None,
        "selection_label": selection["label"] if selection else None,
        "selection_p": selection["probability"] if selection else None,
        "value_key": value["key"] if value else None,
        "value_label": value["label"] if value else None,
        "value_odds": value["odds"] if value else None,
        "value_ev": value["ev"] if value else None,
        "form_home": form["home"].get("sequence"),
        "form_away": form["away"].get("sequence"),
        "sample_home": analysis["sample"].get("home"),
        "sample_away": analysis["sample"].get("away"),
        "sample_h2h": analysis["sample"].get("h2h"),
        "summary": analysis["summary"],
        **match_logos(match, base),
    }
    for key in ("1", "2"):
        market = by_key.get(key)
        row[f"p_{key}"] = market["probability"] if market else None
        row[f"fair_{key}"] = market["fair_odds"] if market else None
        row[f"odds_{key}"] = match.odds.get(key)
    for n, market in enumerate(main[2:4], 3):
        row[f"main_{n}_key"] = market["key"]
        row[f"main_{n}_label"] = market["label"]
        row[f"main_{n}_p"] = market["probability"]
        row[f"main_{n}_odds"] = market["odds"]
    if match.status == "finished" or match.finish_type:
        if match.home_goals is not None and match.away_goals is not None:
            row["result"] = f"{match.home_goals}-{match.away_goals}"
        row["tip_won"] = result_of(match, tip["key"])
        if selection:
            row["selection_won"] = result_of(match, selection["key"])
    return {k: v for k, v in row.items() if k in PREDICTION_COLUMNS_BY_SPORT[sport]}


def summary_row(match, analysis, now, base=""):
    row = prediction_row(match, analysis, now, base)
    h2h = analysis["h2h"]
    form = analysis["form"]
    if match.sport != "football":
        row.update(
            days_since_home=form["home"].get("days_since_last"),
            days_since_away=form["away"].get("days_since_last"),
            h2h_played=h2h.get("played"),
            h2h_home_wins=h2h.get("home_wins"),
            h2h_away_wins=h2h.get("away_wins"),
            retrospective=not is_upcoming(match, now),
            threshold=analysis["threshold"],
            reason=analysis["reason"],
            version=analysis["version"],
        )
        return row
    by_key = {m["key"]: m for m in analysis["markets"]}
    for key in (
        "over05",
        "over45",
        "home_over05",
        "away_over05",
        "home_over15",
        "away_over15",
        "ht_over05",
        "ht_over15",
    ):
        row[column_key(key)] = by_key[key]["probability"]
    row.update(
        days_since_home=form["home"]["days_since_last"],
        days_since_away=form["away"]["days_since_last"],
        h2h_played=h2h["played"],
        h2h_home_wins=h2h.get("home_wins"),
        h2h_draws=h2h.get("draws"),
        h2h_away_wins=h2h.get("away_wins"),
        h2h_goals_avg=h2h.get("goals_avg"),
        h2h_over25=h2h.get("over25"),
        h2h_btts=h2h.get("btts"),
        retrospective=not is_upcoming(match, now),
        threshold=analysis["threshold"],
        reason=analysis["reason"],
        version=analysis["version"],
    )
    return row


def market_rows(analysis):
    tip = headline_tip(analysis)["key"]
    selection = (analysis["selection"] or {}).get("key")
    return [
        {
            **{c: m.get(c) for c in MARKET_COLUMNS},
            "is_tip": m["key"] == tip,
            "is_selection": m["key"] == selection,
        }
        for m in analysis["markets"]
    ]


def fair(probability):
    return 1 / probability if probability > 0 else None


def score_rows(analysis):
    rows = []
    for rank, item in enumerate(analysis["scores"], 1):
        home, away = (int(x) for x in item["score"].split("-"))
        rows.append(
            {
                "rank": rank,
                "score": item["score"],
                "home_goals": home,
                "away_goals": away,
                "probability": item["probability"],
                "fair_odds": fair(item["probability"]),
            }
        )
    return rows


def grid_rows(analysis):
    rows = []
    for home, line in enumerate(analysis["score_grid"][:GRID_SIZE]):
        row = {"home_goals": home}
        row.update({f"away_{away}": p for away, p in enumerate(line[:GRID_SIZE])})
        rows.append(row)
    return rows


def htft_rows(analysis):
    return [
        {**item, "rank": rank, "fair_odds": fair(item["probability"])}
        for rank, item in enumerate(analysis["htft"], 1)
    ]


SIDES = (("home", "Gazde"), ("away", "Oaspeți"))
WINDOWS = (
    ("last5", "Ultimele 5"),
    ("last10", "Ultimele 10"),
    ("home10", "Acasă (10)"),
    ("away10", "Deplasare (10)"),
)


def team(match, side):
    return match.home if side == "home" else match.away


def score_pair(text):
    """(home, away) of a "h-a" score, or (None, None) when it is not one."""
    try:
        home, away = (int(x) for x in str(text).split("-"))
    except ValueError:
        return None, None
    return home, away


def form_rows(match, analysis):
    rows = []
    for side, label in SIDES:
        for n, game in enumerate(analysis["form"][side]["last"], 1):
            home_goals, away_goals = score_pair(game["score"])
            home_side = game["venue"] == "A"
            rows.append(
                {
                    **game,
                    "side": label,
                    "team": team(match, side),
                    "n": n,
                    "goals_for": home_goals if home_side else away_goals,
                    "goals_against": away_goals if home_side else home_goals,
                }
            )
    return rows


def formstats_rows(match, analysis):
    """Form windows of both sides; the football-only statistics stay empty elsewhere."""
    rows = []
    for side, label in SIDES:
        form = analysis["form"][side]
        streaks = form.get("streaks") or {}
        for key, window in WINDOWS:
            stats = form.get(key)
            if not stats:
                continue
            played = stats.get("played", 0)
            rows.append(
                {
                    **stats,
                    "side": label,
                    "team": team(match, side),
                    "window": window,
                    "win_rate": rate(stats.get("wins", 0), played),
                    "sequence": form.get("sequence"),
                    "streak_wins": streaks.get("wins"),
                    "streak_unbeaten": streaks.get("unbeaten"),
                    "streak_winless": streaks.get("winless"),
                    "days_since_last": form.get("days_since_last"),
                    "matches_last_30_days": form.get("matches_last_30_days"),
                }
            )
    return rows


def h2h_rows(match, analysis):
    """Mutual games; `result` is W/D/L for today's home team."""
    home = canonical(match.home)
    rows = []
    for game in analysis["h2h"]["matches"]:
        home_goals, away_goals = score_pair(game["score"])
        if home_goals is None:
            continue
        mine, theirs = (
            (home_goals, away_goals)
            if canonical(game["home"]) == home
            else (away_goals, home_goals)
        )
        result = "W" if mine > theirs else "L" if mine < theirs else "D"
        rows.append({**game, "home_goals": home_goals, "away_goals": away_goals, "result": result})
    return rows


def insight_rows(analysis):
    return [{"n": n, "text": text} for n, text in enumerate(analysis["insights"], 1)]


def standing_rows(match, standings):
    rows = []
    for row in standings:
        role = ""
        for side, label in SIDES:
            team_id = match.home_id if side == "home" else match.away_id
            if (team_id and row["team_id"] == team_id) or canonical(row["name"]) == canonical(
                team(match, side)
            ):
                role = label
        rows.append(
            {
                **row,
                "team": row["name"],
                "goal_diff": row["scored"] - row["conceded"],
                "role": role,
            }
        )
    return rows


async def day_matches(request, day, refresh, sport):
    """Fixtures of a day (stored and settled), without the unavailable ones."""
    s = state(request)
    if sport == "football":
        found, _, _, _ = await s.excel_day_fixtures(day, refresh)
    else:
        found, _, _, _ = await s.day_fixtures(day, sport, refresh)
    return [m for m in found if m.status != "unavailable"]


async def board(request, day, refresh, demo, threshold, sport="football"):
    """(match, analysis) for every available fixture of a day, popular competitions first."""
    if demo:
        if sport != "football":
            return [], []
        history, found = demo_data()
        index = HistoryIndex(history)
        items = [(m, analyze(m, index, threshold)) for m in found]
        return items, catalog(found, demo=True)
    s = state(request)
    found = await day_matches(request, day, refresh, sport)
    found.sort(key=priority)
    items = await run_in_threadpool(lambda: [(m, s.excel_cache.get(m, threshold)) for m in found])
    return items, catalog(found, demo=True, sport=sport)


DEMO_PREFIX = "demo-"
DEMO_WARNING = "Mod demo: meci sintetic, fără FlashScore; nu intră în registru."


def demo_fixture(match_id):
    """(fixture, history index) of a demo board match, or None for any other id."""
    if not match_id.startswith(DEMO_PREFIX):
        return None
    history, fixtures = demo_data()
    for match in fixtures:
        if match.id == match_id:
            return match, HistoryIndex(history)
    return None


def stored_match(request, match_id, sport="football"):
    """(match, demo index): a stored match with None, or a demo fixture with its own index."""
    match = state(request).store.match(match_id)
    if match is not None and match.sport == sport:
        return match, None
    if sport == "football":
        demo = demo_fixture(match_id)
        if demo is not None:
            return demo
    if match is not None and match.sport in SPORTS:
        label = SPORTS[match.sport]["label"].lower()
        raise HTTPException(
            404, f"Meciul {match_id} este de {label}: alege sportul {match.sport} (sport=...)."
        )
    raise HTTPException(
        404, "Meciul nu există în baza locală. Încarcă mai întâi predicțiile zilei lui."
    )


async def match_analysis(request, match, demo_index, threshold):
    if demo_index is not None:
        return await run_in_threadpool(analyze, match, demo_index, threshold)
    return await run_in_threadpool(state(request).excel_cache.get, match, threshold)


def check_section(section, sections):
    section = section.strip().lower()
    if section not in sections:
        raise HTTPException(422, "Secțiune necunoscută. Valori: " + ", ".join(sections) + ".")
    return section


def sports_list(text):
    """Comma list "football,tennis" -> ["football", "tennis"] (registry order); 422 if unknown."""
    names = [part.strip().lower() for part in (text or "").split(",") if part.strip()]
    if not names or any(name not in SPORTS for name in names):
        raise HTTPException(422, "Sport necunoscut. Valori: " + ", ".join(SPORTS) + ".")
    return [sport for sport in SPORTS if sport in names]


# ------------------------------------------------------------------ the JSON API, flattened

# In-process requests to the web app's own JSON API (never the network).
INTERNAL_BASE = "http://127.0.0.1"
MISSING_FEATURE = (
    "Serverul nu are încă această funcție. Actualizează proiectul și repornește start.ps1."
)


def api_detail(status, payload):
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if status in (404, 405) and detail in (None, "Not Found", "Method Not Allowed"):
        return MISSING_FEATURE
    if isinstance(detail, str) and detail:
        return detail
    if status == 422:
        return "Parametri invalizi."
    return f"Eroare a serverului (HTTP {status})."


async def call_api(request, method, path, params=None, body=None):
    """JSON of GET/POST `path` on this same app, or an HTTPException with its Romanian detail.

    The web API stays the only implementation of recommendations, live, the simulator and
    the wallet: these tables only flatten what it answers.
    """
    transport = httpx.ASGITransport(app=request.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url=INTERNAL_BASE, timeout=None
    ) as client:
        response = await client.request(method, path, params=params, json=body)
    try:
        payload = response.json()
    except ValueError:
        payload = None
    # 202: POST /api/simulate/recent/prepare accepted the background load.
    if not 200 <= response.status_code < 300:
        raise HTTPException(response.status_code, api_detail(response.status_code, payload))
    if not isinstance(payload, dict):
        raise HTTPException(502, "Răspuns neașteptat de la server.")
    return payload


def moment(text):
    """Aware datetime of an ISO text ("Z" or offset; naive means UTC), or None."""
    if not isinstance(text, str) or not text:
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def utc_text(text):
    value = moment(text)
    return iso_utc(value) if value else text


def market_of(leg):
    """(key, label) of a leg: a Leg has key/label, a ladder leg may carry `market`."""
    market = leg.get("market")
    key = leg.get("key") or (market.get("key") if isinstance(market, dict) else None)
    label = leg.get("label")
    if not label:
        label = market.get("label") if isinstance(market, dict) else market
    return key, label if isinstance(label, str) else None


def leg_row(leg, base):
    kickoff = moment(leg.get("kickoff"))
    shown = local(kickoff) if kickoff else None
    key, label = market_of(leg)
    return {
        "match_id": leg.get("match_id"),
        "sport": leg.get("sport"),
        "kickoff_utc": iso_utc(kickoff) if kickoff else None,
        "date_local": shown.date().isoformat() if shown else None,
        "time_local": shown.strftime("%H:%M") if shown else None,
        "competition": leg.get("competition"),
        "competition_id": leg.get("competition_id"),
        "home": leg.get("home"),
        "away": leg.get("away"),
        "key": key,
        "label": label,
        "group": leg.get("group"),
        "probability": leg.get("probability"),
        "odds": leg.get("odds"),
        "fair_odds": leg.get("fair_odds"),
        "ev": leg.get("ev"),
        "grade": leg.get("grade"),
        "confidence": leg.get("confidence"),
        "status": leg.get("result") or leg.get("status"),
        "score": leg.get("score"),
        "reason": leg.get("reason"),
        **{field: logo_link(leg.get(field), base) for field in LOGO_COLUMNS},
    }


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def selections(legs):
    """One readable line for a ticket: "Home - Away: label @1.52 (won); ..."."""
    parts = []
    for leg in legs or []:
        _, label = market_of(leg)
        text = f"{leg.get('home', '')} - {leg.get('away', '')}: {label or leg.get('key', '')}"
        odds = number(leg.get("odds"))
        if odds:
            text += f" @{odds:.2f}"
        outcome_text = leg.get("result") or leg.get("status")
        if outcome_text and outcome_text != "pending":
            text += f" ({outcome_text})"
        parts.append(text)
    return "; ".join(parts)


def product(values):
    values = [number(v) for v in values]
    if not values or any(v is None for v in values):
        return None
    return math.prod(values)


# ---------------------------------------------------------------------------- endpoints


@router.get("/health")
def health(request: Request):
    s = state(request)
    matches = s.store.matches()
    now = datetime.now(timezone.utc)
    offset = local(now).utcoffset()
    row = {
        "status": "ok",
        "excel_api": EXCEL_API_VERSION,
        "version": VERSION,
        "api_configured": bool(s.excel_settings.api_key),
        "history_matches": sum(m.status == "finished" for m in matches),
        "synced_days": len(s.store.synced_days()),
        "server_time_utc": iso_utc(now),
        "server_time_local": local(now).strftime("%Y-%m-%d %H:%M"),
        "utc_offset_minutes": int(offset.total_seconds() // 60) if offset else 0,
        "sports": ALL_SPORTS,
    }
    return table(request, HEALTH_COLUMNS, [row])


@router.get("/predictions")
async def predictions(
    request: Request,
    day: Day,
    competition: str = "",
    limit: Annotated[int, Query(ge=1, le=400)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
    min_grade: Grade = "D",
    upcoming_only: bool = False,
    refresh: bool = False,
    demo: bool = False,
    threshold: Threshold = 0.85,
    sport: Sport = "football",
):
    items, _ = await board(request, day, refresh, demo, threshold, sport)
    now = datetime.now(timezone.utc)
    if competition:
        items = [(m, a) for m, a in items if match_competition(m) == competition]
    items = [(m, a) for m, a in items if grade_allowed(a["grade"], min_grade)]
    if upcoming_only:
        items = [(m, a) for m, a in items if is_upcoming(m, now)]
    base = base_url(request)
    rows = [prediction_row(m, a, now, base) for m, a in items[offset : offset + limit]]
    return table(
        request,
        PREDICTION_COLUMNS_BY_SPORT[sport],
        rows,
        headers={"X-Total-Count": str(len(items))},
    )


@router.get("/competitions")
async def competitions(
    request: Request,
    day: Day,
    refresh: bool = False,
    demo: bool = False,
    sport: Sport = "football",
):
    if demo:
        _, found = demo_data() if sport == "football" else (None, [])
    else:
        found = await day_matches(request, day, refresh, sport)
    rows = [
        {**c, "competition_id": c["id"], "competition": c["name"], "matches": c["count"]}
        for c in catalog(found, demo=True, sport=sport)
    ]
    return table(request, COMPETITION_COLUMNS, rows)


@router.get("/match/{match_id}")
async def match_section(
    request: Request,
    match_id: str,
    section: str = "summary",
    threshold: Threshold = 0.85,
    sport: Sport = "football",
):
    section = check_section(section, SECTIONS)
    if sport != "football" and section in FOOTBALL_SECTIONS:
        raise HTTPException(422, f"Secțiunea {section} există doar pentru fotbal.")
    match, demo_index = stored_match(request, match_id, sport)
    if section == "standings":
        if demo_index is not None or match.sport == "tennis":
            return table(request, STANDING_COLUMNS, [])
        standings = await state(request).excel_provider.standings(match)
        return table(request, STANDING_COLUMNS, standing_rows(match, standings))
    analysis = await match_analysis(request, match, demo_index, threshold)
    now = datetime.now(timezone.utc)
    base = base_url(request)
    builders = {
        "summary": lambda: [summary_row(match, analysis, now, base)],
        "markets": lambda: market_rows(analysis),
        "scores": lambda: score_rows(analysis),
        "grid": lambda: grid_rows(analysis),
        "htft": lambda: htft_rows(analysis),
        "form": lambda: form_rows(match, analysis),
        "formstats": lambda: formstats_rows(match, analysis),
        "h2h": lambda: h2h_rows(match, analysis),
        "insights": lambda: insight_rows(analysis),
    }
    columns = SUMMARY_COLUMNS_BY_SPORT[sport] if section == "summary" else SECTIONS[section]
    return table(request, columns, builders[section]())


@router.post("/analyze/{match_id}")
async def analyze_match(
    request: Request,
    match_id: str,
    enrich: bool = True,
    refresh: bool = False,
    threshold: Threshold = 0.85,
    sport: Sport = "football",
):
    """Same as POST /api/analyze: FlashScore H2H + standings + prices, then the snapshot."""
    s = state(request)
    match, demo_index = stored_match(request, match_id, sport)
    now = datetime.now(timezone.utc)
    base = base_url(request)
    columns = ANALYZE_COLUMNS_BY_SPORT[sport]
    if demo_index is not None:
        # Synthetic: no FlashScore request and nothing enters the prospective ledger.
        analysis = await match_analysis(request, match, demo_index, threshold)
        row = summary_row(match, analysis, now, base) | {"saved": False, "warnings": DEMO_WARNING}
        return table(request, columns, [row])
    warnings = []
    if enrich:
        warnings, _ = await s.excel_enrich(match, refresh)
        # Enrichment may have merged matches/odds prices into the stored fixture.
        match = s.store.match(match.id) or match
    analysis = await match_analysis(request, match, None, threshold)
    # Exactly like POST /api/analyze: only pre-match snapshots enter the prospective ledger,
    # and always at the ledger threshold, so a view at 0.5 never freezes a 50% pick.
    saved = False
    if is_upcoming(match, now):
        ledger_threshold = getattr(s, "excel_ledger_threshold", LEDGER_THRESHOLD)
        ledger = analysis
        if threshold != ledger_threshold:
            ledger = await run_in_threadpool(s.excel_cache.get, match, ledger_threshold)
        saved = s.store.snapshot(match, compact(ledger), now)
    row = summary_row(match, analysis, now, base) | {
        "saved": saved,
        "warnings": " | ".join(warnings),
    }
    return table(request, columns, [row])


LEDGER_STATUS = {True: "câștigat", False: "pierdut", None: "anulat"}


@router.get("/record")
def record(request: Request, section: str = "rows"):
    section = section.strip().lower()
    if section not in RECORD_SECTIONS:
        raise HTTPException(422, "Secțiune necunoscută. Valori: rows, metrics, calibration.")
    store = state(request).store
    ledger = store.predictions()
    if section == "metrics":
        metrics = summarize(ledger, store.assessment_count())
        interval = metrics["interval95"] or (None, None)
        row = {**metrics, "ci_low": interval[0], "ci_high": interval[1]}
        return table(request, METRIC_COLUMNS, [row])
    if section == "calibration":
        metrics = summarize(ledger, store.assessment_count())
        return table(request, CALIBRATION_COLUMNS, metrics["calibration"])
    rows = []
    for item in ledger[:1000]:
        match = Match.model_validate(item["match"])
        pick = item["prediction"]["selection"]
        result = item["result"]
        rows.append(
            {
                "match_id": match.id,
                "created_utc": iso_utc(datetime.fromtimestamp(item["created"], timezone.utc)),
                "date_utc": match.kickoff.astimezone(timezone.utc).date().isoformat(),
                "time_utc": match.kickoff.astimezone(timezone.utc).strftime("%H:%M"),
                "competition": competition_name(match.league),
                "home": match.home,
                "away": match.away,
                "selection_key": pick["key"],
                "selection_label": pick["label"],
                "probability": pick["probability"],
                "fair_odds": fair(pick["probability"]),
                "grade": item["prediction"].get("grade"),
                "confidence": item["prediction"].get("confidence"),
                "status": "în așteptare" if result is None else LEDGER_STATUS[result["won"]],
                "score": result["score"] if result else None,
                "won": result["won"] if result else None,
                "sport": match.sport,
            }
        )
    return table(request, RECORD_COLUMNS, rows)


@router.get("/value")
async def value(
    request: Request,
    day: Day,
    competition: str = "",
    min_grade: Grade = "D",
    min_ev: Annotated[float, Query(ge=-1, le=10)] = 0.0,
    upcoming_only: bool = False,
    refresh: bool = False,
    demo: bool = False,
    threshold: Threshold = 0.85,
    sport: Sport = "football",
):
    items, _ = await board(request, day, refresh, demo, threshold, sport)
    now = datetime.now(timezone.utc)
    rows = []
    for match, analysis in items:
        if competition and match_competition(match) != competition:
            continue
        if not grade_allowed(analysis["grade"], min_grade):
            continue
        if upcoming_only and not is_upcoming(match, now):
            continue
        kickoff = local(match.kickoff)
        for market in analysis["markets"]:
            if market["ev"] is None or market["ev"] <= min_ev or not market["odds"]:
                continue
            rows.append(
                {
                    "match_id": match.id,
                    "date_local": kickoff.date().isoformat(),
                    "time_local": kickoff.strftime("%H:%M"),
                    "competition": competition_name(match.league),
                    "home": match.home,
                    "away": match.away,
                    "grade": analysis["grade"],
                    "confidence": analysis["confidence"],
                    "market_key": market["key"],
                    "market_label": market["label"],
                    "probability": market["probability"],
                    "fair_odds": market["fair_odds"],
                    "odds": market["odds"],
                    "edge": market["probability"] - 1 / market["odds"],
                    "ev": market["ev"],
                    "sport": match.sport,
                }
            )
    rows.sort(key=lambda r: r["ev"], reverse=True)
    return table(request, VALUE_COLUMNS, rows)


# ------------------------------------------------------------------ recommendations


@router.get("/recommendations")
async def recommendations(
    request: Request,
    day: Day,
    sports: SportList = ALL_SPORTS,
    targets: Annotated[str, Query(max_length=80)] = "2,5,10,100",
    refresh: bool = False,
    section: str = "legs",
):
    """GET /api/recommendations as rows: ticket legs, the tickets summary or the singles."""
    section = check_section(section, RECO_SECTIONS)
    params = {
        "day": day.isoformat(),
        "sports": ",".join(sports_list(sports)),
        "targets": targets,
        "refresh": "true" if refresh else "false",
    }
    data = await call_api(request, "GET", "/api/recommendations", params)
    base = base_url(request)
    tickets = data.get("tickets") or []
    if section == "singles":
        rows = [
            {"rank": n, **leg_row(leg, base)} for n, leg in enumerate(data.get("singles") or [], 1)
        ]
        return table(request, RECO_SINGLE_COLUMNS, rows)
    if section == "tickets":
        warnings = " | ".join(data.get("warnings") or [])
        rows = []
        for ticket in tickets:
            legs = ticket.get("legs") or []
            window = ticket.get("window") or (None, None)
            used = {leg.get("sport") for leg in legs}
            rows.append(
                {
                    **ticket,
                    "day": ticket.get("day") or data.get("day"),
                    "target": ticket.get("target", ticket.get("target_odds")),
                    "legs": len(legs),
                    "window_low": window[0] if len(window) > 1 else None,
                    "window_high": window[1] if len(window) > 1 else None,
                    "sports": ",".join(s for s in SPORTS if s in used),
                    "selections": selections(legs),
                    "warnings": warnings,
                    "generated_at": data.get("generated_at"),
                    "disclaimer": data.get("disclaimer"),
                }
            )
        return table(request, RECO_TICKET_COLUMNS, rows)
    rows = []
    for ticket in tickets:
        legs = ticket.get("legs") or []
        for n, leg in enumerate(legs, 1):
            rows.append(
                {
                    **leg_row(leg, base),
                    "day": ticket.get("day") or data.get("day"),
                    "target": ticket.get("target", ticket.get("target_odds")),
                    "ticket_status": ticket.get("status"),
                    "ticket_total_odds": ticket.get("total_odds"),
                    "ticket_probability": ticket.get("probability"),
                    "ticket_ev": ticket.get("ev"),
                    "legs_count": len(legs),
                    "leg": n,
                }
            )
    return table(request, RECO_LEG_COLUMNS, rows)


# ------------------------------------------------------------------ live


def suggestion_text(suggestion):
    text = suggestion.get("label") or suggestion.get("key") or ""
    probability = number(suggestion.get("probability"))
    min_odds = number(suggestion.get("min_odds"))
    if probability is not None:
        text += f" ({probability:.0%}"
        if min_odds:
            text += f", cotă minimă {min_odds:.2f}"
        text += ")"
    return text


def live_row(item, data, base):
    match = item.get("match") or {}
    score = item.get("score") or {}
    probabilities = item.get("probabilities") or {}
    suggestions = item.get("suggestions") or []
    first = suggestions[0] if suggestions else {}
    pre = item.get("pre_match") or {}
    pre_odds = pre.get("odds") or {}
    return {
        "match_id": match.get("id"),
        "sport": item.get("sport") or match.get("sport"),
        "kickoff_utc": utc_text(match.get("kickoff")),
        "competition": item.get("competition"),
        "competition_id": item.get("competition_id"),
        "home": match.get("home"),
        "away": match.get("away"),
        "status": match.get("status"),
        "score_home": score.get("home"),
        "score_away": score.get("away"),
        "minute": item.get("minute"),
        "period": item.get("period"),
        "stage": item.get("stage"),
        "clock": item.get("clock"),
        "p_1": probabilities.get("1"),
        "p_x": probabilities.get("X"),
        "p_2": probabilities.get("2"),
        "suggestion": first.get("label"),
        "suggestion_key": first.get("key"),
        "suggestion_p": first.get("probability"),
        "suggestion_fair_odds": first.get("fair_odds"),
        "suggestion_min_odds": first.get("min_odds"),
        "suggestion_kind": first.get("kind"),
        "suggestion_why": first.get("why"),
        "suggestions": " | ".join(suggestion_text(s) for s in suggestions),
        "summary": item.get("summary"),
        "pre_match_source": pre.get("source"),
        "pre_match_odds_1": pre_odds.get("1"),
        "pre_match_odds_x": pre_odds.get("X"),
        "pre_match_odds_2": pre_odds.get("2"),
        "notes": " | ".join(item.get("notes") or []),
        "updated_at": data.get("updated_at"),
        "odds_note": data.get("odds_note"),
        "disclaimer": data.get("disclaimer"),
        # The item's display logos, else the match's own (upstream) ones.
        **{
            field: logo_link(item.get(field), base) or logo_link(match.get(field), base)
            for field in LOGO_COLUMNS
        },
    }


@router.get("/live")
async def live(
    request: Request, sport: Sport = "football", refresh: bool = False, section: str = "matches"
):
    """GET /api/live as rows: one per live game, or one per in-play market."""
    section = check_section(section, LIVE_SECTIONS)
    params = {"sport": sport, "refresh": "true" if refresh else "false"}
    data = await call_api(request, "GET", "/api/live", params)
    items = data.get("matches") or []
    if section == "matches":
        base = base_url(request)
        return table(request, LIVE_COLUMNS, [live_row(item, data, base) for item in items])
    rows = []
    for item in items:
        match = item.get("match") or {}
        score = item.get("score") or {}
        for market in item.get("markets") or []:
            rows.append(
                {
                    **market,
                    "match_id": match.get("id"),
                    "home": match.get("home"),
                    "away": match.get("away"),
                    "minute": item.get("minute"),
                    "score": f"{score.get('home', '')}-{score.get('away', '')}",
                }
            )
    return table(request, LIVE_MARKET_COLUMNS, rows)


# ------------------------------------------------------------------ simulator

# POST /api/simulate is deterministic: the sections of one run (summary, days, ladders, legs,
# equity) are served from one computation for SIM_TTL seconds.
SIM_TTL = 60.0
SIM_MEMO_SIZE = 8


def clock():
    """Monotonic seconds; tests replace it."""
    return time.monotonic()


async def simulation(request, body):
    app_state = state(request)
    memo = getattr(app_state, "excel_sim_memo", None)
    if memo is None:
        memo = app_state.excel_sim_memo = {}
    key = json.dumps(body, sort_keys=True)
    now = clock()
    hit = memo.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    data = await call_api(request, "POST", "/api/simulate", body=body)
    for old in [k for k, (expires, _) in memo.items() if expires <= now]:
        memo.pop(old, None)
    while len(memo) >= SIM_MEMO_SIZE:
        memo.pop(next(iter(memo)))
    memo[key] = (now + SIM_TTL, data)
    return data


def sim_summary_row(data):
    summary = data.get("summary") or {}
    ladder = data.get("ladder") or {}
    baseline = data.get("baseline") or {}
    dataset = data.get("dataset") or {}
    days = data.get("days")
    row = {key: data.get(key, summary.get(key)) for key in SIM_SUMMARY_COLUMNS}
    row.update(
        dataset=dataset.get("id") if isinstance(dataset, dict) else dataset,
        dataset_label=dataset.get("label") if isinstance(dataset, dict) else None,
        days=len(days) if isinstance(days, list) else days,
        ladders=len(ladder.get("ladders") or []) if ladder else None,
        baseline_label=baseline.get("label"),
        baseline_final=baseline.get("final"),
        baseline_profit=baseline.get("profit"),
        baseline_roi=baseline.get("roi"),
        baseline_hit_rate=baseline.get("hit_rate"),
        warnings=" | ".join(data.get("warnings") or []),
    )
    for key in (
        "reinvest",
        "restart_on_loss",
        "max_days",
        "first_run_days",
        "first_run_peak",
        "first_run_status",
        "longest_streak",
        "longest_streak_peak",
        "best_peak",
        "restarts",
        "lost_ladders",
        "cashed_ladders",
        "total_invested",
        "total_returned",
        "net",
        "days_without_ticket",
    ):
        row[key] = ladder.get(key)
    return row


def ticket_numbers(ticket):
    legs = ticket.get("legs") or []
    odds = number(ticket.get("total_odds")) or product(leg.get("odds") for leg in legs)
    probability = number(ticket.get("probability"))
    if probability is None:
        probability = product(leg.get("probability") for leg in legs)
    return legs, odds, probability


def ladder_payout(result, stake, odds):
    if result == "won" and stake is not None and odds:
        return stake * odds
    if result == "void":
        return stake
    if result == "lost":
        return 0.0
    return None


def sim_days(data):
    """[(day row, legs)] of a ladder (one row per day) or of the other strategies (per bet)."""
    days = data.get("days")
    out = []
    if isinstance(days, list):
        for n, day in enumerate(days, 1):
            legs, odds, probability = ticket_numbers(day.get("ticket") or {})
            odds = number(day.get("odds")) or odds
            if number(day.get("probability")) is not None:
                probability = day["probability"]
            stake = number(day.get("stake"))
            result = day.get("result")
            payout = day["payout"] if "payout" in day else ladder_payout(result, stake, odds)
            row = {
                **day,
                "n": n,
                "odds": odds,
                "probability": probability,
                "payout": payout,
                "legs_count": len(legs),
                "selections": selections(legs),
            }
            out.append((row, legs))
        return out
    for n, bet in enumerate(data.get("rows") or [], 1):
        legs = bet.get("legs") or []
        row = {**bet, "n": n, "legs_count": len(legs), "selections": selections(legs)}
        out.append((row, legs))
    return out


def sim_body(params, sports, start, end):
    body = {k: v for k, v in params.items() if v is not None}
    if sports is not None:
        body["sports"] = sports_list(sports)
    if start is not None:
        body["start"] = start.isoformat()
    if end is not None:
        body["end"] = end.isoformat()
    return body


@router.get("/simulate")
async def simulate(
    request: Request,
    section: str = "days",
    dataset: Annotated[str | None, Query(max_length=40)] = None,
    sport: Annotated[str | None, Query(pattern=SPORT_PATTERN)] = None,
    sports: Annotated[str | None, Query(max_length=60)] = None,
    days: Annotated[int | None, Query(ge=1, le=60)] = None,
    bankroll: Annotated[float | None, Query(gt=0, le=10_000_000)] = None,
    strategy: Annotated[str | None, Query(max_length=20)] = None,
    staking: Annotated[str | None, Query(max_length=20)] = None,
    mode: Annotated[str | None, Query(max_length=20)] = None,
    stake: Annotated[float | None, Query(gt=0, le=10_000_000)] = None,
    target_odds: Annotated[float | None, Query(ge=1.01, le=1000)] = None,
    reinvest: Annotated[float | None, Query(gt=0, le=1)] = None,
    restart_on_loss: bool | None = None,
    max_days: Annotated[int | None, Query(ge=1, le=3660)] = None,
    max_bets_per_day: Annotated[int | None, Query(ge=1, le=20)] = None,
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
    seed: Annotated[int | None, Query(ge=0)] = None,
):
    """POST /api/simulate from query parameters (ladder included) as one table per section:
    days (per-day log, default), summary, legs, ladders or equity."""
    section = check_section(section, SIM_SECTIONS)
    params = {
        "dataset": dataset,
        "sport": sport,
        "days": days,
        "bankroll": bankroll,
        "strategy": strategy,
        "staking": staking,
        "mode": mode,
        "stake": stake,
        "target_odds": target_odds,
        "reinvest": reinvest,
        "restart_on_loss": restart_on_loss,
        "max_days": max_days,
        "max_bets_per_day": max_bets_per_day,
        "seed": seed,
    }
    data = await simulation(request, sim_body(params, sports, start, end))
    if section == "summary":
        return table(request, SIM_SUMMARY_COLUMNS, [sim_summary_row(data)])
    if section == "ladders":
        ladders = (data.get("ladder") or {}).get("ladders") or []
        rows = [{**ladder, "n": ladder.get("index", n)} for n, ladder in enumerate(ladders, 1)]
        return table(request, SIM_LADDER_COLUMNS, rows)
    if section == "equity":
        return table(request, SIM_EQUITY_COLUMNS, data.get("equity") or data.get("history") or [])
    if section == "legs":
        base = base_url(request)
        rows = [
            {
                **leg_row(leg, base),
                "date": row.get("date"),
                "n": row["n"],
                "ladder_index": row.get("ladder_index"),
                "leg": index,
            }
            for row, legs in sim_days(data)
            for index, leg in enumerate(legs, 1)
        ]
        return table(request, SIM_LEG_COLUMNS, rows)
    return table(request, SIM_DAY_COLUMNS, [row for row, _ in sim_days(data)])


@router.get("/simulate/datasets")
async def simulate_datasets(request: Request):
    data = await call_api(request, "GET", "/api/simulate/datasets")
    return table(request, SIM_DATASET_COLUMNS, data.get("datasets") or [])


@router.get("/simulate/recent")
async def recent_status(
    request: Request,
    days: Annotated[int | None, Query(ge=1, le=60)] = None,
    sports: Annotated[str | None, Query(max_length=60)] = None,
):
    """Progress of the background load of the recent days (dataset "recent"); with days and
    sports, how much of that window is already loaded."""
    params = {}
    if days is not None:
        params["days"] = days
    if sports is not None:
        params["sports"] = ",".join(sports_list(sports))
    data = await call_api(request, "GET", "/api/simulate/recent/status", params)
    return table(request, RECENT_COLUMNS, [data])


@router.post("/simulate/recent")
async def recent_prepare(
    request: Request,
    days: Annotated[int, Query(ge=1, le=60)] = 14,
    sports: SportList = ALL_SPORTS,
):
    """Starts loading the missing recent days (1 FlashScore list request per day and sport)."""
    body = {"days": days, "sports": sports_list(sports)}
    data = await call_api(request, "POST", "/api/simulate/recent/prepare", body=body)
    return table(request, RECENT_COLUMNS, [data])


# ------------------------------------------------------------------ wallet


@router.get("/wallet")
async def wallet(request: Request, section: str = "summary"):
    """GET /api/wallet (read only): summary, bets, legs of every bet, or money history."""
    section = check_section(section, WALLET_SECTIONS)
    data = await call_api(request, "GET", "/api/wallet")
    bets = data.get("bets") or []
    if section == "summary":
        row = {**data, "bets": len(bets), "notice": data.get("notice") or data.get("disclaimer")}
        return table(request, WALLET_SUMMARY_COLUMNS, [row])
    if section == "history":
        rows = [
            {**entry, "at_utc": utc_text(entry.get("at"))} for entry in data.get("history") or []
        ]
        return table(request, WALLET_HISTORY_COLUMNS, rows)
    if section == "legs":
        base = base_url(request)
        rows = [
            {**leg_row(leg, base), "bet_id": bet.get("id"), "leg": n}
            for bet in bets
            for n, leg in enumerate(bet.get("legs") or [], 1)
        ]
        return table(request, WALLET_LEG_COLUMNS, rows)
    rows = [
        {
            **bet,
            "created_utc": utc_text(bet.get("created")),
            "settled_utc": utc_text(bet.get("settled")),
            "legs_count": len(bet.get("legs") or []),
            "selections": selections(bet.get("legs")),
        }
        for bet in bets
    ]
    return table(request, WALLET_BET_COLUMNS, rows)
