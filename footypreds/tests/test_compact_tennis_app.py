"""TenisPrediction web app, with a tiny injected model (never trains on the full dataset)."""

from fastapi.testclient import TestClient

from tenisPrediction import app as app_module
from tenisPrediction.app import MARKET_WEIGHTS, blend_probability, create_app

from .test_compact_tennis import named_predictor

# a fixed prediction day: idle decay must not depend on the wall clock
DAY = "2024-04-01"


def client():
    return TestClient(create_app(include_core=False, predictor=named_predictor()))


def test_frontend_health_and_prediction_api(monkeypatch):
    def refuse():
        raise AssertionError("the app must not train on the full dataset in tests")

    monkeypatch.setattr(app_module, "trained_model", refuse)
    with client() as http:
        page = http.get("/")
        assert page.status_code == 200
        assert "TenisPrediction" in page.text
        health = http.get("/api/health").json()
        assert health["players"] > 10 and health["version"].startswith("tenisPrediction-3")
        response = http.get(
            "/api/predict",
            params={
                "player_1": "Sinner J.",
                "player_2": "Zverev M.",
                "surface": "Hard",
                "day": DAY,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["decision"] == "selectează"
        assert body["known"] is True and body["experience_1"] > 0
        players = http.get("/api/players", params={"q": "cerun"}).json()["players"]
        assert set(players) == {"Francisco Cerundolo", "Juan Manuel Cerundolo"}


def test_tml_probabilities_use_league_facts_and_validated_blend():
    payload = [
        {
            "id": "1",
            "home": "De Minaur A.",
            "away": "Zverev M.",
            "league": "ATP - SINGLES: Roland Garros (France), clay",
            "market_probability": 0.6,
            "day": DAY,
        },
        {"id": "2", "home": "Sinner J.", "away": "Nobody X.", "market_probability": 0.7},
        {"id": "3", "home": "De Minaur A.", "away": "Zverev M.", "odds_1": 1.5, "odds_2": 2.7},
    ]
    with client() as http:
        body = http.post("/api/tml-probabilities", json=payload).json()
    known, unknown, priced = body["matches"]
    assert tuple(body["market_weights"]) == MARKET_WEIGHTS == (0.25, 0.70)
    assert known["known"] is True and known["model_probability"] is not None
    # the displayed probability is the validated logit blend of model and market
    expected = blend_probability(known["model_probability"], 0.6)
    assert abs(known["probability"] - expected) < 1e-5
    assert known["market_probability"] == 0.6
    assert known["decision"] in {"selectează", "fără pariu"}
    assert known["pick"] in {"1", "2"} and known["validated"] is True
    assert unknown["known"] is False and unknown["decision"] == "fără pariu"
    assert unknown["probability"] == 0.7 and unknown["pick"] is None
    # raw prices: the margin is removed server-side (power method), fair p1 > 1/1.5 normalised
    assert 0.63 < priced["market_probability"] < 0.67
    assert priced["market_probability"] != round((1 / 1.5) / (1 / 1.5 + 1 / 2.7), 6)


def test_without_market_price_the_v3_model_decides_alone():
    payload = [{"id": "1", "home": "Sinner J.", "away": "Zverev M.", "day": DAY}]
    with client() as http:
        (match,) = http.post("/api/tml-probabilities", json=payload).json()["matches"]
    assert match["probability"] == match["model_probability"] > 0.8
    assert match["pick"] == "1" and match["pick_name"] == "Sinner J."
    assert match["decision"] == "selectează"


def test_pick_and_selection_follow_the_blended_probability():
    # regression: v2 gives Sinner ~99% but the market 30%; the blend decides both the pick and
    # "selectează", so the label can never be attached to the side the number does not favour
    payload = [
        {
            "id": "1",
            "home": "Sinner J.",
            "away": "Zverev M.",
            "market_probability": 0.3,
            "day": DAY,
        },
        {"id": "2", "home": "Sinner J.", "away": "Zverev M.", "market_probability": 0.02},
    ]
    with client() as http:
        first, second = http.post("/api/tml-probabilities", json=payload).json()["matches"]
    assert first["model_probability"] > 0.8
    assert abs(first["probability"] - blend_probability(first["model_probability"], 0.3)) < 1e-5
    assert (first["pick"] == "1") == (first["probability"] >= 0.5)
    assert first["pick_name"] == ("Sinner J." if first["pick"] == "1" else "Zverev M.")
    assert second["probability"] < 0.5 and second["pick"] == "2"
    assert second["pick_name"] == "Zverev M."
    for match in (first, second):
        if match["decision"] == "selectează":
            assert max(match["probability"], 1 - match["probability"]) >= match["threshold"]
        assert match["decision_high"] in {"selectează", "fără pariu"}
        if match["decision_high"] == "selectează":
            assert match["decision"] == "selectează"


def test_predict_endpoint_with_raw_odds_keeps_every_field_on_the_same_player():
    # /api/predict takes the real 1/2 prices: the margin is removed server-side and the
    # winner, the displayed probability and both decisions come from the blended number
    params = {"player_1": "Sinner J.", "player_2": "Zverev M.", "surface": "Hard", "day": DAY}
    with client() as http:
        alone = http.get("/api/predict", params=params).json()
        priced = http.get("/api/predict", params={**params, "odds_1": 9.0, "odds_2": 1.07}).json()
        posted = http.post(
            "/api/tml-probabilities",
            json=[
                {
                    "id": "1",
                    "home": "Sinner J.",
                    "away": "Zverev M.",
                    "surface": "Hard",
                    "day": DAY,
                    "odds_1": 9.0,
                    "odds_2": 1.07,
                }
            ],
        ).json()["matches"][0]
    assert alone["market_probability_1"] is None and alone["winner"] == "Sinner J."
    assert alone["probability_1"] == alone["model_probability_1"] > 0.8
    assert priced["model_probability_1"] == alone["model_probability_1"]
    assert 0 < priced["market_probability_1"] < 0.2  # 9.0 / 1.07 without the margin
    expected = blend_probability(priced["model_probability_1"], priced["market_probability_1"])
    assert abs(priced["probability_1"] - expected) < 1e-3
    assert priced["winner"] == ("Sinner J." if priced["probability_1"] >= 0.5 else "Zverev M.")
    for body in (priced, alone):
        chosen = max(body["probability_1"], body["probability_2"])
        if body["decision"] == "selectează":
            assert chosen >= body["threshold"] - 1e-4
        if body["decision_high"] == "selectează":
            assert body["decision"] == "selectează" and chosen >= body["threshold_high"] - 1e-4
    # the board endpoint agrees with /api/predict on the same prices
    assert abs(posted["probability"] - priced["probability_1"]) < 1e-3
    assert posted["pick_name"] == priced["winner"]
    assert posted["pick"] == ("1" if priced["winner"] == "Sinner J." else "2")
    assert posted["decision"] == priced["decision"]
    assert posted["decision_high"] == priced["decision_high"]


def test_unvalidated_competitions_never_select():
    payload = [
        {
            "id": "1",
            "home": "Sinner J.",
            "away": "Zverev M.",
            "league": "ITF MEN - SINGLES: M25 Monastir (Tunisia), hard",
            "day": DAY,
        }
    ]
    with client() as http:
        (match,) = http.post("/api/tml-probabilities", json=payload).json()["matches"]
    assert match["known"] is True and match["validated"] is False
    assert match["decision"] == "fără pariu"


def test_health_does_not_block_while_the_model_trains(monkeypatch):
    started = []
    monkeypatch.setattr(app_module, "_trained", None)
    monkeypatch.setattr(app_module, "ensure_warming", lambda: started.append(True))
    with TestClient(create_app(include_core=False, warm=True)) as http:
        health = http.get("/api/health").json()
        assert health["status"] == "loading" and health["players"] == 0
        busy = http.get("/api/players", params={"q": "x"})
        assert busy.status_code == 503 and "antrenează" in busy.json()["detail"]
        assert http.post("/api/tml-probabilities", json=[]).status_code == 503
    assert started  # the startup hook and the requests start the background training


def test_blend_without_market_keeps_the_model():
    assert blend_probability(0.8, None) == 0.8
    assert abs(blend_probability(0.8, 0.5, weights=(0.5, 0.5)) - 2 / 3) < 1e-9
    assert abs(blend_probability(0.8, 0.5, weights=(0.0, 1.0)) - 0.5) < 1e-12
