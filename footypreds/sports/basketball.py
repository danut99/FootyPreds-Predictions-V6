"""Basketball analyzer: opponent-adjusted points ratings, form, rest, H2H and a market blend.

Pipeline for one fixture (only results that started before kickoff - 3h are visible):

1. Collect both teams' results in every competition of the fixture's population (senior vs.
   youth, men vs. women; namesakes are told apart by team id, as in football), plus the
   opponents' other results (two hops), so beating a weak side counts less.
2. Fit time-weighted multiplicative offence/defence points ratings with a home-court factor:
   E[home points] = mu * home * off[home] * def[away], E[away points] = mu * off[away] *
   def[home]. Conjugate Poisson-style updates shrink every rating towards 1 with
   `prior_games` pseudo-games, so two results never swing a team. Team pace (the points
   level of its games) = mu * (off + def). Overtime results are converted to their tied
   regulation score first; neutral-venue tournaments get no home factor.
3. Adjust the expected margin and total lightly for recent form (last 5 vs. ratings), H2H
   (last 6 meetings vs. ratings) and back-to-back fatigue.
4. Regulation margin ~ Normal(mu_m, sd_m) and total ~ Normal(mu_t, sd_t), discretised on
   whole points. The standard deviations are the fit residuals shrunk towards literature
   priors (margin 12.5 for 48 minutes, total 8 + 0.046 * total: ~18.5 at 225 NBA points and
   ~15.5 at 160 FIBA points), scaled for 40-minute games. A regulation tie goes to a
   5-minute overtime with the same scoring rates, so "1"/"2" (winner incl. overtime) never
   draw, and totals/handicaps settle on the final score as FlashScore stores it.
5. Market blend: every quoted two-way pair of Match.odds (winner, handicaps, totals) is
   made margin-free and inverted into an implied mean; the pairs of one family are averaged
   by Fisher information and the families (winner vs. handicaps) are combined the same way.
   Model and market means are pooled linearly. With equal variances that is exactly the
   log-linear pooling football uses (N(m1, s) ** (1 - w) * N(m2, s) ** w = N(mix, s)).

Market weight: there is no historical basketball odds dataset to tune on, so the weight is a
conservative literature default. NBA/EuroLeague closing lines are well documented as
efficient, and football's tuned 1X2 weight is 0.9; FlashScore basketball histories are
sparser than football ones, so the market gets `market_weight` = 0.85 only when both teams
have plenty of recent data, rising linearly to 1.0 (pure market) as the data quality falls.
"""

import math
from dataclasses import asdict, dataclass, field, replace
from datetime import timedelta

from footypreds.engine.analyzer import team_score
from footypreds.engine.form import is_opponent
from footypreds.engine.history import canonical, is_friendly, is_youth
from footypreds.engine.ratings import decay
from footypreds.sports import common as c
from footypreds.sports.keys import fmt_line, handicap, is_whole_or_half, over, parse, under

VERSION = "basketball-1.0-ratings-rest-market"
SPORT = "basketball"
# Kept for callers of the baseline: typical 40-minute values.
MARGIN_SD = 12.0
TOTAL_SD = 17.0
HOME_ADVANTAGE = 2.5
DEFAULT_TOTAL = 160.0
MARKET_WEIGHT = 0.85
MAX_DAYS = 450
OVERTIME_MINUTES = 5.0

WINNER = "Câștigător (incl. prelungiri)"
REGULATION = "Timp regulamentar"
SPREAD = "Handicap"
TOTAL = "Total puncte"
TEAM_TOTAL = "Puncte pe echipă"
PARITY = "Paritate"
FIRST_HALF = "Prima repriză"

WOMEN_TOKENS = ("women", "wnba", "female", "ladies", "feminin", "damen")
NEUTRAL_TOKENS = (
    "olympic",
    "world cup",
    "asian games",
    "eurobasket",
    "americup",
    "afrobasket",
    "asia cup",
    "final four",
    "world championship",
    "universiade",
)


@dataclass(frozen=True)
class Params:
    """Literature defaults (see the module docstring); no basketball odds dataset to tune on."""

    half_life: float = 180.0
    max_days: int = MAX_DAYS
    prior_games: float = 6.0
    home_prior: float = 1.03
    home_prior_games: float = 60.0
    friendly_weight: float = 0.5
    max_fit_matches: int = 4000
    iterations: int = 60
    margin_sd: float = 12.5
    total_sd_base: float = 8.0
    total_sd_slope: float = 0.046
    sd_prior_games: float = 40.0
    residual_inflation: float = 1.1
    form_window: int = 5
    form_prior: float = 5.0
    form_weight: float = 0.2
    h2h_window: int = 6
    h2h_prior: float = 6.0
    h2h_weight: float = 0.1
    back_to_back: float = 1.5
    market_weight: float = MARKET_WEIGHT
    cutoff_hours: float = 3.0


