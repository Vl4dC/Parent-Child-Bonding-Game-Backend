import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_demo_needs_no_tokens_or_provider_configuration():
    settings = Settings(app_env="test", demo_mode=True, _env_file=None)
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/api/me/profile").status_code == 200
        assert client.put("/api/me/profile", json={"display_name": "Alex"}).status_code == 200
        assert client.get("/api/me/profile").json()["display_name"] == "Alex"


def test_demo_cannot_start_in_production_or_without_explicit_environment():
    for env in ("production", None):
        kwargs = {"app_env": env} if env else {}
        with pytest.raises(ValueError, match="DEMO_MODE is allowed only"):
            Settings(demo_mode=True, _env_file=None, **kwargs)


def test_storage_mode_has_no_auth_configuration_requirement():
    settings = Settings(_env_file=None)
    assert not settings.demo_mode
    assert settings.app_env == "production"
