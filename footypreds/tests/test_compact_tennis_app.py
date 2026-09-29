from fastapi.testclient import TestClient

from tenisPrediction.app import create_app


def test_separate_frontend_and_prediction_api():
    with TestClient(create_app(include_core=False)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "TenisPrediction" in page.text
        response = client.get(
            "/api/predict",
            params={"player_1": "Jannik Sinner", "player_2": "Carlos Alcaraz", "surface": "Hard"},
        )
        assert response.status_code == 200
        assert response.json()["decision"] in {"selectează", "fără pariu"}