PARAMS = Params()


def with_params(**changes):
    return replace(PARAMS, **changes)


# ---------------------------------------------------------------- populations and venues


def game_minutes(league):
    """48 for the NBA (and G League), 40 for FIBA, WNBA, NCAA and the rest."""
    tokens = canonical(league).replace("-", " ").split()
    return 48.0 if "nba" in tokens else 40.0


def is_women(match):
    league = canonical(match.league)
    if any(token in league for token in WOMEN_TOKENS):
        return True
    return match.home.endswith(" W") or match.away.endswith(" W")


def population(match):
    """Senior/youth and men/women games are different populations: never mixed."""
    return is_youth(match.league), is_women(match)


def is_neutral(league):
    name = canonical(league)
    return "qualif" not in name and any(token in name for token in NEUTRAL_TOKENS)


def regulation_score(match):
    """(home, away) points at the end of regulation: an overtime game was tied there."""
    if match.finish_type == "aet":
        minutes = game_minutes(match.league)
        level = (match.home_goals + match.away_goals) * minutes / (minutes + OVERTIME_MINUTES)
        return level / 2, level / 2
    return float(match.home_goals), float(match.away_goals)


def default_total(fixture):
    if game_minutes(fixture.league) == 48:
        return 225.0
    return 140.0 if is_women(fixture) else DEFAULT_TOTAL


# ---------------------------------------------------------------- ratings


@dataclass
class Fit:
    mu: float
    home: float
    attack: dict
    defence: dict
    weight: dict
    matches: int
    rows: list
    keys: dict = field(default_factory=dict)

    def expected(self, home_key, away_key, neutral=False):
        factor = 1.0 if neutral else self.home
        return (
            self.mu * factor * self.attack.get(home_key, 1.0) * self.defence.get(away_key, 1.0),
            self.mu * self.attack.get(away_key, 1.0) * self.defence.get(home_key, 1.0),
        )

    def rating(self, key):
        offence = self.mu * self.attack.get(key, 1.0)
        defence = self.mu * self.defence.get(key, 1.0)
        return {
            "offence": offence,
            "defence": defence,
            "pace": offence + defence,
            "weight": self.weight.get(key, 0.0),
        }


def fit_points(rows, params=PARAMS):
    """rows: (home_key, away_key, home_points, away_points, weight, neutral). None if empty."""
    rows = [r for r in rows if r[4] > 0]
    if not rows:
        return None
    teams = sorted({key for r in rows for key in r[:2]})
    attack = dict.fromkeys(teams, 1.0)
    defence = dict.fromkeys(teams, 1.0)
    total_weight = sum(r[4] for r in rows)
    points = sum(r[4] * (r[2] + r[3]) for r in rows)
    home_points = sum(r[4] * r[2] for r in rows if not r[5])
    mu, home = points / (2 * total_weight), params.home_prior
    for _ in range(params.iterations):
        previous = dict(attack)
        prior = params.prior_games * mu
        scored = dict.fromkeys(teams, prior)
        chances = dict.fromkeys(teams, prior)
        for h, a, hp, ap, w, neutral in rows:
            factor = 1.0 if neutral else home
            scored[h] += w * hp
            chances[h] += w * mu * factor * defence[a]
            scored[a] += w * ap
            chances[a] += w * mu * defence[h]
        attack = {t: scored[t] / chances[t] for t in teams}
        conceded = dict.fromkeys(teams, prior)
        exposure = dict.fromkeys(teams, prior)
        for h, a, hp, ap, w, neutral in rows:
            factor = 1.0 if neutral else home
            conceded[h] += w * ap
            exposure[h] += w * mu * attack[a]
            conceded[a] += w * hp
            exposure[a] += w * mu * factor * attack[h]
        defence = {t: conceded[t] / exposure[t] for t in teams}
        base_home = sum(w * mu * attack[h] * defence[a] for h, a, _, _, w, n in rows if not n)
        home_prior = params.home_prior_games * mu
        home = (home_points + home_prior * params.home_prior) / (base_home + home_prior)
        base = sum(
            w * ((1.0 if n else home) * attack[h] * defence[a] + attack[a] * defence[h])
            for h, a, _, _, w, n in rows
        )
        mu = points / base
        if max(abs(attack[t] - previous[t]) for t in teams) < 1e-7:
            break
    weight = dict.fromkeys(teams, 0.0)
    for h, a, *_, w, _ in rows:
        weight[h] += w
        weight[a] += w
    return Fit(mu, home, attack, defence, weight, len(rows), rows)


