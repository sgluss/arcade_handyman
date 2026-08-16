"""Model-resolution tests: one env knob picks the pipeline model, and the
eval gate's case model follows it provider-for-provider unless overridden."""

from handyman.llm import eval_model_name


def test_eval_model_defaults_to_haiku_on_the_same_provider(monkeypatch):
    monkeypatch.delenv("HANDYMAN_EVAL_MODEL", raising=False)
    monkeypatch.setenv("HANDYMAN_MODEL", "anthropic:claude-sonnet-5")
    assert eval_model_name() == "anthropic:claude-haiku-4-5"
    monkeypatch.setenv("HANDYMAN_MODEL", "bedrock:us.anthropic.claude-sonnet-5")
    assert eval_model_name().startswith("bedrock:us.anthropic.claude-haiku-4-5")


def test_eval_model_follows_the_pipeline_when_no_weak_tier_exists(monkeypatch):
    monkeypatch.delenv("HANDYMAN_EVAL_MODEL", raising=False)
    monkeypatch.setenv("HANDYMAN_MODEL", "openai:gpt-5")
    assert eval_model_name() == "openai:gpt-5"


def test_eval_model_override_wins(monkeypatch):
    monkeypatch.setenv("HANDYMAN_EVAL_MODEL", "anthropic:claude-sonnet-5")
    monkeypatch.setenv("HANDYMAN_MODEL", "bedrock:us.anthropic.claude-sonnet-5")
    assert eval_model_name() == "anthropic:claude-sonnet-5"
