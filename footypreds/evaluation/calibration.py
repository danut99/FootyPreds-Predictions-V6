"""Goals-market reliability and the goals calibration fit (VALIDATION season only).

python -m footypreds.evaluation.calibration              # 16 leagues when downloaded, else top 5
python -m footypreds.evaluation.calibration --top5       # the 5 benchmark leagues only
python -m footypreds.evaluation.calibration --fit        # also fit the totals parameters

What is fitted (engine.analyzer.Params, docs/MODEL.md):
- ``totals_intercept``/``totals_slope``: a Platt map on logit P(over 2.5) of the matrix BEFORE
  the totals step (the 1X2-market variant, as in the app), maximum likelihood.
- ``totals_market_weight``: weight of the margin-free over/under 2.5 price in the logarithmic
  pool with the calibrated probability, the log-loss minimum on a 0.05 grid.
- ``recommend.MAX_VALUE``: the cap on probability x odds with the best validation ROI of the
  model's own (uncalibrated) over/under 2.5 legs.

Both use only predictions of the validation season (``protocol.json`` "validation_seasons"),
made walk-forward with history before each match date. The locked test season is refused.
"""

import argparse
import hashlib
import json
import math
from dataclasses import replace

from footypreds.engine import PARAMS
from footypreds.engine.markets import outcome, platt, pool_binary