HOME_KEY, AWAY_KEY = "\x00home", "\x00away"


def local_fit(fixture, index, home_rows, away_rows, cutoff, oldest, params):
    """Fit on the two teams, their opponents and the opponents' other results (2 hops)."""
    overrides, selected = {}, {}
    for rows, key in ((home_rows, HOME_KEY), (away_rows, AWAY_KEY)):
        for match, side in rows:
            selected[match.id] = match
            keys = overrides.get(match.id, (None, None))
            overrides[match.id] = (key, keys[1]) if side == "home" else (keys[0], key)
    # Mutual games are already selected; a name-only lookup of the fixture teams would pull
    # in their namesakes, so they are never an "opponent".
    opponents = {
        canonical(match.away if side == "home" else match.home)
        for match, side in home_rows + away_rows
    } - {canonical(fixture.home), canonical(fixture.away)}
    for opponent in sorted(opponents):
        for match, _ in index.team(opponent, "", cutoff, oldest, exclude=fixture.id):
            selected.setdefault(match.id, match)
    matches = sorted(selected.values(), key=lambda m: (m.kickoff, m.id))
    matches = matches[-params.max_fit_matches :]
    kind = population(fixture)
    rows = []
    for match in matches:
        if population(match) != kind:
            continue
        age = (fixture.kickoff - match.kickoff).total_seconds() / 86400
        w = decay(age, params.half_life)
        if is_friendly(match.league):
            w *= params.friendly_weight
        home_key, away_key = overrides.get(match.id, (None, None))
        hp, ap = regulation_score(match)
        rows.append(
            (
                home_key or canonical(match.home),
                away_key or canonical(match.away),
                hp,
                ap,
                w,
                is_neutral(match.league),
            )
        )
    fit = fit_points(rows, params)
    if fit is not None:
        fit.keys = overrides
    return fit


def residual_sds(fit):
    """Weighted residual variances of margin and total, plus the effective sample size."""
    total_weight = square = margin_var = total_var = 0.0
    for h, a, hp, ap, w, neutral in fit.rows:
        eh, ea = fit.expected(h, a, neutral)
        margin_var += w * ((hp - ap) - (eh - ea)) ** 2
        total_var += w * ((hp + ap) - (eh + ea)) ** 2
        total_weight += w
        square += w * w
    if total_weight <= 0:
        return None
    return margin_var / total_weight, total_var / total_weight, total_weight**2 / square


def shrink_sd(prior, variance, n_eff, params):
    k = params.sd_prior_games
    if variance is None:
        return prior
    return math.sqrt((n_eff * variance * params.residual_inflation + k * prior**2) / (n_eff + k))


def residuals(rows, key, fit, window):
    """Recent (margin, total) residuals of one team vs. the ratings, newest first."""
    output = []
    for match, side in list(reversed(rows))[:window]:
        home, away = fit.keys.get(match.id, (None, None))
        home = key if side == "home" else home or canonical(match.home)
        away = key if side == "away" else away or canonical(match.away)
        eh, ea = fit.expected(home, away, is_neutral(match.league))
        hp, ap = regulation_score(match)
        margin = (hp - ap) - (eh - ea)
        output.append((margin if side == "home" else -margin, (hp + ap) - (eh + ea)))
    return output


def h2h_residual(fixture, home_rows, fit, params):
    total, count = 0.0, 0
    for match, side in reversed(home_rows):
        if not is_opponent(match, side, fixture):
            continue
        home = HOME_KEY if side == "home" else AWAY_KEY
        away = AWAY_KEY if side == "home" else HOME_KEY
        eh, ea = fit.expected(home, away, is_neutral(match.league))
        hp, ap = regulation_score(match)
        margin = (hp - ap) - (eh - ea)
        total += margin if side == "home" else -margin
        count += 1
        if count == params.h2h_window:
            break
    return total / (count + params.h2h_prior), count


def rest_hours(rows, kickoff):
    if not rows:
        return None
    return (kickoff - rows[-1][0].kickoff).total_seconds() / 3600


# ---------------------------------------------------------------- distribution


