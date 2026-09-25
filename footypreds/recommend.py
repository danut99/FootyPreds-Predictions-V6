"""Daily AI recommendations across sports: the safest ticket for each target odds.

The user only chooses the target odds (and optionally the sports). There is no minimum
probability: for a target T the optimizer picks the combination of real, pre-match legs whose
product of probabilities is the highest while the product of the quoted odds stays within
[0.93 T, 1.12 T], with at most one leg per match (and per team).

Pipeline for one day (docs/CONTRACTS.md §10.2):
1. `collect`: the day's fixtures of every requested sport (app.state.day_fixtures), the most
   promising ones enriched within a strict budget (app.state.enrich: H2H, standings and every
   quoted market price), then every upcoming priced fixture analysed (app.state.cache).
2. `eligible_legs`: shared candidate legs (sports.legs.candidate_legs: pre-match, grade A-C,
   selectable, real price) inside the odds band, without clearly negative value, without
   refundable (push) lines, and without legs where the model claims far more than the price
   (p * odds > MAX_VALUE: a strong disagreement with the market that validation never backed).
3. `optimize`: dynamic programme over matches with discretised log-odds states, pruned by
   bounds that never drop the optimum (branch and bound).
4. Persistence in `reco_*` tables and settlement of the stored tickets from stored results.
"""

import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from starlette.concurrency import run_in_threadpool

from footypreds.competitions import priority
from footypreds.engine import canonical
from footypreds.provider import ProviderError
from footypreds.sports import SPORTS
from footypreds.sports.legs import candidate_legs, settle_leg, settled_odds, ticket, ticket_status
from footypreds.sports.settle import can_push

log = logging.getLogger(__name__)

TARGETS = (2.0, 5.0, 10.0, 100.0)
# Maximum legs for the standard targets; other targets interpolate on the log scale.
MAX_LEGS = {2.0: 3, 5.0: 5, 10.0: 7, 100.0: 14}
LEGS_LIMIT = 15
# Accepted total odds, relative to the target.
ODDS_WINDOW = (0.93, 1.12)
# Leg prices considered at all: very short prices add risk without adding odds, long prices
# are lotteries.
LEG_ODDS = (1.08, 4.0)
# Legs whose estimated value p * odds is below this are clearly bad bets and are skipped.
MIN_VALUE = 0.95
# ... and legs above this are skipped too: the model then disagrees with the price by more than
# the bookmaker margin, with no measured evidence that it is right. On the 2024-25 validation
# season (16 leagues, the model's own over/under 2.5 view) legs with p * odds in 0.95-1.05
# returned -4.6%, all legs above 1.05 -9.0%; no market has shown an edge, so no exception.
# A conservative heuristic, not a fitted optimum: it is the best of 7 candidate caps on those
# legs, but the gaps between caps are about one standard error of the ROI (~1.6 pp), and it was
# measured only on football over/under 2.5 (`python -m footypreds.evaluation.tune --totals`,
# docs/MODEL.md). Other markets and sports inherit it without their own measurement.
MAX_VALUE = 1.05
# Safest single picks need a price worth staking.
SINGLE_MIN_ODDS = 1.2
SINGLES = 10
# FlashScore enrichment (H2H + standings + all market prices) per sport and request.
ENRICH_BUDGET = 8
# Fixtures analysed per sport and day (priority order: popular competitions first).
ANALYSIS_LIMIT = 150
# Width of a log-odds state of the dynamic programme.
BUCKET = 0.005
EPS = 1e-12
THRESHOLD = 0.85
GRADE_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}
DISCLAIMER = "Estimări statistice, nu garanții. 18+."
ASSUMPTION = (
    "Probabilitatea biletului este produsul probabilităților selecțiilor "
    "(presupune că meciurile sunt independente)."
)


def utcnow():
    """Current time; tests replace it to freeze the clock."""
    return datetime.now(timezone.utc)


# --- optimizer ------------------------------------------------------------------------------


