"""Cote pre-meci pentru tenisPrediction: tennis-data.co.uk, potrivire cu TML, marjă, amestec.

Three independent pieces, all opt-in (the v2 model without odds never imports this module):

Odds table
    ``load_odds(years, tours, directory)`` reads the tennis-data.co.uk workbooks (the files
    ``footypreds.evaluation.tennis_data.download`` stores as ``{tour}_{year}.xlsx``) and keeps,
    per match, the winner/loser names, the date and the closing prices of every book we use:
    ``avg`` (AvgW/AvgL, the Oddsportal market average), ``ps`` (Pinnacle), ``b365`` and ``max``.
    These are CLOSING prices (taken just before the start). They are pre-match information, but
    sharper than the earlier FlashScore prices the app sees in production.

Join
    ``attach_odds(rows, source, ...)`` matches benchmark ``Row`` objects (ATP and WTA main tour)
    to tennis-data matches by the unordered pair of players (FlashScore-style "De Minaur A."
    against the TML full name) and a date window around the event start, then returns rows
    wrapped as ``benchmark.OddsRow`` carrying ``winner_odds`` / ``loser_odds``. Prices follow
    the player, not the result column, so a match is found the same way whoever won. Pairs
    met twice in the window or claimed twice stay unmatched.

Margin and blend
    ``fair_probability(odds_1, odds_2, method)`` removes the bookmaker margin (``proportional``,
    ``power``, ``shin`` or ``additive``); ``blend_logit`` is the validated stack
    ``w_model * logit(p_model) + w_market * logit(p_market)`` (no intercept: symmetric).
"""

from __future__ import annotations

import math
import os
import pickle
import warnings
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_ODDS_DIR = ROOT.parent / "footypreds" / "data" / "benchmark" / "tennis" / "raw"
ODDS_TOURS = ("atp", "wta")
BOOKS = {
    "avg": ("AvgW", "AvgL"),
    "ps": ("PSW", "PSL"),
    "b365": ("B365W", "B365L"),
    "max": ("MaxW", "MaxL"),
}
MARGIN_METHODS = ("proportional", "power", "shin", "additive")
# tennis-data dates are match days; TML dates are event starts (a few days before the first
# round for some events, two weeks before a Grand Slam final)
DAYS_BEFORE_START = 4
DAYS_AFTER_START = 20
TABLE_VERSION = 1
MIN_OVERROUND = 0.97
MAX_OVERROUND = 1.35


# --------------------------------------------------------------------------- margin


def _clip(p: float) -> float:
    return min(1.0 - 1e-9, max(1e-9, p))


def logit(p: float) -> float:
    p = _clip(p)
    return math.log(p / (1.0 - p))


def valid_odds(odds_1: float | None, odds_2: float | None) -> bool:
    if not odds_1 or not odds_2 or not (1.0 < odds_1 < 1001.0 and 1.0 < odds_2 < 1001.0):
        return False
    return MIN_OVERROUND <= 1.0 / odds_1 + 1.0 / odds_2 <= MAX_OVERROUND


def _bisect(function, low: float, high: float, steps: int = 60) -> float:
    f_low = function(low)
    for _ in range(steps):
        middle = 0.5 * (low + high)
        f_middle = function(middle)
        if (f_middle > 0) == (f_low > 0):
            low, f_low = middle, f_middle
        else:
            high = middle
    return 0.5 * (low + high)


def fair_probability(odds_1: float, odds_2: float, method: str = "proportional") -> float:
    """P(player 1 wins) without the bookmaker margin.

    ``proportional``: 1/o normalised. ``power``: (1/o)**k with k chosen so the two sum to 1
    (shrinks longshots more: favourite-longshot bias). ``shin``: Shin's insider model for two
    outcomes. ``additive``: equal margin taken from both sides. Without a margin (or a
    negative one) all methods agree with ``proportional``.
    """
    q1, q2 = 1.0 / odds_1, 1.0 / odds_2
    booksum = q1 + q2
    if method == "proportional" or booksum <= 1.0:
        return q1 / booksum
    if method == "additive":
        return _clip(q1 - (booksum - 1.0) / 2.0)
    if method == "power":
        k = _bisect(lambda e: q1**e + q2**e - 1.0, 1.0, 20.0)
        return q1**k / (q1**k + q2**k)
    if method == "shin":

        def shares(z: float) -> tuple[float, float]:
            def one(q: float) -> float:
                return (math.sqrt(z * z + 4.0 * (1.0 - z) * q * q / booksum) - z) / (
                    2.0 * (1.0 - z)
                )

            return one(q1), one(q2)

        z = _bisect(lambda s: sum(shares(s)) - 1.0, 0.0, 0.99)
        p1, p2 = shares(z)
        return p1 / (p1 + p2)
    raise ValueError(f"Metodă de marjă necunoscută: {method!r}; permise: {MARGIN_METHODS}")