class Distribution:
    """Final score distribution: discretised Normal regulation margin/total plus overtime."""

    def __init__(self, margin, total, margin_sd, total_sd, minutes):
        self.margin, self.total = margin, total
        self.margin_sd, self.total_sd = margin_sd, total_sd
        self.ratio = OVERTIME_MINUTES / minutes
        self.tie = self.reg_cdf(0.5) - self.reg_cdf(-0.5)
        self.ot_margin = margin * self.ratio
        self.ot_sd = max(1.5, margin_sd * math.sqrt(self.ratio))
        ot_tie = self.ot_cdf(0.5) - self.ot_cdf(-0.5)
        self.ot_scale = 1 / max(1e-9, 1 - ot_tie)

    def reg_cdf(self, x):
        return c.NORMAL.cdf((x - self.margin) / self.margin_sd)

    def ot_cdf(self, x):
        return c.NORMAL.cdf((x - self.ot_margin) / self.ot_sd)

    def margin_at_least(self, k):
        """P(final margin >= k), k integer; a final margin is never 0."""
        if k <= 0:
            if k == 0:
                k = 1
            else:
                below = self.reg_cdf(k - 0.5) + self.tie * self.ot_cdf(k - 0.5) * self.ot_scale
                return 1 - below
        return 1 - self.reg_cdf(k - 0.5) + self.tie * (1 - self.ot_cdf(k - 0.5)) * self.ot_scale

    def margin_is(self, k):
        return self.margin_at_least(k) - self.margin_at_least(k + 1)

    def cover(self, line):
        """(win, push) for the home side with handicap `line` added to its score."""
        target = -line
        if float(target).is_integer():
            k = int(target)
            return self.margin_at_least(k + 1), self.margin_is(k)
        return self.margin_at_least(math.floor(target) + 1), 0.0

    def total_at_least(self, k):
        spread = self.total_sd * math.sqrt(1 + self.ratio)
        regular = 1 - c.NORMAL.cdf((k - 0.5 - self.total) / self.total_sd)
        extra = 1 - c.NORMAL.cdf((k - 0.5 - self.total * (1 + self.ratio)) / spread)
        return (1 - self.tie) * regular + self.tie * extra

    def team_at_least(self, side, k):
        margin = self.margin if side == "home" else -self.margin
        sd = math.sqrt(self.total_sd**2 + self.margin_sd**2) / 2
        regular = 1 - c.NORMAL.cdf((k - 0.5 - (self.total + margin) / 2) / sd)
        extra_mean = self.total / 2 + self.ratio * (self.total + margin) / 2
        extra_sd = math.sqrt(self.total_sd**2 / 4 + self.ratio * sd**2)
        extra = 1 - c.NORMAL.cdf((k - 0.5 - extra_mean) / extra_sd)
        return (1 - self.tie) * regular + self.tie * extra

    @staticmethod
    def line(at_least, line):
        """(win, push) of an over bet at `line` given a P(X >= k) function."""
        if float(line).is_integer():
            k = int(line)
            return at_least(k + 1), at_least(k) - at_least(k + 1)
        return at_least(math.floor(line) + 1), 0.0

    def over(self, line):
        return self.line(self.total_at_least, line)

    def team_over(self, side, line):
        return self.line(lambda k: self.team_at_least(side, k), line)

    def summary(self):
        """(E[final margin], P(odd final total)); the total and margin share their parity."""
        span = int(abs(self.margin) + 8 * self.margin_sd) + 10
        expected = odd = 0.0
        previous = self.margin_at_least(-span)
        for k in range(-span, span + 1):
            current = self.margin_at_least(k + 1)
            p = previous - current
            previous = current
            expected += k * p
            if k % 2:
                odd += p
        return expected, odd

    def expected_total(self):
        return self.total * (1 + self.tie * self.ratio)


def conditional(win, push):
    """Win probability given no push (whole lines refund the stake)."""
    decided = 1 - push
    return win / decided if decided > 1e-12 else 0.5


# ---------------------------------------------------------------- market


def quoted(odds):
    """Half/whole lines present in the odds: home handicaps, totals and team totals."""
    spreads, totals, teams = set(), set(), {"home": set(), "away": set()}
    for key in odds:
        spec = parse(key)
        if spec is None:
            continue
        family, groups = spec
        if family == "handicap" and is_whole_or_half(groups[1]):
            line = float(groups[1])
            spreads.add(line if groups[0] == "1" else -line)
        elif family == "total" and is_whole_or_half(groups[1]):
            totals.add(float(groups[1]))
        elif family == "team_total" and is_whole_or_half(groups[2]):
            teams[groups[0]].add(float(groups[2]))
    return spreads, totals, teams


