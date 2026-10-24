import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def demo_app():
    def no_network(request):
        pytest.fail("Demo mode must never call Supabase")

    settings = Settings(app_env="test", demo_mode=True, _env_file=None)
    return create_app(settings, httpx.MockTransport(no_network))


def test_demo_actors_keep_data_separate_without_login_or_network():
    with TestClient(demo_app()) as client:
        assert client.get("/health").json()["auth_mode"] == "demo"
        parent = {"Authorization": "Bearer demo_parent"}
        child = {"Authorization": "Bearer demo_child"}
        assert (
            client.put("/api/me/profile", headers=parent, json={"display_name": "Alex"}).status_code
            == 200
        )
        assert client.get("/api/me/profile", headers=child).json()["display_name"] == "Learner"
        assert client.get("/api/me/profile", headers=parent).json()["display_name"] == "Alex"


@pytest.mark.parametrize("token", [None, "demo_access_forged", "supabase-token"])
def test_unknown_and_missing_demo_identities_are_rejected(token):
    with TestClient(demo_app()) as client:
        response = client.get(
            "/api/me/profile", headers={"Authorization": f"Bearer {token}"} if token else {}
        )
        assert response.status_code == 401


def test_demo_cannot_start_in_production_or_without_explicit_environment():
    for env in ("production", None):
        kwargs = {"app_env": env} if env else {}
        with pytest.raises(ValueError, match="DEMO_MODE is allowed only"):
            Settings(demo_mode=True, _env_file=None, **kwargs)


def test_supabase_mode_still_requires_configuration():
    with pytest.raises(ValueError, match="required when DEMO_MODE is off"):
        Settings(_env_file=None)
