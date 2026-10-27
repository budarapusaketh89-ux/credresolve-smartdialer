from fastapi.testclient import TestClient

from app.main import app


def test_dashboard_and_static_assets_are_served():
    client = TestClient(app)

    page = client.get("/")
    stylesheet = client.get("/static/styles.css")
    script = client.get("/static/dashboard.js")

    assert page.status_code == 200
    assert "SmartDialer Overview" in page.text
    assert stylesheet.status_code == 200
    assert "color-scheme" in stylesheet.text
    assert script.status_code == 200
    assert 'fetch("/simulate"' in script.text


def test_existing_health_and_simulation_endpoints_remain_available():
    client = TestClient(app)

    health = client.get("/health")
    simulation = client.post(
        "/simulate",
        json={
            "agent_count": 2,
            "duration_seconds": 2,
            "answer_rate": 0.3,
            "avg_talk_seconds": 2,
            "mode": "PROGRESSIVE",
            "max_calls": 2,
            "seed": 3,
        },
    )

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert simulation.status_code == 200
    assert simulation.json()["result_kind"] == "modeled_simulation"
    assert simulation.json()["capacity_invariant_holds"] is True