def auto_max_legs(target):
    """Max legs for a target: the table above, interpolated on log(target) elsewhere."""
    points = sorted(MAX_LEGS.items())
    x = math.log(max(target, 1.0001))
    xs = [math.log(t) for t, _ in points]
    ys = [n for _, n in points]
    if x <= xs[0]:
        value = ys[0] + (x - xs[0]) * (ys[1] - ys[0]) / (xs[1] - xs[0])
    elif x >= xs[-1]:
        value = ys[-1] + (x - xs[-1]) * (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
    else:
        i = next(n for n in range(1, len(xs)) if x <= xs[n])
        value = ys[i - 1] + (x - xs[i - 1]) * (ys[i] - ys[i - 1]) / (xs[i] - xs[i - 1])
    return max(1, min(LEGS_LIMIT, round(value)))


def window_of(target, window=ODDS_WINDOW):
    return target * window[0], target * window[1]


def conflict_groups(legs, extra=None):
    """Legs that may not share a ticket, as groups: same match, or a team in common.

    Connected components over match ids and canonical team names, so at most one leg per group
    means one leg per match and never the same team twice (e.g. a player in singles and
    doubles, or a duplicated fixture). `extra(leg)` adds one more shared key (for example the
    competition id, for tickets with every leg in a different league). Deterministic order.
    """
    parent = {}

    def find(node):
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for item in legs:
        node = ("m", str(item["match_id"]))
        find(node)
        for side in ("home", "away"):
            if item.get(side):
                for team in str(item[side]).split("/"):
                    if canonical(team):
                        union(node, ("t", canonical(team)))
        if extra is not None:
            union(node, ("x", str(extra(item))))
    groups = {}
    for item in legs:
        groups.setdefault(find(("m", str(item["match_id"]))), []).append(item)
    return [groups[key] for key in sorted(groups)]


def rank_key(item):
    """Deterministic order of legs inside a group."""
    return (str(item["match_id"]), str(item["key"]), item["odds"], item["probability"])


def greedy(ordered, lo, hi, max_legs):
    """A quick valid ticket (or None) that seeds the pruning bound of `optimize`."""
    score = log_sum = 0.0
    path, count = None, 0
    for _, options in ordered:
        if count == max_legs or log_sum >= lo - EPS:
            break
        fitting = [o for o in options if log_sum + o[0] <= hi + EPS]
        if not fitting:
            continue
        log_odds, log_p, item = max(fitting, key=lambda o: (o[1] / o[0], o[0]))
        score, log_sum, count = score + log_p, log_sum + log_odds, count + 1
        path = (item, path)
    return (score, log_sum, count, path) if lo - EPS <= log_sum <= hi + EPS else None


def optimize(legs, target, max_legs=None, window=ODDS_WINDOW, bucket=BUCKET, extra=None):
    """Legs of the most likely ticket for `target`, or [] when no combination fits.

    Maximizes sum(log p) subject to lo <= sum(log odds) <= hi (lo, hi from `window`), at most
    `max_legs` legs and at most one leg per conflict group. States are (legs, log-odds bucket)
    and keep the likeliest partial ticket; the exact log-odds travel with each state, so the
    window check is exact. Pruning is safe: probabilities only fall as legs are added, and a
    partial ticket still needing log-odds `need` can at best gain `need * r` where r is the
    best log p / log odds ratio among the remaining groups. Ties: higher total odds (higher
    EV), then fewer legs. Discretisation may only lose a ticket whose odds lie within
    max_legs * bucket (log scale) of the window edges. `extra`: see conflict_groups.
    """
    if not legs or target <= 1:
        return []
    max_legs = max(1, min(LEGS_LIMIT, max_legs or auto_max_legs(target)))
    low, high = window_of(target, window)
    lo, hi = math.log(low), math.log(high)
    ordered = []
    for group in conflict_groups(legs, extra):
        options = []
        for item in sorted(group, key=rank_key):
            odds, p = item["odds"], item["probability"]
            if not (odds and odds > 1 and p and 0 < p <= 1):
                continue
            log_odds = math.log(odds)
            if log_odds > hi + EPS:
                continue
            options.append((log_odds, math.log(p), item))
        if options:
            best_ratio = max(lp / lo_ for lo_, lp, _ in options)
            ordered.append((best_ratio, options))
    # Efficient groups first: good tickets appear early and prune the rest.
    ordered.sort(key=lambda entry: -entry[0])
    count = len(ordered)
    suffix_ratio = [None] * (count + 1)
    suffix_odds = [None] * (count + 1)
    for i in range(count - 1, -1, -1):
        ratio = ordered[i][0]
        top = max(o for o, _, _ in ordered[i][1])
        suffix_ratio[i] = ratio if suffix_ratio[i + 1] is None else max(ratio, suffix_ratio[i + 1])
        suffix_odds[i] = top if suffix_odds[i + 1] is None else max(top, suffix_odds[i + 1])

    # state (legs, bucket) -> (score, log_odds, path); path = (leg, parent path) | None
    states = {(0, 0): (0.0, 0.0, None)}
    best = greedy(ordered, lo, hi, max_legs)  # (score, log_odds, legs, path) | None

    def better(candidate, current):
        if current is None:
            return True
        if candidate[0] > current[0] + EPS:
            return True
        if candidate[0] < current[0] - EPS:
            return False
        if candidate[1] > current[1] + EPS:
            return True
        if candidate[1] < current[1] - EPS:
            return False
        return candidate[2] < current[2]

    for i, (_, options) in enumerate(ordered):
        rest_ratio, rest_odds = suffix_ratio[i + 1], suffix_odds[i + 1]
        updates = {}
        for (legs_in, _), (score, log_sum, path) in states.items():
            if legs_in >= max_legs:
                continue
            for log_odds, log_p, item in options:
                new_log = log_sum + log_odds
                if new_log > hi + EPS:
                    continue
                new_score = score + log_p
                if best is not None and new_score < best[0] - EPS:
                    continue
                need = lo - new_log
                if need <= EPS:
                    candidate = (new_score, new_log, legs_in + 1, (item, path))
                    if better(candidate, best):
                        best = candidate
                    # Adding legs can only lower the probability: no need to extend it.
                    continue
                left = max_legs - legs_in - 1
                if left <= 0 or rest_odds is None or rest_odds * left < need - EPS:
                    continue
                if best is not None and new_score + rest_ratio * need < best[0] - EPS:
                    continue
                key = (legs_in + 1, int(new_log / bucket))
                current = updates.get(key) or states.get(key)
                if current is None or (new_score, new_log) > (current[0], current[1]):
                    updates[key] = (new_score, new_log, (item, path))
        states.update(updates)
        if best is not None:
            states = {k: v for k, v in states.items() if v[0] >= best[0] - EPS}
    if best is None:
        return []
    chosen, path = [], best[3]
    while path is not None:
        chosen.append(path[0])
        path = path[1]
    return sorted(chosen, key=lambda item: (item["kickoff"], str(item["match_id"])))


def explain(legs, target, max_legs, window=ODDS_WINDOW):
    """Romanian reason why no ticket fits the target."""
    low, high = window_of(target, window)
    if not legs:
        return (
            "Nu există selecții eligibile: e nevoie de meciuri viitoare cu cote reale și "
            "date suficiente (grad A–C). Alege altă zi sau mai multe sporturi."
        )
    tops = sorted((max(item["odds"] for item in group) for group in conflict_groups(legs)))
    reachable = math.prod(tops[-max_legs:])
    if reachable < low:
        return (
            f"Cota maximă realizabilă cu cel mult {max_legs} selecții este {reachable:.2f}, "
            f"sub ținta {target:g} (minimum {low:.2f}). Alege o cotă mai mică sau mai multe "
            "sporturi."
        )
    cheapest = min(item["odds"] for item in legs)
    if cheapest > high:
        return (
            f"Cea mai mică cotă eligibilă este {cheapest:.2f}, peste intervalul "
            f"{low:.2f}–{high:.2f}. Alege o cotă țintă mai mare."
        )
    return f"Nicio combinație de selecții nu intră în intervalul {low:.2f}–{high:.2f}."


def build_ticket(legs, target, day, max_legs=None, window=ODDS_WINDOW):
    """Ticket (sports.legs shape + extras) for `target`; status "unavailable" when impossible."""
    max_legs = max(1, min(LEGS_LIMIT, max_legs or auto_max_legs(target)))
    chosen = optimize(legs, target, max_legs, window)
    low, high = window_of(target, window)
    extras = {
        "target": target,
        "max_legs": max_legs,
        "window": [round(low, 4), round(high, 4)],
        "assumption": ASSUMPTION,
    }
    if not chosen:
        built = ticket([], target, day, explain(legs, target, max_legs, window))
        return built | extras | {"expected_value": None, "rationale": built["reason"]}
    built = ticket([dict(item) for item in chosen], target, day)
    sports = sorted({item["sport"] for item in chosen}, key=list(SPORTS).index)
    names = ", ".join(SPORTS[s]["label"].lower() for s in sports)
    one = len(chosen) == 1
    rationale = (
        f"{len(chosen)} {'selecție' if one else 'selecții'} ({names}), "
        f"{'aleasă' if one else 'alese'} automat pentru cea mai mare probabilitate "
        f"combinată la cota țintă {target:g} "
        f"(interval acceptat {low:.2f}–{high:.2f}). Cotă totală {built['total_odds']:.2f}, "
        f"probabilitate estimată {built['probability']:.1%}."
    )
    return built | extras | {"expected_value": built["ev"], "rationale": rationale}


def safest_singles(legs, count=SINGLES):
    """Highest-probability legs with a price of at least SINGLE_MIN_ODDS, one per match."""
    ranked = sorted(
        (item for item in legs if item["odds"] >= SINGLE_MIN_ODDS),
        key=lambda item: (-item["probability"], -item["odds"], rank_key(item)),
    )
    output, seen = [], set()
    for item in ranked:
        if item["match_id"] in seen:
            continue
        seen.add(item["match_id"])
        output.append(dict(item))
        if len(output) == count:
            break
    return output


# --- candidate legs -------------------------------------------------------------------------


def leg_reason(analysis, item):
    """Short Romanian explanation of one leg."""
    implied = 1 / item["odds"]
    text = (
        f"Probabilitate estimată {item['probability']:.0%}, față de {implied:.0%} "
        f"implicit în cota {item['odds']:.2f}."
    )
    notes = [note for note in analysis.get("insights", []) if isinstance(note, str) and note]
    if notes:
        text += " " + notes[0]
    elif analysis.get("summary"):
        text += " " + analysis["summary"].split(". ")[0].rstrip(".") + "."
    return text[:260]


def eligible_legs(
    match, analysis, now=None, min_value=MIN_VALUE, leg_odds=LEG_ODDS, max_value=MAX_VALUE
):
    """Candidate legs of one fixture used by recommendations and the ticket generator.

    Kept: the legs `leg_allowed` accepts (the simulator applies the same predicate).
    """
    refundable = {m["key"] for m in analysis["markets"] if m.get("push")}
    output = []
    for item in candidate_legs(match, analysis, now):
        allowed = leg_allowed(
            match.sport,
            item["key"],
            item["probability"],
            item["odds"],
            push=item["key"] in refundable,
            leg_odds=leg_odds,
            min_value=min_value,
            max_value=max_value,
        )
        if allowed:
            output.append(item | {"reason": leg_reason(analysis, item)})
    return output


def leg_allowed(
    sport,
    key,
    probability,
    odds,
    *,
    push=False,
    leg_odds=LEG_ODDS,
    min_value=MIN_VALUE,
    max_value=MAX_VALUE,
):
    """The one leg rule shared by recommendations, the generator, plans and the simulator.

    A leg is kept when it cannot be refunded (no push probability, no whole line / DNB), its
    real price is inside the odds band and MIN_VALUE <= probability x odds <= MAX_VALUE (either
    bound None = off).
    """
    if push or can_push(sport, key):
        return False
    if not (odds and probability and probability > 0):
        return False
    if not leg_odds[0] <= odds <= leg_odds[1]:
        return False
    value = probability * odds
    if min_value is not None and value < min_value:
        return False
    return max_value is None or value <= max_value


# --- persistence ----------------------------------------------------------------------------


def ensure_tables(store):
    with store.connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS reco_sets (
                day TEXT NOT NULL, sports TEXT NOT NULL, created REAL NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY (day, sports)
            );
            CREATE TABLE IF NOT EXISTS reco_tickets (
                day TEXT NOT NULL, sports TEXT NOT NULL, target REAL NOT NULL,
                created REAL NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                PRIMARY KEY (day, sports, target)
            );
            CREATE TABLE IF NOT EXISTS reco_enriched (
                match_id TEXT PRIMARY KEY, day TEXT NOT NULL, sport TEXT NOT NULL,
                at REAL NOT NULL
            );
        """)


def sports_key(sports):
    return ",".join(sports)


def load_set(store, day, sports):
    with store.connect() as db:
        row = db.execute(
            "SELECT payload FROM reco_sets WHERE day=? AND sports=?",
            (day.isoformat(), sports_key(sports)),
        ).fetchone()
        tickets = {
            float(r[0]): json.loads(r[1])
            for r in db.execute(
                "SELECT target, payload FROM reco_tickets WHERE day=? AND sports=?",
                (day.isoformat(), sports_key(sports)),
            )
        }
    return (json.loads(row[0]) if row else None), tickets


def save_set(store, day, sports, meta, tickets):
    now = time.time()
    with store.connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO reco_sets VALUES (?, ?, ?, ?)",
            (day.isoformat(), sports_key(sports), now, json.dumps(meta)),
        )
        for target, item in tickets.items():
            db.execute(
                "INSERT OR REPLACE INTO reco_tickets VALUES (?, ?, ?, ?, ?, ?)",
                (
                    day.isoformat(),
                    sports_key(sports),
                    float(target),
                    now,
                    json.dumps(item),
                    item["status"],
                ),
            )


def enriched_ids(store, day, sport):
    with store.connect() as db:
        return {
            r[0]
            for r in db.execute(
                "SELECT match_id FROM reco_enriched WHERE day=? AND sport=?",
                (day.isoformat(), sport),
            )
        }


def mark_enriched(store, day, sport, match_id):
    with store.connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO reco_enriched VALUES (?, ?, ?, ?)",
            (match_id, day.isoformat(), sport, time.time()),
        )


def settle_ticket(store, item):
    """Ticket with its legs settled from stored results (unchanged while not final)."""
    if not item.get("legs"):
        return item
    legs = [settle_leg(leg, store.match(leg["match_id"])) for leg in item["legs"]]
    status = ticket_status([leg["status"] for leg in legs])
    output = item | {"legs": legs, "status": status}
    if status in ("won", "void"):
        output["payout_odds"] = settled_odds(legs)
    return output


def locked(item, now):
    """A ticket whose first game started (or any leg settled) is part of the track record."""
    if item.get("status") != "pending" or not item.get("legs"):
        return item.get("status") in ("won", "lost", "void")
    return any(
        leg["status"] != "pending" or datetime.fromisoformat(leg["kickoff"]) <= now
        for leg in item["legs"]
    )


# --- candidate pool -------------------------------------------------------------------------


@dataclass
class Pool:
    legs: list = field(default_factory=list)
    analyzed: dict = field(default_factory=dict)
    enriched: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


def upcoming(store, day, sport, now):
    matches = [
        m
        for m in store.matches_on(day, sport)
        if m.status == "scheduled" and m.kickoff > now and m.odds
    ]
    matches.sort(key=priority)
    return matches[:ANALYSIS_LIMIT]


async def collect(state, day, sports, refresh=False, budget=None, fresh_budget=True):
    """Candidate legs of `day` for `sports` (see the module docstring).

    fresh_budget=True: enrich up to `budget` not-yet-enriched fixtures per sport (refresh of
    the recommendations); False: only what is left of `budget` for that day and sport, so
    repeated ticket generations never spend more than `budget` enrichments per day.
    """
    store, cache = state.store, state.cache
    budget = ENRICH_BUDGET if budget is None else budget
    ensure_tables(store)
    pool = Pool()
    stop_enrich = False
    for sport in sports:
        try:
            _, _, rejected, _ = await state.day_fixtures(day, sport, refresh)
        except ProviderError as exc:
            if exc.status == 503:
                raise
            pool.warnings.append(f"{SPORTS[sport]['label']}: {exc}")
            pool.analyzed[sport] = 0
            continue
        if rejected:
            pool.warnings.append(
                f"{SPORTS[sport]['label']}: {rejected} meciuri incomplete au fost ignorate."
            )
        now = utcnow()
        fixtures = upcoming(store, day, sport, now)
        done = enriched_ids(store, day, sport)
        allowance = budget if fresh_budget else max(0, budget - len(done))
        pool.enriched[sport] = 0
        if allowance and not stop_enrich:
            # Most promising first: priority order, then the best grades within a shortlist.
            shortlist = [m for m in fixtures if m.id not in done][: 3 * budget]
            first = await run_in_threadpool(lambda: [cache.get(m, THRESHOLD) for m in shortlist])
            order = sorted(range(len(shortlist)), key=lambda n: (GRADE_RANK[first[n]["grade"]], n))
            for n in order[:allowance]:
                match = shortlist[n]
                try:
                    notes, _ = await state.enrich(match)
                except ProviderError as exc:
                    if exc.status == 503:
                        raise
                    pool.warnings.append(f"{match.home} – {match.away}: {exc}")
                    if exc.status == 429:
                        stop_enrich = True
                        break
                    continue
                mark_enriched(store, day, sport, match.id)
                pool.enriched[sport] += 1
                if notes:
                    log.info("Enrichment warnings for %s: %s", match.id, notes)
            fixtures = upcoming(store, day, sport, now)
        analyses = await run_in_threadpool(lambda: [cache.get(m, THRESHOLD) for m in fixtures])
        now = utcnow()
        pool.analyzed[sport] = len(fixtures)
        for match, analysis in zip(fixtures, analyses):
            pool.legs.extend(eligible_legs(match, analysis, now))
    return pool


# --- features -------------------------------------------------------------------------------


def settle_all(store, tickets):
    """Every stored ticket settled from stored results (the caller persists them)."""
    return {target: settle_ticket(store, item) for target, item in tickets.items()}


def response(day, sports, targets, meta, tickets):
    return {
        "day": day.isoformat(),
        "sports": list(sports),
        "targets": [t if not float(t).is_integer() else int(t) for t in targets],
        "tickets": [tickets[float(t)] for t in targets],
        "singles": meta.get("singles", []),
        "analyzed": meta.get("analyzed", {}),
        "enriched": meta.get("enriched", {}),
        "warnings": meta.get("warnings", []),
        "generated_at": meta.get("generated_at"),
        "disclaimer": DISCLAIMER,
    }


async def recommendations(state, day, sports, targets, refresh=False):
    """The day's recommendations: stored unless `refresh` (or missing), settled from stored
    results. On refresh, a ticket whose first game already started keeps its legs, so the
    track record can never be rewritten after a result is known."""
    store = state.store
    ensure_tables(store)
    targets = [float(t) for t in targets]
    meta, stored = load_set(store, day, sports)
    missing = [t for t in targets if t not in stored]
    now = utcnow()
    if refresh or meta is None or missing:
        # Adding a target to a stored day only uses what is left of the enrichment budget.
        fresh = refresh or meta is None
        pool = await collect(state, day, sports, refresh=refresh, fresh_budget=fresh)
        now = utcnow()
        # Re-check each stored ticket: results may have arrived with the day fixtures.
        stored = settle_all(store, stored)
        chosen = {}
        for target in targets:
            current = stored.get(target)
            if current is not None and (locked(current, now) or not refresh):
                continue
            chosen[target] = await run_in_threadpool(build_ticket, pool.legs, target, day)
        if refresh or meta is None:
            meta = {
                "singles": safest_singles(pool.legs),
                "analyzed": pool.analyzed,
                "enriched": pool.enriched,
                "warnings": pool.warnings,
                "generated_at": now.isoformat(),
            }
        stored |= chosen
        save_set(store, day, sports, meta, stored)
    else:
        pending = {
            leg["sport"]
            for item in stored.values()
            for leg in item.get("legs", [])
            if leg["status"] == "pending" and datetime.fromisoformat(leg["kickoff"]) <= now
        }
        for sport in [s for s in sports if s in pending]:
            try:
                await state.day_fixtures(day, sport)
            except ProviderError as exc:
                if exc.status == 503:
                    raise
                meta = meta | {"warnings": meta.get("warnings", []) + [str(exc)]}
        settled = settle_all(store, stored)
        if settled != stored:
            save_set(store, day, sports, load_set(store, day, sports)[0] or meta, settled)
        stored = settled
    meta = meta | {"singles": [settle_leg(s, store.match(s["match_id"])) for s in meta["singles"]]}
    return response(day, sports, targets, meta, stored)


async def generate(state, day, target, sports, max_legs=None, exclude=(), alternatives=2):
    """One ticket for `target` from the day's pool, plus up to `alternatives` tickets on
    completely different matches. Never takes a minimum probability."""
    pool = await collect(state, day, sports, fresh_budget=False)
    excluded = set(exclude)
    legs = [item for item in pool.legs if item["match_id"] not in excluded]
    main = await run_in_threadpool(build_ticket, legs, target, day, max_legs)
    others = []
    used = {leg["match_id"] for leg in main["legs"]}
    while main["legs"] and len(others) < alternatives:
        rest = [item for item in legs if item["match_id"] not in used]
        extra = await run_in_threadpool(build_ticket, rest, target, day, max_legs)
        if not extra["legs"]:
            break
        others.append(extra)
        used |= {leg["match_id"] for leg in extra["legs"]}
    return {
        "ticket": main,
        "alternatives": others,
        "analyzed": pool.analyzed,
        "enriched": pool.enriched,
        "candidates": len(legs),
        "warnings": pool.warnings,
        "disclaimer": DISCLAIMER,
    }


def history(store, sports, days=60, today=None):
    """Track record of the stored AI tickets (settled from stored results, newest first).

    Profit is per 1 unit staked on every settled ticket: won -> odds - 1 (void legs count 1.0),
    lost -> -1, void -> 0.
    """
    ensure_tables(store)
    today = today or utcnow().date()
    with store.connect() as db:
        rows = db.execute(
            "SELECT day, target, payload FROM reco_tickets WHERE sports=? AND day<=? "
            "ORDER BY day DESC, target",
            (sports_key(sports), today.isoformat()),
        ).fetchall()
    by_day, summary = {}, {}
    changed = []
    for day_text, target, payload in rows:
        if len(by_day) >= days and day_text not in by_day:
            break
        item = json.loads(payload)
        settled = settle_ticket(store, item)
        if settled != item:
            changed.append((day_text, target, settled))
        row = {
            "target": target,
            "status": settled["status"],
            "total_odds": settled.get("total_odds"),
            "probability": settled.get("probability"),
            "legs": len(settled.get("legs", [])),
            "payout_odds": settled.get("payout_odds"),
        }
        by_day.setdefault(day_text, []).append(row)
        stats = summary.setdefault(
            target,
            {"tickets": 0, "won": 0, "lost": 0, "void": 0, "pending": 0, "unavailable": 0},
        )
        stats["tickets"] += 1
        stats[settled["status"]] += 1
        stats.setdefault("profit", 0.0)
        if settled["status"] == "won":
            stats["profit"] += settled.get("payout_odds", settled["total_odds"]) - 1
        elif settled["status"] == "lost":
            stats["profit"] -= 1
    if changed:
        with store.connect() as db:
            for day_text, target, item in changed:
                db.execute(
                    "UPDATE reco_tickets SET payload=?, status=? WHERE day=? AND sports=? "
                    "AND target=?",
                    (json.dumps(item), item["status"], day_text, sports_key(sports), target),
                )
    for stats in summary.values():
        decided = stats["won"] + stats["lost"]
        staked = decided + stats["void"]
        stats["hit_rate"] = stats["won"] / decided if decided else None
        stats["roi"] = stats["profit"] / staked if staked else None
    return {
        "sports": list(sports),
        "days": [{"day": d, "tickets": items} for d, items in by_day.items()],
        "summary": [{"target": t} | s for t, s in sorted(summary.items())],
        "disclaimer": DISCLAIMER,
        "note": "Profit calculat la o miză de 1 unitate pe fiecare bilet decis.",
    }


def parse_targets(text):
    """ "2,5,10,100" -> [2.0, 5.0, 10.0, 100.0]; ValueError when invalid."""
    values = []
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        value = round(float(part), 2)
        if not math.isfinite(value) or not 1.2 <= value <= 1000:
            raise ValueError("Cota țintă trebuie să fie între 1.2 și 1000.")
        if value not in values:
            values.append(value)
    if not values or len(values) > 8:
        raise ValueError("Alege între 1 și 8 cote țintă.")
    return values


def parse_sports(value):
    """ "football,tennis" or a list -> known sports in registry order; ValueError otherwise."""
    items = value.split(",") if isinstance(value, str) else list(value)
    items = [str(s).strip() for s in items if str(s).strip()]
    if not items or any(s not in SPORTS for s in items):
        raise ValueError("Sport necunoscut.")
    return [s for s in SPORTS if s in items]


__all__ = [
    "ASSUMPTION",
    "DISCLAIMER",
    "ENRICH_BUDGET",
    "TARGETS",
    "auto_max_legs",
    "build_ticket",
    "collect",
    "conflict_groups",
    "eligible_legs",
    "generate",
    "history",
    "optimize",
    "recommendations",
    "safest_singles",
]
