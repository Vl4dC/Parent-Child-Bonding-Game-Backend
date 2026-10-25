from app.config import Settings
from app.generation import model_settings
from learning_service import quiz_graph
from learning_service.visuals import harness


def test_luna_main_and_haiku_visuals_keep_distinct_provider_routes(monkeypatch):
    calls = []
    monkeypatch.setattr(
        quiz_graph,
        "init_chat_model",
        lambda model, **kwargs: calls.append((model, kwargs)) or object(),
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")
    token = model_settings.set(Settings(app_env="test", _env_file=None))
    try:
        quiz_graph._llm()
        harness.drawer_llm()
    finally:
        model_settings.reset(token)
    assert [model for model, _ in calls] == [
        "openai:openai/gpt-6-luna",
        "openai:anthropic/claude-haiku-5.5",
    ]
    assert all(options["base_url"] == "https://openrouter.ai/api/v1" for _, options in calls)