def blend_logit(
    model_logit: float,
    market_probability: float | None,
    weights: Sequence[float] | None,
) -> float:
    """``w_model * model_logit + w_market * logit(market)``; the model logit alone without a
    market price or weights."""
    if market_probability is None or not weights:
        return model_logit
    w_model, w_market = weights
    return w_model * model_logit + w_market * logit(market_probability)


def blend_probability(
    model_probability: float,
    market_probability: float | None,
    weights: Sequence[float] | None,
) -> float:
    z = blend_logit(logit(model_probability), market_probability, weights)
    return 1.0 / (1.0 + math.exp(-z))


def fit_blend_weights(
    model_logits: Sequence[float], market_logits: Sequence[float], l2: float = 1e-3
) -> tuple[float, float]:
    """(w_model, w_market) by Newton on winner-first out-of-sample pairs (no intercept).

    Rows are oriented winner first, so the target is always 1 and the fit is exactly the
    symmetric stack ``sigmoid(w_model * z_model + w_market * z_market)``. A tiny ridge keeps
    the solve stable when the two logits are nearly collinear.
    """
    import numpy as np

    x = np.column_stack([np.asarray(model_logits, float), np.asarray(market_logits, float)])
    if len(x) == 0:
        return (1.0, 0.0)
    w = np.array([0.5, 0.5])
    for _ in range(60):
        s = 0.5 * (1.0 + np.tanh(0.5 * (x @ w)))
        grad = -(x.T @ (1.0 - s)) + l2 * w
        hess = (x * (s * (1.0 - s))[:, None]).T @ x + l2 * np.eye(2)
        step = np.linalg.solve(hess, grad)
        w = w - step
        if float(np.abs(step).max()) < 1e-10:
            break
    return (float(w[0]), float(w[1]))


# --------------------------------------------------------------------------- names


def _normal(value: str | None) -> list[str]:
    from .model import normalize_name

    return normalize_name(value).split()


def _td_name(name: str) -> tuple[str, str]:
    """tennis-data "De Minaur A." -> ("deminaur", "a"); "Wang Xiy." -> ("wang", "xiy")."""
    from .model import _split_query

    surname, initials = _split_query(name)
    if not initials and len(surname) >= 2:
        # "Struff J L" or a missing dot: a trailing short token is the given-name initial
        if len(surname[-1]) <= 2:
            surname, initials = surname[:-1], [surname[-1]]
    return "".join(surname), "".join(initials)


def _tml_keys(full_name: str | None) -> list[tuple[str, str]]:
    """(joined surname, given names joined) readings of a TML full name, both orders."""
    tokens = _normal(full_name)
    readings = []
    for cut in range(1, len(tokens)):
        readings.append(("".join(tokens[cut:]), "".join(tokens[:cut])))  # given first
        readings.append(("".join(tokens[:cut]), "".join(tokens[cut:])))  # surname first
    if len(tokens) == 1:
        readings.append((tokens[0], ""))
    return readings


def _initials_ok(initials: str, given: str) -> bool:
    return not initials or not given or given.startswith(initials) or given[0] == initials[0]


# --------------------------------------------------------------------------- odds table


