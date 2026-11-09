"""Unit: registry/router de provedores (providers.yaml)."""
from __future__ import annotations

import pytest

from agent_gateway.model_registry import load_registry
from agent_gateway.model_router import ModelRouter
from agent_gateway.providers import (ProviderConfig, create_provider,
                                     OpenAICompatibleProvider,
                                     AnthropicProvider, GeminiProvider,
                                     OllamaProvider)


def test_load_registry_from_repo_config():
    registry = load_registry(__import__("pathlib").Path(__file__).parents[2]
                             / "config" / "providers.yaml")
    ids = set(registry.configs)
    assert {"local_ollama", "local_lmstudio", "cloud_anthropic",
            "cloud_gemini", "cloud_custom"} <= ids
    assert registry.roles["vision"] == "cloud_gemini"


def test_unconfigured_models_flagged():
    config = ProviderConfig("x", "openai_compatible", "http://x", "MODEL_ID")
    errors = config.validate()
    assert any("MODEL_ID" in e for e in errors)


def test_create_provider_by_adapter():
    for adapter, cls in (
        ("openai_compatible", OpenAICompatibleProvider),
        ("anthropic", AnthropicProvider),
        ("gemini", GeminiProvider),
        ("ollama", OllamaProvider),
    ):
        config = ProviderConfig("p", adapter, "http://127.0.0.1", "m")
        assert isinstance(create_provider(config), cls)
    with pytest.raises(ValueError):
        create_provider(ProviderConfig("p", "nope", "http://x", "m"))


def test_router_roles_and_default():
    registry = load_registry(__import__("pathlib").Path(__file__).parents[2]
                             / "config" / "providers.yaml")
    router = ModelRouter(registry)
    assert router.default_id is not None
    planner = router.for_role("planner")
    assert planner.config.provider_id == registry.roles.get(
        "planner", router.default_id)
    routing = router.describe_routing()
    assert set(routing) == {"default", "planner", "executor"}