def solve(probability, target, low, high):
    """Mean whose `probability(mean)` (increasing) equals target, by bisection."""
    for _ in range(60):
        middle = (low + high) / 2
        if probability(middle) < target:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def implied(probability, target, low, high):
    """(implied mean, Fisher information) of one margin-free two-way pair."""
    target = min(0.99, max(0.01, target))
    mean = solve(probability, target, low, high)
    slope = (probability(mean + 0.25) - probability(mean - 0.25)) / 0.5
    return mean, slope**2 / (target * (1 - target))


def pooled(estimates):
    """Information-weighted mean of (mean, info) pairs, and the best single information."""
    estimates = [e for e in estimates if e[1] > 0]
    if not estimates:
        return None
    info = sum(i for _, i in estimates)
    return sum(m * i for m, i in estimates) / info, max(i for _, i in estimates)


def market_view(odds, margin_sd, total_sd, minutes, total_hint, margin_hint):
    """Market-implied regulation margin and total (None when not quoted)."""
    spreads, totals, _ = quoted(odds)

    def margin_prob(line):
        def probability(mean):
            dist = Distribution(mean, total_hint, margin_sd, total_sd, minutes)
            return conditional(*dist.cover(line))

        return probability

    groups = []
    winner = c.two_way(odds, "1", "2")
    if winner:
        groups.append(pooled([implied(margin_prob(-0.5), winner[0], -80, 80)]))
    pairs = []
    for line in sorted(spreads):
        pair = c.two_way(odds, handicap("1", line), handicap("2", -line))
        if pair:
            pairs.append(implied(margin_prob(line), pair[0], -80, 80))
    if pairs:
        groups.append(pooled(pairs))
    margin = pooled([g for g in groups if g])
    # The overtime chance (and so the final total) depends on the margin.
    centre = margin[0] if margin else margin_hint
    totals_found = []
    for line in sorted(totals):
        pair = c.two_way(odds, over(line), under(line))
        if pair:

            def probability(mean, line=line):
                dist = Distribution(centre, mean, margin_sd, total_sd, minutes)
                return conditional(*dist.over(line))

            totals_found.append(implied(probability, pair[0], 20, 450))
    total = pooled(totals_found)
    return {
        "winner": winner,
        "margin": margin[0] if margin else None,
        "total": total[0] if total else None,
        "handicap_pairs": len(pairs),
        "total_pairs": len(totals_found),
    }


def reference_total(odds):
    """The quoted total line closest to 50/50, as a scale hint for the total sd prior."""
    best = None
    for line in quoted(odds)[1]:
        pair = c.two_way(odds, over(line), under(line))
        if pair and (best is None or abs(pair[0] - 0.5) < best[1]):
            best = (line, abs(pair[0] - 0.5))
    return best[0] if best else None


# ---------------------------------------------------------------- form and insights


def streak(form_rows):
    """(result, length) of the current run, newest first rows of the form table."""
    if not form_rows:
        return None
    first = form_rows[0]["result"]
    length = 0
    for row in form_rows:
        if row["result"] != first:
            break
        length += 1
    return {"result": first, "count": length}


def rich_form(rows, kickoff):
    form = c.team_form(rows, kickoff)
    recent = list(reversed(rows))
    margins = [c.perspective(m, s)[0] - c.perspective(m, s)[1] for m, s in recent[:10]]
    hours = rest_hours(rows, kickoff)
    form.update(
        {
            "margin_avg5": sum(margins[:5]) / len(margins[:5]) if margins else None,
            "margin_avg10": sum(margins) / len(margins) if margins else None,
            "streak": streak(form["last"]),
            "rest_days": round(hours / 24, 1) if hours is not None else None,
            "back_to_back": hours is not None and hours < 36,
            "home10": c.window([r for r in recent if r[1] == "home"][:10]),
            "away10": c.window([r for r in recent if r[1] == "away"][:10]),
        }
    )
    return form


