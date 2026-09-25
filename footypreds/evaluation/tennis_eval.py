"""Walk-forward, blind evaluation of the tennis model against bookmaker prices.

Data: tennis-data.co.uk ATP + WTA singles (evaluation/tennis_data.py). Protocol (PROTOCOL):

- walk forward one day at a time: every game of day D is predicted from an Elo book that has
  seen only days before D (the analyzer's kickoff - 3h rule, since all games of one date share
  a kickoff); the game passed to the model has its score hidden; the day's results are added
  only after every game of the day was predicted;
- scored games: completed singles with prices on both players (retirements and walkovers are
  void for bets and are left out of the metrics; retirements still update the ratings with
  weight `retired_k`);
- the market reference is the margin-free average closing price (AvgW/AvgL);
- parameters are tuned ONLY on the validation year; the test year is evaluated once per model
  version with the frozen `sports.tennis.PARAMS` (`report-test.json` is the lock).

Commands (from the repository root):

    python -m footypreds.evaluation.tennis_eval --download    # fetch the workbooks
    python -m footypreds.evaluation.tennis_eval --validate    # tune + validation report
    python -m footypreds.evaluation.tennis_eval               # locked test, once per version
"""

import argparse
import json
import math
import sys
from dataclasses import asdict, replace
from datetime import datetime, timezone
from itertools import groupby

from footypreds.evaluation.tennis_data import DATA_DIR, RAW_DIR, download, load_tennis_matches
from footypreds.sports import common as c
from footypreds.sports import tennis

PROTOCOL = {
    "name": "Tenis ATP+WTA: walk-forward orb, tuning pe 2024, test blocat 2025",
    "history_years": list(range(2013, 2024)),
    "validation_year": 2024,
    "test_year": 2025,
    "tours": ["atp", "wta"],
    "prediction_time": "ziua meciului; sunt vizibile doar rezultatele zilelor anterioare",
    "scored": "meciuri de simplu terminate normal, cu cote pentru ambii jucători",
    "market": "cote medii de închidere AvgW/AvgL (Oddsportal via tennis-data.co.uk), fără marjă",
    "primary_metric": "log loss pe câștigătorul meciului (predicția finală, model + piață)",
}
REPORTS = {"validation": DATA_DIR / "report-validation.json", "test": DATA_DIR / "report-test.json"}
EPS = 1e-12
ELO_GRID = {
    "k_base": [100.0, 125.0, 150.0, 200.0, 250.0, 300.0],
    "k_shape": [0.3, 0.4, 0.5],
    "surface_weight": [0.0, 0.1, 0.25, 0.5, 0.75],
    "mov": [0.0, 0.1, 0.25, 0.5],
    "retired_k": [0.0, 0.5, 1.0],
    "idle_half_life": [math.inf, 1095.0, 730.0, 365.0, 180.0],
}
MARKET_WEIGHTS = [round(0.5 + 0.05 * i, 2) for i in range(10)] + [0.975, 1.0]
EXPERIENCE = [0.0, 10.0, 20.0, 40.0, 80.0]
SPREADS = [round(0.1 * i, 1) for i in range(17)]
SERVE_MEN = [0.54, 0.56, 0.58, 0.60, 0.62, 0.64, 0.66]
SERVE_WOMEN = [0.48, 0.50, 0.52, 0.54, 0.56, 0.58]
GAMES_STEP = 1  # every game with a known total in reports
TUNE_GAMES_STEP = 5  # every 5th game while tuning the serve bases (the point model is slow)


def blind(match):
    """The fixture as the model sees it before the start: no score, no status."""
    return match.model_copy(
        update={"status": "scheduled", "home_goals": None, "away_goals": None, "finish_type": ""}
    )


def games_total(match):
    games = tennis.source_hint(match, "games")
    try:
        return sum(int(a) + int(b) for a, b in (s.split("-") for s in games.split()))
    except ValueError:
        return None