def _day(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _price(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(str(value).replace(",", ".")) if isinstance(value, str) else float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _parse_workbook(path: Path) -> list[tuple]:
    """[(day ordinal, winner, loser, {book: (winner price, loser price)})] of one workbook."""
    from openpyxl import load_workbook

    with warnings.catch_warnings():
        # openpyxl warns about an unknown extension in these files; it is harmless
        warnings.simplefilter("ignore", UserWarning)
        workbook = load_workbook(path, read_only=True, data_only=True)
        return _read_table(workbook)


def _read_table(workbook) -> list[tuple]:
    try:
        rows = workbook.worksheets[0].iter_rows(values_only=True)
        header = [str(cell).strip() if cell is not None else "" for cell in next(rows, ())]
        table = []
        for values in rows:
            if not values or all(value is None for value in values):
                continue
            row = dict(zip(header, values))
            day = _day(row.get("Date"))
            winner = str(row.get("Winner") or "").strip()
            loser = str(row.get("Loser") or "").strip()
            if day is None or not winner or not loser or winner == loser:
                continue
            prices = {}
            for book, (win_column, lose_column) in BOOKS.items():
                pair = _price(row.get(win_column)), _price(row.get(lose_column))
                if valid_odds(*pair):
                    prices[book] = pair
            table.append((day.toordinal(), winner, loser, prices))
        return table
    finally:
        workbook.close()


def load_odds(
    years: Iterable[int],
    tours: Iterable[str] = ODDS_TOURS,
    directory: str | Path = DEFAULT_ODDS_DIR,
    cache_dir: str | Path | None = None,
) -> dict[str, list[tuple]]:
    """{tour: [(day ordinal, winner, loser, prices)]} of the stored workbooks (missing skipped)."""
    result: dict[str, list[tuple]] = {}
    for tour in tours:
        table: list[tuple] = []
        for year in sorted(set(years)):
            path = Path(directory) / f"{tour}_{year}.xlsx"
            if not path.exists():
                continue
            table.extend(_cached_table(path, cache_dir))
        result[tour] = sorted(table, key=lambda item: item[0])
    return result


def _cached_table(path: Path, cache_dir: str | Path | None) -> list[tuple]:
    if cache_dir is None:
        return _parse_workbook(path)
    stat = path.stat()
    stamp = (TABLE_VERSION, str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    cache_file = Path(cache_dir) / f"odds_{path.stem}.pkl"
    try:
        with cache_file.open("rb") as handle:
            cached = pickle.load(handle)
        if cached.get("stamp") == stamp:
            return cached["table"]
    except (OSError, pickle.PickleError, EOFError, AttributeError, KeyError, TypeError):
        pass
    table = _parse_workbook(path)
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_file.with_suffix(f".{os.getpid()}.tmp")
        with temporary.open("wb") as handle:
            pickle.dump({"stamp": stamp, "table": table}, handle, pickle.HIGHEST_PROTOCOL)
        os.replace(temporary, cache_file)
    except OSError:
        pass  # the cache is only an optimisation
    return table


# --------------------------------------------------------------------------- join


def match_odds(rows: Sequence, tables: dict[str, list[tuple]]) -> tuple[dict, dict]:
    """({row position: {book: (winner price, loser price)}}, stats) for ATP/WTA rows.

    A tennis-data match joins the TML row of the same tour whose two players both read as its
    two names ("<surname> <initials>." on the TML full name, surname-first names too) and whose
    event started between ``DAYS_AFTER_START`` days before and ``DAYS_BEFORE_START`` days after
    the match day. Several candidate rows, or one row claimed by two matches, stay unmatched.
    """
    readings: dict[str, dict[tuple[str, str], set[str]]] = {}
    by_player: dict[tuple[str, str], list[tuple[int, int]]] = {}
    names: dict[tuple[str, str], str] = {}
    for position, row in enumerate(rows):
        tour = row["tour"]
        if tour not in tables or not row["winner_key"] or not row["loser_key"]:
            continue
        day = row["date"].toordinal()
        for side in ("winner", "loser"):
            key = row[f"{side}_key"]
            by_player.setdefault((tour, key), []).append((day, position))
            if (tour, key) not in names:
                names[tour, key] = row[f"{side}_name"] or ""
                index = readings.setdefault(tour, {})
                for surname, given in _tml_keys(row[f"{side}_name"]):
                    index.setdefault((surname, given[:1]), set()).add(key)
                    index.setdefault((surname, ""), set()).add(key)
    for items in by_player.values():
        items.sort()

    def candidates(tour: str, name: str) -> set[str]:
        surname, initials = _td_name(name)
        index = readings.get(tour, {})
        found = set(index.get((surname, initials[:1]), set())) if initials else set()
        if len(initials) > 1 and len(found) > 1:
            # "Wang Xiy." keeps Xiyu Wang and drops Xinyu Wang when both exist
            strict = {
                key
                for key in found
                if any(
                    s == surname and g.startswith(initials) for s, g in _tml_keys(names[tour, key])
                )
            }
            found = strict or found
        if not found:
            found = set(index.get((surname, ""), set()))
            if initials:
                found = {
                    key
                    for key in found
                    if any(
                        s == surname and _initials_ok(initials, g)
                        for s, g in _tml_keys(names[tour, key])
                    )
                }
        return found

    def loose(tour: str, name: str) -> set[str]:
        """Players sharing a surname word: "Osorio M.C." -> Camila Osorio, "Riske A." ->
        Alison Riske Amritraj, "Mpetshi G." -> Giovanni Mpetshi Perricard."""
        from .model import _split_query

        words = [word for word in _split_query(name)[0] if len(word) >= 3]
        return set().union(*(tokens.get(tour, {}).get(word, set()) for word in words))

    tokens: dict[str, dict[str, set[str]]] = {}
    for (tour, key), full in names.items():
        for word in _normal(full):
            tokens.setdefault(tour, {}).setdefault(word, set()).add(key)

    def search(tour: str, day: int, first: set[str], second: set[str]) -> list[int]:
        found = []
        for key in first:
            items = by_player.get((tour, key), [])
            start = bisect_left(items, (day - DAYS_AFTER_START, -1))
            for when, position in items[start:]:
                if when > day + DAYS_BEFORE_START:
                    break
                row = rows[position]
                other = row["loser_key"] if row["winner_key"] == key else row["winner_key"]
                if other in second and other != key and position not in found:
                    found.append(position)
        if len(found) > 1:
            # the same pair in consecutive events: the latest event started by the match day
            # (+1 for events dated the Monday after a Sunday start)
            starts = [rows[p]["date"].toordinal() for p in found]
            begun = [when for when in starts if when <= day + 1]
            if begun:
                latest = max(begun)
                found = [p for p, when in zip(found, starts) if when == latest]
        return found

    claimed: dict[int, dict] = {}
    conflicts: set[int] = set()
    stats = {
        tour: {"odds_rows": 0, "matched": 0, "ambiguous": 0, "no_row": 0, "loose": 0}
        for tour in tables
    }
    cache: dict[tuple[str, str], set[str]] = {}
    loose_cache: dict[tuple[str, str], set[str]] = {}
    for tour, table in tables.items():
        for day, winner, loser, prices in table:
            if not prices:
                continue
            stats[tour]["odds_rows"] += 1
            first = cache.setdefault((tour, winner), candidates(tour, winner))
            second = cache.setdefault((tour, loser), candidates(tour, loser))
            found = search(tour, day, first, second)
            if not found:
                # one name strict, the other sharing a surname word (married names, long
                # surnames shortened by tennis-data)
                loose_1 = loose_cache.setdefault((tour, winner), loose(tour, winner))
                loose_2 = loose_cache.setdefault((tour, loser), loose(tour, loser))
                found = list(
                    dict.fromkeys(
                        search(tour, day, first, loose_2) + search(tour, day, loose_1, second)
                    )
                )
                if len(found) == 1:
                    first, second = first | loose_1, second | loose_2
                    stats[tour]["loose"] += 1
            if len(found) != 1:
                stats[tour]["ambiguous" if found else "no_row"] += 1
                continue
            (position,) = found
            row = rows[position]
            straight = row["winner_key"] in first and row["loser_key"] in second
            swapped = row["winner_key"] in second and row["loser_key"] in first
            if straight == swapped:  # both names read as both players: no orientation
                stats[tour]["ambiguous"] += 1
                continue
            # prices follow the player: the TML winner is the tennis-data name that reads as it
            oriented = {
                book: (pair if straight else (pair[1], pair[0])) for book, pair in prices.items()
            }
            if position in claimed:
                conflicts.add(position)
            claimed[position] = oriented
    for position in conflicts:
        claimed.pop(position, None)
    for tour in stats:
        stats[tour]["matched"] = sum(1 for p in claimed if rows[p]["tour"] == tour)
    stats["conflicts"] = len(conflicts)
    return claimed, stats


def attach_odds(
    rows: list,
    source: str = "avg",
    *,
    directory: str | Path = DEFAULT_ODDS_DIR,
    cache_dir: str | Path | None = None,
    fallback: bool = True,
) -> tuple[list, dict]:
    """(rows with ``OddsRow`` where a price of ``source`` exists, join statistics).

    With ``fallback`` a missing ``source`` price falls back to the other books in the order
    avg, ps, b365 (the order of ``footypreds.evaluation.tennis_data``); ``max`` never stands in.
    """
    from .benchmark import OddsRow

    if source not in BOOKS:
        raise ValueError(f"Sursă de cote necunoscută: {source!r}; permise: {tuple(BOOKS)}")
    years = sorted({row["season"] for row in rows} | {row["date"].year for row in rows})
    tables = load_odds(years, ODDS_TOURS, directory, cache_dir)
    matched, stats = match_odds(rows, tables)
    order = [source] + ([b for b in ("avg", "ps", "b365") if b != source] if fallback else [])
    output = list(rows)
    per_group: dict[str, list[int]] = {}
    for position, prices in matched.items():
        pair = next((prices[book] for book in order if book in prices), None)
        if pair is None:
            continue
        row = rows[position]
        output[position] = OddsRow(row._values, pair[0], pair[1])
        if not row["is_walkover"]:
            per_group.setdefault(f"{row['tour']}/{row['season']}", [0, 0])[0] += 1
    for row in rows:
        if row["tour"] in ODDS_TOURS and not row["is_walkover"]:
            per_group.setdefault(f"{row['tour']}/{row['season']}", [0, 0])[1] += 1
    stats["groups"] = {
        key: {"with_odds": hit, "played": total, "rate": hit / total if total else None}
        for key, (hit, total) in sorted(per_group.items())
        if hit
    }
    return output, stats