def build_insights(fixture, forms, h2h, context):
    notes = []
    for team, form in ((fixture.home, forms["home"]), (fixture.away, forms["away"])):
        last10 = form["last10"]
        if not last10:
            notes.append(f"{team}: niciun rezultat recent disponibil.")
            continue
        notes.append(f"{team} a câștigat {last10['wins']} din ultimele {last10['played']} meciuri.")
        notes.append(
            f"{team}: media de puncte {last10['scored_avg']:.1f} marcate și "
            f"{last10['conceded_avg']:.1f} primite (diferență {form['margin_avg10']:+.1f})."
        )
        run = form["streak"]
        if run and run["count"] >= 3 and run["result"] in ("W", "L"):
            kind = "victorii" if run["result"] == "W" else "înfrângeri"
            notes.append(f"{team} are o serie de {run['count']} {kind} consecutive.")
        if form["back_to_back"]:
            notes.append(f"{team} joacă back-to-back (a jucat și cu o zi înainte).")
        elif form["rest_days"] is not None and form["rest_days"] >= 10:
            notes.append(f"{team} nu a mai jucat de {form['rest_days']:.0f} zile.")
    if h2h["played"]:
        notes.append(
            f"Meciuri directe: {h2h['home_wins']}-{h2h['away_wins']} (gazde-oaspeți) "
            f"în ultimele {h2h['played']} întâlniri."
        )
    if context["neutral"]:
        notes.append("Teren neutru: nu se acordă avantajul terenului propriu.")
    model, market = context["model_margin"], context["market_margin"]
    if model is not None and market is not None and abs(model - market) >= 4:
        notes.append(
            f"Cotele indică o diferență de {market:+.1f} puncte pentru gazde, "
            f"modelul propriu {model:+.1f}."
        )
    return notes


# ---------------------------------------------------------------- analysis


def model_view(fixture, index, params):
    """Ratings-based margin/total with form, H2H and rest adjustments (no market)."""
    cutoff = fixture.kickoff - timedelta(hours=params.cutoff_hours)
    kind = population(fixture)
    home_rows, away_rows = c.team_rows(index, fixture, params.max_days)
    home_rows = [(m, s) for m, s in home_rows if m.kickoff < cutoff and population(m) == kind]
    away_rows = [(m, s) for m, s in away_rows if m.kickoff < cutoff and population(m) == kind]
    neutral = is_neutral(fixture.league)
    minutes = game_minutes(fixture.league)
    oldest = fixture.kickoff - timedelta(days=params.max_days)
    fit = None
    if home_rows or away_rows:
        fit = local_fit(fixture, index, home_rows, away_rows, cutoff, oldest, params)
    view = {
        "home_rows": home_rows,
        "away_rows": away_rows,
        "fit": fit,
        "neutral": neutral,
        "minutes": minutes,
        "adjustments": {},
    }
    scale = minutes / 48
    if fit is None:
        total = default_total(fixture)
        home_edge = 0.0 if neutral else total / 2 * (params.home_prior - 1)
        view.update(margin=home_edge, total=total, sds=None)
        return view
    eh, ea = fit.expected(HOME_KEY, AWAY_KEY, neutral)
    home_res = residuals(home_rows, HOME_KEY, fit, params.form_window)
    away_res = residuals(away_rows, AWAY_KEY, fit, params.form_window)
    form_home = sum(r[0] for r in home_res) / (len(home_res) + params.form_prior)
    form_away = sum(r[0] for r in away_res) / (len(away_res) + params.form_prior)
    pace_home = sum(r[1] for r in home_res) / (len(home_res) + params.form_prior)
    pace_away = sum(r[1] for r in away_res) / (len(away_res) + params.form_prior)
    h2h_margin, h2h_count = h2h_residual(fixture, home_rows, fit, params)
    home_rest = rest_hours(home_rows, fixture.kickoff)
    away_rest = rest_hours(away_rows, fixture.kickoff)
    fatigue = params.back_to_back * scale
    rest = (-fatigue if home_rest is not None and home_rest < 36 else 0.0) + (
        fatigue if away_rest is not None and away_rest < 36 else 0.0
    )
    form_adj = params.form_weight * (form_home - form_away)
    h2h_adj = params.h2h_weight * h2h_margin
    pace_adj = params.form_weight * (pace_home + pace_away) / 2
    view.update(
        margin=eh - ea + form_adj + h2h_adj + rest,
        total=eh + ea + pace_adj,
        sds=residual_sds(fit),
        adjustments={
            "form": form_adj,
            "h2h": h2h_adj,
            "h2h_games": h2h_count,
            "rest": rest,
            "pace": pace_adj,
        },
        ratings={"home": fit.rating(HOME_KEY), "away": fit.rating(AWAY_KEY)},
        base={"home": eh, "away": ea},
    )
    return view


def complement(p):
    """1 - p, never closer to 50% than p in floating point.

    main_markets() picks the line "closest to 50%" and keeps the first of equal candidates,
    so the over/home side of a pair must not lose that tie to a rounding bit.
    """
    other = 1 - p
    if abs(other - 0.5) < abs(p - 0.5):
        other = math.nextafter(other, 0.0 if other < 0.5 else 1.0)
    return other