def walk_forward(matches, params, years):
    """Blind day-by-day predictions of the completed games in `years` (one record each)."""
    years = set(years)
    book = tennis.EloBook(params)
    records = []
    ordered = sorted(matches, key=lambda m: (m.kickoff, m.id))
    for kickoff, day in groupby(ordered, key=lambda m: m.kickoff):
        day = list(day)
        if kickoff.year in years:
            for match in day:
                if match.status != "finished" or match.finish_type:
                    continue
                fixture = blind(match)
                view = tennis.elo_view(book, fixture)
                priced = c.two_way(fixture.odds, "1", "2")
                records.append(
                    {
                        "id": match.id,
                        "date": kickoff.date().isoformat(),
                        "year": kickoff.year,
                        "tour": tennis.source_hint(match, "tour") or "",
                        "women": tennis.is_women(match),
                        "best_of": tennis.best_of(match),
                        "elo": view["probability"],
                        "home_played": view["home_played"],
                        "away_played": view["away_played"],
                        "market": priced[0] if priced else None,
                        "odds": (match.odds.get("1"), match.odds.get("2")),
                        "home_won": match.home_goals > match.away_goals,
                        "sets": (match.home_goals, match.away_goals),
                        "games": games_total(match),
                    }
                )
        for match in day:
            book.update(match)
    return records


def final_probability(record, params):
    weight = tennis.model_weight(record, params)
    return tennis.blend(record["elo"], record["market"], weight)


def log_loss(p, won):
    p = min(1 - EPS, max(EPS, p))
    return -math.log(p if won else 1 - p)


def winner_metrics(pairs):
    """pairs: [(p_home, home_won)] -> log loss, Brier, accuracy."""
    n = len(pairs)
    if not n:
        return {"n": 0}
    return {
        "n": n,
        "log_loss": sum(log_loss(p, won) for p, won in pairs) / n,
        "brier": sum((p - won) ** 2 for p, won in pairs) / n,
        "accuracy": sum((p >= 0.5) == won for p, won in pairs) / n,
    }


def calibration(pairs, bins=10):
    """Reliability table on the favourite's probability (0.5-1 in `bins` equal bins)."""
    table = []
    width = 0.5 / bins
    for i in range(bins):
        low, high = 0.5 + i * width, 0.5 + (i + 1) * width
        rows = [
            (max(p, 1 - p), won if p >= 0.5 else not won)
            for p, won in pairs
            if low <= max(p, 1 - p) < high or (i == bins - 1 and max(p, 1 - p) == 1)
        ]
        if rows:
            table.append(
                {
                    "bin": f"{low:.2f}-{high:.2f}",
                    "n": len(rows),
                    "predicted": sum(p for p, _ in rows) / len(rows),
                    "observed": sum(w for _, w in rows) / len(rows),
                }
            )
    return table


def set_metrics(records, probabilities, spread):
    """Exact set-score log loss and straight-sets Brier, per best-of."""
    out = {}
    for sets in (3, 5):
        rows = [(r, p) for r, p in zip(records, probabilities) if r["best_of"] == sets]
        if not rows:
            continue
        loss = brier = 0.0
        for record, p in rows:
            scores = tennis.set_scores(tennis.set_probability(p, sets, spread), sets, spread)
            h, a = record["sets"]
            loss -= math.log(max(EPS, scores.get((h, a), 0.0)))
            straight = sum(prob for (x, y), prob in scores.items() if min(x, y) == 0)
            brier += (straight - (min(h, a) == 0)) ** 2
        out[f"bo{sets}"] = {
            "n": len(rows),
            "set_score_log_loss": loss / len(rows),
            "straight_sets_brier": brier / len(rows),
            "straight_sets_observed": sum(min(r["sets"]) == 0 for r, _ in rows) / len(rows),
        }
    return out


def games_metrics(records, probabilities, params, step=1):
    """Total-games log loss, mean error and bias on every `step`-th game with a known total."""
    rows = [(r, p) for r, p in zip(records, probabilities) if r["games"] is not None][::step]
    if not rows:
        return {"n": 0}
    loss = error = bias = 0.0
    for record, p in rows:
        base = params.serve_women if record["women"] else params.serve_men
        totals, _ = tennis.games_totals(p, record["best_of"], base, params.set_spread)
        mean = sum(g * prob for g, prob in totals.items())
        loss -= math.log(max(EPS, totals.get(record["games"], 0.0)))
        error += abs(mean - record["games"])
        bias += mean - record["games"]
    n = len(rows)
    return {"n": n, "log_loss": loss / n, "mae": error / n, "bias": bias / n}


