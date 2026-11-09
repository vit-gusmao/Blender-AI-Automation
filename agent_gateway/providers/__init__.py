"""Adaptadores de provedores LLM (§7.2).

Formato interno de mensagens (normalizado):
  {"role": "user"|"assistant"|"tool", "content": str,
   "tool_calls": [{"id", "name", "arguments": dict}],   # assistant
   "tool_call_id": str, "name": str}                    # tool

Cada adaptador converte para o formato da sua API e normaliza a resposta
para {"content": str|None, "tool_calls": [...], "usage": {...}, "raw": ...}.
"""
from .base import Provider, ProviderConfig, ChatResult, ToolCall
from .openai_compatible import OpenAICompatibleProvider
from .anthropic import AnthropicProvider
from .gemini import GeminiProvider
from .ollama import OllamaProvider
from .custom import CustomProvider

ADAPTERS = {
    "openai_compatible": OpenAICompatibleProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "ollama": OllamaProvider,
    "custom": CustomProvider,
}


def create_provider(config: ProviderConfig) -> Provider:
    cls = ADAPTERS.get(config.adapter)
    if cls is None:
        raise ValueError(
            f"adaptador desconhecido: {config.adapter} "
            f"(disponíveis: {sorted(ADAPTERS)})")
    return cls(config)


__all__ = [
    "Provider", "ProviderConfig", "ChatResult", "ToolCall", "ADAPTERS",
    "create_provider",
    "OpenAICompatibleProvider", "AnthropicProvider", "GeminiProvider",
    "OllamaProvider", "CustomProvider",
]
