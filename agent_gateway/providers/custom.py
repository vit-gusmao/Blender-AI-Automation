"""Adaptador custom: OpenAI-compatible com headers/paths configuráveis.

Para endpoints "quase compatíveis" que exigem um header próprio ou caminho
diferente (ex.: ``/v2/chat/completions``).
"""
from __future__ import annotations

from typing import Any, Dict

from .openai_compatible import OpenAICompatibleProvider


class CustomProvider(OpenAICompatibleProvider):
    name = "custom"

    def endpoint(self, payload, tools) -> str:
        path = self.config.extra.get("chat_path", "/chat/completions")
        base = self.config.base_url.rstrip("/")
        if path.startswith("http"):
            return path
        return f"{base}{path}"

    def build_headers(self) -> Dict[str, str]:
        headers = super().build_headers()
        for key, value in (self.config.extra.get("headers") or {}).items():
            headers[key] = value
        return headers