def value_bets(records, probabilities, edge=0.03):
    """Flat 1-unit bets where p * average price - 1 > edge (information only)."""
    staked = returned = 0.0
    wins = 0
    for record, p in zip(records, probabilities):
        for side, prob in (("1", p), ("2", 1 - p)):
            price = record["odds"][0 if side == "1" else 1]
            if price and prob * price - 1 > edge:
                won = record["home_won"] == (side == "1")
                staked += 1
                returned += price if won else 0.0
                wins += won
    return {
        "bets": int(staked),
        "won": wins,
        "roi": (returned - staked) / staked if staked else None,
        "edge": edge,
    }


def evaluate(records, params, games_step=None):
    """Every metric of one scored period."""
    priced = [r for r in records if r["market"] is not None]
    final = [final_probability(r, params) for r in priced]
    report = {
        "matches": len(priced),
        "unpriced_skipped": len(records) - len(priced),
        "final": winner_metrics(list(zip(final, (r["home_won"] for r in priced)))),
        "elo_only": winner_metrics([(r["elo"], r["home_won"]) for r in priced]),
        "market_only": winner_metrics([(r["market"], r["home_won"]) for r in priced]),
        "calibration": calibration(list(zip(final, (r["home_won"] for r in priced)))),
        "by_tour": {},
        "sets": set_metrics(priced, final, params.set_spread),
        "games": games_metrics(priced, final, params, games_step or GAMES_STEP),
        "value_bets": value_bets(priced, final),
    }
    for tour in ("atp", "wta"):
        rows = [(p, r) for p, r in zip(final, priced) if r["tour"] == tour]
        report["by_tour"][tour] = {
            "final": winner_metrics([(p, r["home_won"]) for p, r in rows]),
            "market_only": winner_metrics([(r["market"], r["home_won"]) for _, r in rows]),
        }
    return report


def elo_loss(matches, params, years):
    records = [r for r in walk_forward(matches, params, years)]
    return winner_metrics([(r["elo"], r["home_won"]) for r in records])["log_loss"], records


def tune(matches, year, base=None, passes=2, grid=None, log=print):
    """Coordinate descent on the validation year only; returns (params, trace)."""
    params = base or tennis.PARAMS
    grid = grid or ELO_GRID
    trace = []
    best, records = elo_loss(matches, params, [year])
    for _ in range(passes):
        improved = False
        for name, values in grid.items():
            for value in values:
                if getattr(params, name) == value:
                    continue
                candidate = replace(params, **{name: value})
                loss, rows = elo_loss(matches, candidate, [year])
                trace.append({"stage": "elo", name: value, "log_loss": loss})
                if loss < best - 1e-6:
                    best, params, records, improved = loss, candidate, rows, True
                    log(f"  elo {name}={value}: {loss:.5f}")
        if not improved:
            break
    priced = [r for r in records if r["market"] is not None]
    outcomes = [r["home_won"] for r in priced]
    best_blend = None
    for weight in MARKET_WEIGHTS:
        for experience in EXPERIENCE:
            candidate = replace(params, market_weight=weight, experience=experience)
            p = [final_probability(r, candidate) for r in priced]
            loss = winner_metrics(list(zip(p, outcomes)))["log_loss"]
            trace.append(
                {
                    "stage": "blend",
                    "market_weight": weight,
                    "experience": experience,
                    "log_loss": loss,
                }
            )
            if best_blend is None or loss < best_blend[0] - 1e-9:
                best_blend = (loss, candidate)
    params = best_blend[1]
    log(f"  blend market_weight={params.market_weight} experience={params.experience}")
    final = [final_probability(r, params) for r in priced]
    best_spread = None
    for spread in SPREADS:
        metrics = set_metrics(priced, final, spread)
        n = sum(m["n"] for m in metrics.values())
        loss = sum(m["set_score_log_loss"] * m["n"] for m in metrics.values()) / n
        trace.append({"stage": "sets", "set_spread": spread, "log_loss": loss})
        if best_spread is None or loss < best_spread[0] - 1e-9:
            best_spread = (loss, spread)
    params = replace(params, set_spread=best_spread[1])
    log(f"  sets set_spread={params.set_spread}")
    for field, values, women in (
        ("serve_men", SERVE_MEN, False),
        ("serve_women", SERVE_WOMEN, True),
    ):
        subset = [r for r in priced if r["women"] == women]
        probs = [final_probability(r, params) for r in subset]
        best_serve = None
        for value in values:
            candidate = replace(params, **{field: value})
            metrics = games_metrics(subset, probs, candidate, step=TUNE_GAMES_STEP)
            trace.append({"stage": "games", field: value, **metrics})
            if best_serve is None or metrics["log_loss"] < best_serve[0] - 1e-9:
                best_serve = (metrics["log_loss"], candidate)
        params = best_serve[1]
        log(f"  games {field}={getattr(params, field)}")
    return params, trace