BANDS = (0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
# Markets whose reliability is reported (all derived from the same score matrix).
GOAL_KEYS = ("over15", "under15", "over25", "under25", "over35", "under35", "btts", "no_btts")
OTHER_KEYS = ("1X", "X2", "12")
WEIGHT_GRID = tuple(round(0.05 * i, 2) for i in range(21))
IDENTITY = {"totals_intercept": 0.0, "totals_slope": 1.0, "totals_market_weight": 0.0}


def log_loss(observations):
    if not observations:
        return None
    total = 0.0
    for p, y in observations:
        p = min(1 - 1e-15, max(1e-15, p))
        total -= math.log(p if y else 1 - p)
    return total / len(observations)


def reliability(observations, bands=BANDS, minimum=1):
    """Mean prediction vs frequency per probability band, ECE, log loss and Brier."""
    rows = []
    for index, (low, high) in enumerate(zip(bands, bands[1:], strict=False)):
        last = index == len(bands) - 2
        group = [(p, y) for p, y in observations if low <= p < high or (last and p == high)]
        if len(group) >= minimum:
            rows.append(
                {
                    "range": f"{low:.0%}–{high:.0%}",
                    "count": len(group),
                    "predicted": sum(p for p, _ in group) / len(group),
                    "actual": sum(y for _, y in group) / len(group),
                }
            )
    count = len(observations)
    return {
        "count": count,
        "mean_predicted": sum(p for p, _ in observations) / count if count else None,
        "mean_actual": sum(y for _, y in observations) / count if count else None,
        "log_loss": log_loss(observations),
        "brier": sum((p - y) ** 2 for p, y in observations) / count if count else None,
        "ece": sum(abs(b["predicted"] - b["actual"]) * b["count"] for b in rows) / count
        if count
        else None,
        "bands": rows,
    }


def fit_platt(observations, iterations=100, ridge=1e-6):
    """(intercept, slope) of P(y) = sigmoid(a + b * logit(p)): damped Newton on the log loss.

    Every step is halved until the (ridge-penalized) log loss falls, so it cannot diverge. The
    slope must stay positive for a monotone map: a non-positive fit, too few rows or a single
    outcome fall back to the identity (0, 1).
    """
    from footypreds.engine.markets import logit, sigmoid

    xs = [(logit(p), 1.0 if y else 0.0) for p, y in observations]
    if len(xs) < 20 or len({y for _, y in xs}) < 2 or len({x for x, _ in xs}) < 2:
        return 0.0, 1.0

    def loss(a, b):
        total = ridge * (a * a + (b - 1) ** 2) / 2
        for x, y in xs:
            z = a + b * x
            # log(1 + e^z) - y z, computed stably.
            total += max(z, 0) + math.log1p(math.exp(-abs(z))) - y * z
        return total

    a, b = 0.0, 1.0
    current = loss(a, b)
    for _ in range(iterations):
        g_a, g_b = ridge * a, ridge * (b - 1)
        h_aa = h_bb = ridge
        h_ab = 0.0
        for x, y in xs:
            q = sigmoid(a + b * x)
            w = q * (1 - q)
            g_a += q - y
            g_b += (q - y) * x
            h_aa += w
            h_ab += w * x
            h_bb += w * x * x
        det = h_aa * h_bb - h_ab * h_ab
        if det <= 0:
            break
        step_a = (h_bb * g_a - h_ab * g_b) / det
        step_b = (h_aa * g_b - h_ab * g_a) / det
        factor = 1.0
        while factor > 1e-10:
            trial_a, trial_b = a - factor * step_a, b - factor * step_b
            trial = loss(trial_a, trial_b)
            if trial <= current:
                break
            factor /= 2
        else:
            break
        moved = abs(trial_a - a) + abs(trial_b - b)
        a, b, current = trial_a, trial_b, trial
        if moved < 1e-12:
            break
    if not (math.isfinite(a) and math.isfinite(b)) or b <= 0:
        return 0.0, 1.0
    return a, b


def total_goals_over(row, line=2.5):
    return row["home_goals"] + row["away_goals"] > line


def raw_observations(rows, variant="totals_market"):
    """(raw P(over 2.5) before the totals step, happened) of every row."""
    return [(r[variant]["model_over25"], total_goals_over(r)) for r in rows if r.get(variant)]


def pooled_observations(rows, intercept, slope, weight, variant="totals_market"):
    """(target P(over 2.5) the matrix would get, happened) for rows with an O/U price."""
    output = []
    for r in rows:
        totals = r.get(variant)
        if not totals or totals.get("market_over25") is None:
            continue
        calibrated = platt(totals["model_over25"], intercept, slope)
        output.append(
            (pool_binary(calibrated, totals["market_over25"], weight), total_goals_over(r))
        )
    return output


def fit_totals(rows, grid=WEIGHT_GRID):
    """Totals parameters from validation rows computed with the IDENTITY totals parameters."""
    for r in rows:
        totals = r.get("totals_market")
        if totals and abs(totals["over25"] - totals["model_over25"]) > 1e-12:
            raise ValueError("Rows must come from the identity totals parameters.")
    intercept, slope = fit_platt(raw_observations(rows))
    losses = {w: log_loss(pooled_observations(rows, intercept, slope, w)) for w in grid}
    usable = {w: v for w, v in losses.items() if v is not None}
    weight = min(usable, key=lambda w: (round(usable[w], 6), w)) if usable else 0.0
    return {
        "totals_intercept": round(intercept, 4),
        "totals_slope": round(slope, 4),
        "totals_market_weight": weight,
        "fit_rows": len(raw_observations(rows)),
        "pool_rows": len(pooled_observations(rows, intercept, slope, weight)),
        "pool_log_loss": {str(w): round(v, 6) for w, v in usable.items()},
    }


# --- value cap (recommend.MAX_VALUE) -----------------------------------------------------------
VALUE_EDGES = (0.95, 1.0, 1.05, 1.1, 1.15, 1.2, 1.3, math.inf)
VALUE_CAPS = (1.0, 1.05, 1.1, 1.15, 1.2, 1.3, math.inf)


def value_legs(rows, odds_by_id, keys=("over25", "under25"), leg_odds=(1.08, 4.0)):
    """(value = p x odds, won, odds) of every priced leg the model's own view would allow.

    Use rows computed with the IDENTITY totals parameters: the question is what happens when
    the model disagrees with the price, so the market must not already be in the probability.
    """
    output = []
    for r in rows:
        prices = odds_by_id.get(r["id"], {})
        for key in keys:
            price = prices.get(key)
            if not price or not leg_odds[0] <= price <= leg_odds[1]:
                continue
            probability = r["markets_market"][key]
            won = outcome(key, r["home_goals"], r["away_goals"])
            output.append((probability * price, won, price))
    return output


def roi(legs):
    return sum((price if won else 0.0) - 1 for _, won, price in legs) / len(legs) if legs else None


def value_table(legs, edges=VALUE_EDGES):
    table = []
    for low, high in zip(edges, edges[1:], strict=False):
        group = [leg for leg in legs if low <= leg[0] < high]
        if group:
            table.append(
                {
                    "range": f"{low:.2f}–{high:.2f}",
                    "count": len(group),
                    "roi": roi(group),
                    "hit_rate": sum(won for _, won, _ in group) / len(group),
                }
            )
    return table


def fit_value_cap(legs, floor=0.95, caps=VALUE_CAPS):
    """The cap c maximizing the validation ROI of legs with floor <= value <= c (ties: lower)."""
    scores = {}
    for cap in caps:
        chosen = [leg for leg in legs if floor <= leg[0] <= cap]
        if chosen:
            scores[cap] = roi(chosen)
    if not scores:
        return None, {}
    best = max(scores, key=lambda cap: (round(scores[cap], 6), -cap))
    return best, {str(cap): round(value, 4) for cap, value in scores.items()}


# --- validation data --------------------------------------------------------------------------


def calibration_protocol(protocol, leagues):
    """The benchmark protocol with another league list (history/validation seasons unchanged)."""
    return protocol | {"leagues": list(leagues)}


def validation_records(protocol, top5=False):
    """(records, leagues, sha256) for the validation run: benchmark + the extra leagues."""
    from footypreds.evaluation.dataset import load

    records, _ = load()
    leagues = list(protocol["leagues"])
    if not top5:
        try:
            from footypreds.evaluation.sim_datasets import load_extra_records

            extra = load_extra_records()
        except (OSError, ValueError):
            extra = []
        allowed = set(protocol.get("calibration", {}).get("leagues", []))
        known = {r["match"]["id"] for r in records}
        added = [r for r in extra if r["league_code"] in allowed and r["match"]["id"] not in known]
        records = records + added
        leagues += sorted({r["league_code"] for r in added} - set(leagues))
    digest = hashlib.sha256()
    for r in sorted(records, key=lambda r: r["match"]["id"]):
        if r["league_code"] in leagues:
            digest.update(json.dumps(r, sort_keys=True).encode())
    return records, leagues, digest.hexdigest()


def validation_rows(protocol, params=PARAMS, top5=False, holdout=None, records=None):
    """Walk-forward predictions of the VALIDATION season (never the locked test season).

    `records` (tests): use these instead of the downloaded archives. `evaluate` only loads the
    seasons up to the holdout, so later seasons cannot reach the fit.
    """
    from footypreds.evaluation.run import evaluate

    holdout = holdout or protocol["validation_seasons"][-1]
    if holdout not in protocol["validation_seasons"] or holdout == protocol["test_season"]:
        raise ValueError("Calibration is fitted on the validation season only.")
    if records is None:
        records, leagues, digest = validation_records(protocol, top5)
    else:
        leagues, digest = list(protocol["leagues"]), None
    _, rows = evaluate(
        records,
        calibration_protocol(protocol, leagues),
        holdout=holdout,
        params=params,
        baseline=False,
        market=True,
    )
    return rows, leagues, digest


def report(rows):
    """Reliability of every goals market: model (no odds), model + market, bookmaker."""
    output = {}
    for field in ("markets", "markets_market"):
        output[field] = {
            key: reliability(
                [(r[field][key], outcome(key, r["home_goals"], r["away_goals"])) for r in rows]
            )
            for key in GOAL_KEYS + OTHER_KEYS
        }
    bookmaker = [
        (r["bookmaker_over25"], total_goals_over(r)) for r in rows if r["bookmaker_over25"]
    ]
    output["bookmaker_over25"] = reliability(bookmaker)
    output["same_rows_over25"] = {
        "model": reliability(
            [(r["markets"]["over25"], total_goals_over(r)) for r in rows if r["bookmaker_over25"]]
        ),
        "model_market": reliability(
            [
                (r["markets_market"]["over25"], total_goals_over(r))
                for r in rows
                if r["bookmaker_over25"]
            ]
        ),
        "bookmaker": output["bookmaker_over25"],
    }
    return output


def print_report(summary, keys=GOAL_KEYS + OTHER_KEYS):
    for field in ("markets", "markets_market"):
        print(f"== {field}")
        for key in keys:
            metric = summary[field][key]
            bands = "; ".join(
                f"{b['range']}: {b['count']} {b['predicted']:.3f}/{b['actual']:.3f}"
                for b in metric["bands"]
                if b["count"] >= 15
            )
            print(
                f"{key:8s} mean {metric['mean_predicted']:.3f}/{metric['mean_actual']:.3f} "
                f"ll {metric['log_loss']:.4f} ece {metric['ece']:.4f} | {bands}"
            )
    same = summary["same_rows_over25"]
    print(
        "over25 same rows: "
        + ", ".join(f"{k} ll {v['log_loss']:.4f} ece {v['ece']:.4f}" for k, v in same.items())
    )


def tune_totals(protocol, top5=False, records=None):
    """Everything the goals calibration fits, from identity-parameter validation predictions."""
    digest = None
    if records is None:
        records, leagues, digest = validation_records(protocol, top5)
        protocol = calibration_protocol(protocol, leagues)
    rows, leagues, _ = validation_rows(
        protocol, params=replace(PARAMS, **IDENTITY), top5=top5, records=records
    )
    odds_by_id = {r["match"]["id"]: r.get("reference_odds", {}) for r in records}
    legs = value_legs(rows, odds_by_id)
    cap, cap_scores = fit_value_cap(legs)
    return {
        "season": protocol["validation_seasons"][-1],
        "leagues": leagues,
        "records_sha256": digest,
        "matches": len(rows),
        **fit_totals(rows),
        "max_value": cap,
        "max_value_roi": cap_scores,
        "value_table": value_table(legs),
    }


def main():
    from footypreds.evaluation.run import PROTOCOL

    parser = argparse.ArgumentParser(description="Calibrarea piețelor de goluri (validare).")
    parser.add_argument("--top5", action="store_true", help="only the 5 benchmark leagues")
    parser.add_argument("--fit", action="store_true", help="fit the totals parameters")
    args = parser.parse_args()
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    print("Current params:", json.dumps({k: getattr(PARAMS, k) for k in IDENTITY}), flush=True)
    rows, leagues, digest = validation_rows(protocol, top5=args.top5)
    print(f"Validation rows: {len(rows)}; leagues {leagues}; records sha256 {digest}", flush=True)
    print_report(report(rows))
    if args.fit:
        print("FIT", json.dumps(tune_totals(protocol, args.top5), default=str), flush=True)


if __name__ == "__main__":
    main()
