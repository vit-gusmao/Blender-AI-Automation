"""Roteamento de modelos — inclusive troca durante o projeto (§7.4)."""
from __future__ import annotations

from typing import Optional

from .model_registry import ModelRegistry
from .providers import Provider, create_provider

# Only advertise stages with consumers in the execution pipeline.
KNOWN_ROLES = ("planner", "executor")


class ModelRouter:
    def __init__(self, registry: ModelRegistry, default_id: Optional[str] = None):
        self.registry = registry
        self.default_id = default_id or registry.default_id()
        self._cache: dict[str, Provider] = {}

    def provider(self, provider_id: Optional[str] = None) -> Provider:
        pid = provider_id or self.default_id
        if pid not in self._cache:
            self._cache[pid] = create_provider(self.registry.get(pid))
        return self._cache[pid]

    def for_role(self, role: str) -> Provider:
        """planner/executor/reviewer/vision — permite multi-modelo por papel."""
        pid = self.registry.roles.get(role) or self.default_id
        return self.provider(pid)

    def describe_routing(self) -> dict[str, Optional[str]]:
        return {
            "default": self.default_id,
            **{role: self.registry.roles.get(role) for role in KNOWN_ROLES},
        }