def make_market(key, group, probability, odds, selectable=True, label=None):
    item = c.market(SPORT, key, group, probability, odds, selectable)
    if label:
        item["label"] = label
    return item


def build_markets(dist, odds, mu_home, mu_away):
    markets = []
    p_home = dist.margin_at_least(1)
    markets.append(make_market("1", WINNER, p_home, odds))
    markets.append(make_market("2", WINNER, 1 - p_home, odds))
    reg_home = 1 - dist.reg_cdf(0.5)
    reg_away = dist.reg_cdf(-0.5)
    for key, p, text in (
        ("reg_1", reg_home, "Gazdele câștigă în timpul regulamentar"),
        ("reg_X", dist.tie, "Egal după timpul regulamentar (prelungiri)"),
        ("reg_2", reg_away, "Oaspeții câștigă în timpul regulamentar"),
    ):
        # A stored final score includes overtime, so these are informative only.
        markets.append(make_market(key, REGULATION, p, odds, False, text))

    spreads, totals, team_lines = quoted(odds)
    centre = math.floor(-dist.margin) + 0.5
    for line in sorted(spreads | {centre + d for d in (-10, -6, -3, 0, 3, 6, 10)}):
        p = conditional(*dist.cover(line))
        markets.append(make_market(handicap("1", line), SPREAD, p, odds))
        markets.append(make_market(handicap("2", -line), SPREAD, complement(p), odds))

    middle = math.floor(dist.total) + 0.5
    for line in sorted(totals | {middle + d for d in (-15, -10, -5, 0, 5, 10, 15)}):
        if line <= 0:
            continue
        p = conditional(*dist.over(line))
        markets.append(make_market(over(line), TOTAL, p, odds))
        markets.append(make_market(under(line), TOTAL, complement(p), odds))

    for side, mean in (("home", mu_home), ("away", mu_away)):
        base = math.floor(mean) + 0.5
        for line in sorted(team_lines[side] | {base + d for d in (-8, -4, 0, 4, 8)}):
            if line <= 0:
                continue
            p = conditional(*dist.team_over(side, line))
            text = fmt_line(line)
            markets.append(make_market(f"{side}_over_{text}", TEAM_TOTAL, p, odds))
            markets.append(make_market(f"{side}_under_{text}", TEAM_TOTAL, complement(p), odds))

    _, odd = dist.summary()
    markets.append(make_market("odd", PARITY, odd, odds))
    markets.append(make_market("even", PARITY, 1 - odd, odds))

    # First half: half the rates, sd / sqrt(2). Not settleable from a final score.
    half_margin, half_sd = dist.margin / 2, dist.margin_sd / math.sqrt(2)
    ht_home = 1 - c.NORMAL.cdf((0.5 - half_margin) / half_sd)
    ht_away = c.NORMAL.cdf((-0.5 - half_margin) / half_sd)
    for key, p, text in (
        ("ht_1", ht_home, "Prima repriză: gazdele conduc"),
        ("ht_X", 1 - ht_home - ht_away, "Prima repriză: egal"),
        ("ht_2", ht_away, "Prima repriză: oaspeții conduc"),
    ):
        markets.append(make_market(key, FIRST_HALF, p, odds, False, text))
    half_total, half_total_sd = dist.total / 2, dist.total_sd / math.sqrt(2)
    for d in (-5, 0, 5):
        line = math.floor(half_total) + 0.5 + d
        p = 1 - c.NORMAL.cdf((line - half_total) / half_total_sd)
        text = fmt_line(line)
        markets.append(
            make_market(
                f"ht_over_{text}", FIRST_HALF, p, odds, False, f"Prima repriză: peste {text} puncte"
            )
        )
        markets.append(
            make_market(
                f"ht_under_{text}",
                FIRST_HALF,
                1 - p,
                odds,
                False,
                f"Prima repriză: sub {text} puncte",
            )
        )
    return markets


