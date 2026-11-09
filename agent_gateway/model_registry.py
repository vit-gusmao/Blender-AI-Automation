"""Cadastro de provedores a partir de config/providers.yaml (§7.2)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .providers import ProviderConfig, create_provider


@dataclass
class ModelRegistry:
    configs: Dict[str, ProviderConfig]
    roles: Dict[str, str]

    def get(self, provider_id: str) -> ProviderConfig:
        config = self.configs.get(provider_id)
        if config is None:
            raise KeyError(
                f"provedor não cadastrado: {provider_id} "
                f"(disponíveis: {sorted(self.configs)})")
        return config

    def default_id(self) -> Optional[str]:
        return next(iter(self.configs), None)

    def validate_all(self) -> List[str]:
        errors: List[str] = []
        for config in self.configs.values():
            errors.extend(config.validate())
        return errors

    def describe(self) -> List[Dict[str, Any]]:
        return [
            {"id": c.provider_id, "adapter": c.adapter, "model": c.model,
             "base_url": c.base_url, "tool_calling": c.tool_calling,
             "vision": c.vision, "configured": not c.validate()}
            for c in self.configs.values()
        ]


def _load_yaml(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except ImportError:
        from universal_blender_mcp.settings import _mini_yaml

        return _mini_yaml(text) or {}


def load_registry(providers_yaml: Path) -> ModelRegistry:
    data = _load_yaml(providers_yaml) if providers_yaml.exists() else {}
    configs: Dict[str, ProviderConfig] = {}
    for pid, cfg in (data.get("providers") or {}).items():
        cfg = cfg or {}
        configs[pid] = ProviderConfig(
            provider_id=pid,
            adapter=cfg.get("adapter", "openai_compatible"),
            base_url=cfg.get("base_url", ""),
            model=cfg.get("model", ""),
            api_key_env=cfg.get("api_key_env"),
            tool_calling=bool(cfg.get("tool_calling", True)),
            vision=bool(cfg.get("vision", False)),
            context_window=int(cfg.get("context_window", 32768)),
            max_output=int(cfg.get("max_output", 4096)),
            parameters=cfg.get("parameters") or {},
            extra=cfg.get("extra") or {},
        )
    roles = {k: v for k, v in (data.get("roles") or {}).items()}
    return ModelRegistry(configs=configs, roles=roles)
