# FlashScore Predictor Pro V6

A professional football match prediction engine built in Excel VBA, combining advanced statistical models with real-time data from the FlashScore API to generate accurate match predictions and betting recommendations across 10+ European leagues.

## Table of Contents

- [Overview](#overview)
- [What's New in V6](#whats-new-in-v6)
- [Supported Leagues](#supported-leagues)
- [Prediction Models](#prediction-models)
- [Betting Markets](#betting-markets)
- [Setup & Installation](#setup--installation)
- [Usage](#usage)
- [Bankroll Management](#bankroll-management)
- [Results Tracking](#results-tracking)
- [Configuration](#configuration)
- [Version History](#version-history)
- [API Reference](#api-reference)

---

## Overview

FlashScore Predictor Pro V6 is the latest iteration of a football prediction system that uses a **Dixon-Coles model** with **Bivariate Poisson distribution**, **Bayesian shrinkage**, **Platt scaling**, and **xG blending** to produce calibrated match outcome probabilities. It fetches real-time data from the FlashScore API, enriches it with local xG datasets, and outputs actionable predictions with confidence tiers and recommended stakes.

### Key Capabilities

- Predict match outcomes (1X2, Double Chance, Draw No Bet)
- Over/Under goals and corners markets
- BTTS (Both Teams to Score) probabilities
- Correct score matrices (7x7)
- Value bet detection via Expected Value analysis
- Automated ticket generation with bankroll-safe staking
- Per-match tactical summaries and team profiling
- Full results tracking with hit rate, ROI, and Brier score

---

## What's New in V6

| Feature | V5 | V6 |
|---|---|---|
| **Platt Scaling** | A=1.2, B=-0.06 | A=1.15, B=-0.075 (better calibration) |
| **Bayesian Shrinkage** | Factor 10 | Factor 8 (stronger regression to mean) |
| **Lambda Bounds** | 0.3 - 4.0 | 0.4 - 3.5 (tighter, more realistic) |
| **Momentum Scoring** | -- | Win/draw/loss streak detection |
| **Corner Predictions** | -- | Poisson-based O/U 8.5, 9.5, 10.5 |
| **Team Profiles** | -- | ATTACKING / DEFENSIVE / BALANCED classification |
| **Win-to-Nil / Clean Sheet** | -- | Dedicated probability outputs |
| **Exact Goals Ranges** | -- | 0-1, 2-3, 4+ goal probabilities |
| **Home/Away Over 0.5 Goals** | -- | Individual team goal expectations |
| **Best Picks of the Day** | -- | Curated top recommendations across all markets |
| **Match Summaries** | -- | Generated tactical narratives per game |
| **Results Tracking Sheet** | -- | Accuracy measurement with hit rate & ROI |
| **Max Picks Per Ticket** | 4-8 | 3 (higher quality, lower variance) |

---

## Supported Leagues

| League | Country |
|---|---|
| Premier League | England |
| LaLiga | Spain |
| Serie A | Italy |
| Bundesliga | Germany |
| Ligue 1 | France |
| Superliga | Romania |
| Jupiler Pro League | Belgium |
| Super League | Switzerland |
| Primeira Liga | Portugal |
| Super Lig | Turkey |

---

## Prediction Models

### Dixon-Coles Model

The core engine uses the **Dixon-Coles** extension of the Poisson model, which corrects for the dependency between low-scoring outcomes (0-0, 1-0, 0-1, 1-1) via a tau correction factor.

- **Rho (rho):** -0.04
- **Lambda3:** 0.08 (Bivariate Poisson correlation parameter)

### Bivariate Poisson Distribution

Goal expectations for home and away teams are modeled as correlated Poisson random variables, capturing the tendency for match scorelines to be interdependent.

### Bayesian Shrinkage

Team attack and defense ratings are shrunk toward the league mean with a factor of **8**, preventing overreaction to small sample sizes early in the season.

### Platt Scaling

Final probabilities are passed through a sigmoid calibration function (A=1.15, B=-0.075) to ensure well-calibrated outputs.

### xG Blending

When available, predictions blend **70% xG data** with **30% actual goals** to better capture underlying performance rather than results variance.

### Momentum Scoring

Recent form is weighted with exponential time-decay across the last 5 matches, with additional streak bonuses/penalties for consecutive wins or losses.

### ELO-Inspired Power Ratings

Teams receive strength ratings updated after each matchday, with a home advantage boost of ~3 ELO points.

---

## Betting Markets

### Pre-Match Markets

| Market | Description |
|---|---|
| **1X2** | Home / Draw / Away win probabilities |
| **Double Chance** | 1X, X2, 12 combinations |
| **Draw No Bet** | Home or Away (draw stakes returned) |
| **Over/Under Goals** | O/U 0.5, 1.5, 2.5, 3.5 |
| **BTTS** | Both Teams to Score Yes/No |
| **Over/Under Corners** | O/U 8.5, 9.5, 10.5 |
| **Win-to-Nil** | Home/Away wins without conceding |
| **Clean Sheet** | Home/Away keeps a clean sheet |
| **Correct Score** | 7x7 scoreline probability matrix |
| **HT/FT Patterns** | Half-time / Full-time outcome combinations |
| **Exact Goals Range** | 0-1, 2-3, 4+ goals |

### Confidence Tiers

Each prediction is assigned a confidence level:

- **ELITE** - Highest confidence, strongest statistical edge
- **HIGH** - Strong confidence with clear model advantage
- **MEDIUM** - Moderate confidence, smaller edge
- **LOW** - Marginal edge, use with caution

---

## Setup & Installation

### Prerequisites

- Microsoft Excel (2016 or later recommended) with macros enabled
- Windows OS
- Internet connection for API calls
- RapidAPI key for FlashScore API (`flashscore4.p.rapidapi.com`)

### Installation Steps

1. **Clone/Download** this repository
2. **Open** `V6 predictions.xlsx` (or the `.xlsm` workbook) in Excel
3. **Enable macros** when prompted
4. **Import the VBA code** from `FLASHSCORE_PREDICTOR_PRO_V6.txt`:
   - Press `Alt+F11` to open the VBA Editor
   - Import the module or paste the code into a new module
5. **Configure API keys** in the code constants section
6. **(Optional)** Place local JSON data files in the configured data path for xG enrichment

### Local Data Files (Optional)

For enhanced xG predictions, place these JSON files in the data directory:

```
Premier League CurentSeason Statistics.json
Premier League teams Home statistics golas.json
Premier League teams Away statistics golas.json
LaLiga CurrentSeason Statistics.json
LaLiga teams Home statistics golas.json
LaLiga teams Away statistics golas.json
```

---

## Usage

### Workflow

```
1. SETUP          -->  Initialize dashboard (run once)
2. LOAD BY DATE   -->  Enter date (YYYY-MM-DD), fetch scheduled matches
3. DEEP ANALYZE   -->  Select a match, run full statistical breakdown
4. PREDICT ALL    -->  Batch predictions for all loaded matches
5. VALUE BETS     -->  View EV-filtered selections
6. CORRECT SCORES -->  View 7x7 scoreline matrices
7. TRACK RESULTS  -->  Record outcomes and measure accuracy
```

### Step-by-Step

1. **Run Setup** - Click the `SETUP` button to create the dashboard with all control buttons
2. **Load Matches** - Enter a date and click `LOAD BY DATE` to fetch all scheduled matches for that day
3. **Select a Match** - Choose a match from the list box on the dashboard
4. **Deep Analysis** - Click `DEEP ANALYZE` for a detailed breakdown including:
   - Team attack/defense ratings
   - Form analysis (last 5 matches)
   - Head-to-head history
   - xG comparison
   - Tactical summary
   - Recommended bets for this match
5. **Predict All** - Click `PREDICT ALL` to generate predictions across all matches, including:
   - Best Picks of the Day
   - Professional betting tickets (Safe / Balanced / Value)
6. **Review Output** - Check the `PREDICTIONS` sheet for probabilities and the `VALUE` sheet for EV-positive selections

### Excel Sheet Structure

| Sheet | Purpose |
|---|---|
| **MAIN** | Dashboard with controls and buttons |
| **DATA** | Raw match data from API |
| **ANALYSIS** | Detailed calculations and breakdowns |
| **PREDICTIONS** | Final predictions and betting tickets |
| **SCORES** | Correct score probability matrices |
| **VALUE** | Expected Value filtered selections |
| **RESULTS** | Accuracy tracking and statistics |

---

## Bankroll Management

The system implements a conservative, professional bankroll management framework:

| Parameter | Value |
|---|---|
| **Base Bankroll** | EUR 10 |
| **Safe Stake** | 25% (EUR 2.50) |
| **Balanced Stake** | 15% (EUR 1.50) |
| **Value Stake** | 10% (EUR 1.00) |
| **Daily Loss Limit** | 40% of bankroll |
| **Max Picks Per Ticket** | 3 |
| **Staking Model** | Quarter-Kelly Criterion |

### Ticket Types

- **Safe Ticket** - Conservative selections with highest confidence, larger stakes
- **Balanced Ticket** - Mixed confidence levels, moderate stakes
- **Value/Bold Ticket** - Higher odds selections with positive EV, smaller stakes

### Safeguards

- EV-gating: Only positive Expected Value bets qualify for tickets
- League diversity: No duplicate leagues within a single ticket
- Kelly fraction cap: Quarter-Kelly to prevent overbetting
- Daily loss limit enforced at 40% of bankroll

---

## Results Tracking

V6 introduces a built-in results tracking system:

1. Click `INIT RESULTS` to set up the tracking sheet
2. After matches complete, enter outcomes: **W** (Win), **L** (Loss), **D** (Draw), **V** (Void)
3. Click `CALC STATS` to compute:
   - **Hit Rate** - Percentage of winning predictions
   - **ROI** - Return on Investment across all bets
   - **Brier Score** - Probability calibration metric (lower = better)
   - Per-market breakdown of accuracy

---

## Configuration

### Key Constants

```vba
API_HOST          = "flashscore4.p.rapidapi.com"
DC_RHO            = -0.04       ' Dixon-Coles rho parameter
DC_LAMBDA3        = 0.08        ' Bivariate Poisson correlation
PLATT_A           = 1.15        ' Platt scaling parameter A
PLATT_B           = -0.075      ' Platt scaling parameter B
SHRINKAGE_FACTOR  = 8           ' Bayesian shrinkage strength
BANKROLL          = 10          ' Base bankroll in EUR
MAX_DAILY_LOSS    = 0.4         ' 40% daily loss limit
LAMBDA_MIN        = 0.4         ' Minimum goal expectation
LAMBDA_MAX        = 3.5         ' Maximum goal expectation
```

### API Key Rotation

V6 supports **4 API keys** with automatic rotation when rate limits are hit. Configure keys in the constants section of the code.

---

## Version History

| Version | Highlights |
|---|---|
| **V2** | Poisson regression, odds-based normalization |
| **V3** | Dixon-Coles model, power ratings, xG blending, H2H analysis |
| **V4** | Bivariate Poisson, Platt scaling, stronger H2H weighting |
| **V5** | EV-gated tickets, Quarter-Kelly staking, bankroll management, 3 ticket types |
| **V6** | Momentum scoring, improved calibration, corners model, team profiles, results tracking, best picks, match narratives |

---

## API Reference

The system integrates with the FlashScore API via RapidAPI:

| Endpoint | Purpose | Calls |
|---|---|---|
| `matches/list-by-date` | Scheduled/completed matches | 1 per load |
| `matches/details` | Match info (league, venue, scores) | 1 per match |
| `matches/match/stats` | Live stats (xG, possession, shots) | 1 per match |
| `matches/odds` | Full odds (1X2, O/U, BTTS, etc.) | 1 per match |
| `matches/h2h` | Head-to-head history | 1 per match |
| `matches/standings` | League table (overall/home/away) | 1 per league |
| `matches/standings/form` | Last 5 matches form | 1 per league |
| `matches/standings/over-under` | O/U team statistics | 1 per league |
| `matches/standings/ht-ft` | HT/FT patterns | 1 per league |

---

## Disclaimer

This tool is intended for educational and analytical purposes. Sports betting carries financial risk. Always gamble responsibly and within your means. Past prediction accuracy does not guarantee future results.

---

## License

This project is proprietary. All rights reserved.
