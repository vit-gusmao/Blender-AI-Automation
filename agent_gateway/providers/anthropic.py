"""Adaptador Anthropic Messages API (com blocos tool_use)."""
from __future__ import annotations

import json
from typing import Any, Dict, List

from .base import ChatResult, Provider, ToolCall


class AnthropicProvider(Provider):
    name = "anthropic"

    def endpoint(self, payload, tools) -> str:
        base = self.config.base_url.rstrip("/")
        if "/v1/messages" in base:
            return base
        return f"{base}/v1/messages"

    def build_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.config.api_key or "",
            "anthropic-version": "2023-06-01",
        }

    def build_payload(self, system: str, messages: List[Dict[str, Any]],
                      tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        out: List[Dict[str, Any]] = []
        for msg in messages:
            role = msg["role"]
            if role == "assistant":
                blocks = []
                if msg.get("content"):
                    blocks.append({"type": "text", "text": msg["content"]})
                for c in msg.get("tool_calls", []):
                    blocks.append({"type": "tool_use", "id": c["id"],
                                   "name": c["name"], "input": c.get("arguments", {})})
                out.append({"role": "assistant",
                            "content": blocks or [{"type": "text", "text": ""}]})
            elif role == "tool":
                block = {'type': 'tool_result', 'tool_use_id': msg.get('tool_call_id'),
                         'content': msg.get('content', '')}
                if (out and out[-1]['role'] == 'user' and isinstance(out[-1]['content'], list)
                        and all(b.get('type') == 'tool_result' for b in out[-1]['content'])):
                    out[-1]['content'].append(block)
                else:
                    out.append({'role': 'user', 'content': [block]})
            else:
                out.append({"role": role, "content": msg.get("content", "")})

        payload: Dict[str, Any] = {
            "model": self.config.model,
            "system": system,
            "max_tokens": self.config.max_output,
            "messages": out,
        }
        payload.update(self.config.parameters or {})
        if tools and self.config.tool_calling:
            payload["tools"] = [
                {"name": t["name"], "description": t.get("description", ""),
                 "input_schema": t.get("input_schema", {"type": "object"})}
                for t in tools
            ]
        return payload

    def parse_response(self, raw: Dict[str, Any]) -> ChatResult:
        content_text: List[str] = []
        tool_calls: List[ToolCall] = []
        for block in raw.get("content", []):
            if block.get("type") == "text":
                content_text.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(ToolCall(id=block.get("id", ""),
                                           name=block.get("name", ""),
                                           arguments=block.get("input", {})))
        usage = raw.get("usage", {})
        return ChatResult(content="\n".join(content_text) or None,
                          tool_calls=tool_calls, usage=usage, raw=raw,
                          provider_id=self.config.provider_id)