def analyze(fixture, history, threshold=0.85, *, params=PARAMS, **_):
    """Full pre-match basketball analysis in the common shape (sports.validate_analysis)."""
    index = c.as_index(history, SPORT)
    view = model_view(fixture, index, params)
    home_rows, away_rows = view["home_rows"], view["away_rows"]
    minutes, odds = view["minutes"], fixture.odds
    scale = minutes / 48

    # Standard deviations: residuals of the local fit shrunk towards the priors.
    hint = reference_total(odds) or view["total"]
    margin_prior = params.margin_sd * math.sqrt(scale)
    total_prior = params.total_sd_base + params.total_sd_slope * hint
    sds = view["sds"]
    margin_sd = shrink_sd(margin_prior, sds and sds[0], sds[2] if sds else 0, params)
    total_sd = shrink_sd(total_prior, sds and sds[1], sds[2] if sds else 0, params)
    margin_sd = min(18.0, max(8.0, margin_sd))
    total_sd = min(28.0, max(10.0, total_sd))

    market = market_view(odds, margin_sd, total_sd, minutes, hint, view["margin"])
    quality = min(
        team_score(home_rows, fixture.kickoff, params.half_life),
        team_score(away_rows, fixture.kickoff, params.half_life),
    )
    weight = 1 - (1 - params.market_weight) * quality
    margin, total = view["margin"], view["total"]
    if market["margin"] is not None:
        margin = (1 - weight) * margin + weight * market["margin"]
    if market["total"] is not None:
        total = (1 - weight) * total + weight * market["total"]
    margin = min(80.0, max(-80.0, margin))
    total = min(400.0, max(40.0, total))

    dist = Distribution(margin, total, margin_sd, total_sd, minutes)
    expected_margin, _ = dist.summary()
    expected_total = dist.expected_total()
    mu_home = (expected_total + expected_margin) / 2
    mu_away = (expected_total - expected_margin) / 2
    markets = build_markets(dist, odds, (total + margin) / 2, (total - margin) / 2)

    confidence, grade = c.confidence_of(
        home_rows, away_rows, fixture.kickoff, bool(market["winner"]), params.half_life
    )
    sufficient = grade != "D"
    selection, reason = c.choose(markets, threshold, sufficient)
    forms = {
        "home": rich_form(home_rows, fixture.kickoff),
        "away": rich_form(away_rows, fixture.kickoff),
    }
    h2h = c.head_to_head(home_rows, fixture)
    by_key = {m["key"]: m for m in markets}
    p_home = by_key["1"]["probability"]
    favourite = by_key["1"] if p_home >= 0.5 else by_key["2"]
    totals = [m for m in markets if m["group"] == TOTAL]
    spreads = [m for m in markets if m["group"] == SPREAD]
    tips = [
        c.pick("Câștigător", favourite),
        c.pick("Handicap", min(spreads, key=lambda m: abs(m["probability"] - 0.6))),
        c.pick("Total puncte", min(totals, key=lambda m: abs(m["probability"] - 0.5))),
    ]
    value = c.value_tip(markets)
    if value:
        tips.append(value)

    name = fixture.home if p_home >= 0.5 else fixture.away
    summary = (
        f"Modelul favorizează {name} ({max(p_home, 1 - p_home):.0%}), cu un avantaj estimat "
        f"de {abs(expected_margin):.1f} puncte. Scor estimat: {mu_home:.0f}-{mu_away:.0f}; "
        f"total estimat {expected_total:.1f} puncte."
    )
    if grade == "D":
        summary += " Atenție: date insuficiente, încrederea este scăzută."
    model_margin = view["margin"] if view["fit"] is not None else None
    insights = build_insights(
        fixture,
        forms,
        h2h,
        {
            "neutral": view["neutral"],
            "model_margin": model_margin,
            "market_margin": market["margin"],
        },
    )
    fit = view["fit"]
    return {
        "version": VERSION,
        "sport": SPORT,
        "threshold": threshold,
        "calibrated": False,
        "grade": grade,
        "confidence": confidence,
        "quality": "sufficient" if sufficient else "insufficient",
        "expected": {
            "home": mu_home,
            "away": mu_away,
            "margin": expected_margin,
            "total": expected_total,
            "margin_sd": margin_sd,
            "total_sd": total_sd,
            "overtime": dist.tie,
            "minutes": minutes,
        },
        "markets": markets,
        "selection": selection,
        "reason": reason,
        "tips": tips,
        "summary": summary,
        "insights": insights,
        "form": forms,
        "h2h": h2h,
        "sample": {
            "home": len(home_rows),
            "away": len(away_rows),
            "h2h": h2h["played"],
        },
        "components": {
            "model": (
                {
                    "margin": view["margin"],
                    "total": view["total"],
                    "base": view["base"],
                    "ratings": view["ratings"],
                    "adjustments": view["adjustments"],
                    "mu": fit.mu,
                    "home_factor": fit.home,
                }
                if fit
                else None
            ),
            "fit_matches": fit.matches if fit else 0,
            "market": market,
            "market_weight": weight,
            "data_quality": quality,
            "regulation": {"margin": margin, "total": total},
            "neutral": view["neutral"],
            "population": dict(zip(("youth", "women"), population(fixture), strict=True)),
            "params": asdict(params),
        },
    }
