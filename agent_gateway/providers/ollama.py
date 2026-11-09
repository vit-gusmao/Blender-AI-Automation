"""Adaptador Ollama nativo (/api/chat com tools)."""
from __future__ import annotations

from typing import Any, Dict, List

from .base import ChatResult, Provider, ToolCall


class OllamaProvider(Provider):
    name = "ollama"

    def endpoint(self, payload, tools) -> str:
        return f"{self.config.base_url.rstrip('/')}/api/chat"

    def build_payload(self, system: str, messages: List[Dict[str, Any]],
                      tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        out: List[Dict[str, Any]] = [{"role": "system", "content": system}]
        for msg in messages:
            role = msg["role"]
            if role == "assistant" and msg.get("tool_calls"):
                out.append({
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "tool_calls": [{
                        "function": {"name": c["name"],
                                     "arguments": c.get("arguments", {})}
                    } for c in msg["tool_calls"]],
                })
            elif role == "tool":
                out.append({"role": "tool", "content": msg.get("content", "")})
            else:
                out.append({"role": role, "content": msg.get("content", "")})

        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": out,
            "stream": False,
            "options": {"num_predict": self.config.max_output,
                        **(self.config.parameters or {})},
        }
        if tools and self.config.tool_calling:
            payload["tools"] = [
                {"type": "function",
                 "function": {"name": t["name"],
                              "description": t.get("description", ""),
                              "parameters": t.get("input_schema",
                                                  {"type": "object"})}}
                for t in tools
            ]
        return payload

    def parse_response(self, raw: Dict[str, Any]) -> ChatResult:
        message = raw.get("message", {}) or {}
        tool_calls: List[ToolCall] = []
        for i, call in enumerate(message.get("tool_calls") or []):
            fn = call.get("function", {})
            tool_calls.append(ToolCall(
                id=fn.get("id") or f"call_{i}",
                name=fn.get("name", ""),
                arguments=fn.get("arguments", {})))
        return ChatResult(content=message.get("content"),
                          tool_calls=tool_calls,
                          usage={"eval_count": raw.get("eval_count", 0)},
                          raw=raw, provider_id=self.config.provider_id)