def params_json(params):
    return {
        k: (None if isinstance(v, float) and math.isinf(v) else v)
        for k, v in asdict(params).items()
    }


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load(years):
    matches = load_tennis_matches(years=years, tours=tuple(PROTOCOL["tours"]))
    if not matches:
        raise SystemExit(
            f"Nu există date în {RAW_DIR}. Rulează întâi: "
            "python -m footypreds.evaluation.tennis_eval --download"
        )
    return matches


def run_validation(log=print):
    year = PROTOCOL["validation_year"]
    matches = load(PROTOCOL["history_years"] + [year])
    log(f"Tuning pe {year} ({len(matches)} meciuri încărcate)...")
    tuned, trace = tune(matches, year, log=log)
    frozen = evaluate(walk_forward(matches, tennis.PARAMS, [year]), tennis.PARAMS)
    report = {
        "protocol": PROTOCOL,
        "version": tennis.VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tuned_params": params_json(tuned),
        "current_params": params_json(tennis.PARAMS),
        "tuned": evaluate(walk_forward(matches, tuned, [year]), tuned),
        "current": frozen,
        "trace": trace,
    }
    write(REPORTS["validation"], report)
    return report


def run_test(force=False, log=print):
    path = REPORTS["test"]
    if path.exists() and not force:
        previous = json.loads(path.read_text(encoding="utf-8"))
        if previous.get("version") == tennis.VERSION and previous.get("params") == params_json(
            tennis.PARAMS
        ):
            log("Testul blocat a rulat deja pentru această versiune; se afișează raportul salvat.")
            return previous
    year = PROTOCOL["test_year"]
    matches = load(PROTOCOL["history_years"] + [PROTOCOL["validation_year"], year])
    report = {
        "protocol": PROTOCOL,
        "version": tennis.VERSION,
        "params": params_json(tennis.PARAMS),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "test": evaluate(walk_forward(matches, tennis.PARAMS, [year]), tennis.PARAMS),
    }
    write(path, report)
    return report


def summary(section):
    lines = [f"  meciuri: {section['matches']}"]
    for name in ("final", "elo_only", "market_only"):
        m = section[name]
        lines.append(
            f"  {name:12s} log loss {m['log_loss']:.4f}  Brier {m['brier']:.4f}  "
            f"acuratețe {m['accuracy']:.3f}"
        )
    for key, m in section["sets"].items():
        lines.append(
            f"  {key}: log loss scor seturi {m['set_score_log_loss']:.4f}, "
            f"2-0/3-0 observat {m['straight_sets_observed']:.3f}"
        )
    g = section["games"]
    if g.get("n"):
        lines.append(
            f"  game-uri: log loss {g['log_loss']:.4f}, MAE {g['mae']:.2f}, bias {g['bias']:+.2f}"
        )
    v = section["value_bets"]
    if v["bets"]:
        lines.append(f"  pariuri valoare (info): {v['bets']} pariuri, ROI {v['roi']:+.3f}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluare walk-forward a modelului de tenis.")
    parser.add_argument("--download", action="store_true", help="descarcă fișierele lipsă")
    parser.add_argument("--refresh", action="store_true", help="re-descarcă toate fișierele")
    parser.add_argument("--validate", action="store_true", help="tuning + raport de validare")
    parser.add_argument("--force", action="store_true", help="re-rulează testul blocat")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Romanian text on a Windows console
    if args.download or args.refresh:
        entries = download(refresh=args.refresh)
        print(f"Descărcate: {len(entries)} fișiere în {RAW_DIR}")
        return 0
    if args.validate:
        report = run_validation()
        print("Validare, parametri actuali:\n" + summary(report["current"]))
        print("Validare, parametri propuși:\n" + summary(report["tuned"]))
        print(json.dumps(report["tuned_params"], indent=2))
        return 0
    report = run_test(force=args.force)
    print(f"Test blocat {PROTOCOL['test_year']} ({report['version']}):\n" + summary(report["test"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
