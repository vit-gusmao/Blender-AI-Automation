"""Adaptador OpenAI-compatible: OpenAI, LM Studio, DeepSeek, GLM, Kimi etc."""
from __future__ import annotations

import json
from typing import Any, Dict, List

from .base import ChatResult, Provider, ToolCall


class OpenAICompatibleProvider(Provider):
    name = "openai_compatible"

    def endpoint(self, payload, tools) -> str:
        return f"{self.config.base_url.rstrip('/')}/chat/completions"

    def build_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def build_payload(self, system: str, messages: List[Dict[str, Any]],
                      tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        out: List[Dict[str, Any]] = [{"role": "system", "content": system}]
        for msg in messages:
            role = msg["role"]
            if role == "assistant" and msg.get("tool_calls"):
                calls = [{
                    "id": c["id"], "type": "function",
                    "function": {"name": c["name"],
                                 "arguments": json.dumps(
                                     c.get("arguments", {}), ensure_ascii=False)},
                } for c in msg["tool_calls"]]
                out.append({"role": "assistant",
                            "content": msg.get("content") or None,
                            "tool_calls": calls})
            elif role == "tool":
                out.append({"role": "tool", "tool_call_id": msg.get("tool_call_id"),
                            "content": msg.get("content", "")})
            else:
                out.append({"role": role, "content": msg.get("content", "")})

        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": out,
            "max_tokens": self.config.max_output,
        }
        payload.update(self.config.parameters or {})
        if tools and self.config.tool_calling:
            payload["tools"] = [
                {"type": "function",
                 "function": {"name": t["name"],
                              "description": t.get("description", ""),
                              "parameters": t.get("input_schema",
                                                  {"type": "object"})}}
                for t in tools
            ]
            payload["tool_choice"] = "auto"
        return payload

    def parse_response(self, raw: Dict[str, Any]) -> ChatResult:
        choices = raw.get('choices')
        if (not isinstance(choices, list) or not choices or not isinstance(choices[0], dict)
                or not isinstance(choices[0].get('message'), dict)):
            raise ValueError('MalformedResponse: missing assistant message in choices')
        choice = choices[0]
        message = choice['message']
        tool_calls: List[ToolCall] = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function", {})
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    raise ValueError('MalformedToolCall: arguments must be JSON object') from None
            tool_calls.append(ToolCall(id=call.get("id", ""),
                                       name=fn.get("name", ""),
                                       arguments=args))
        return ChatResult(content=message.get("content"),
                          tool_calls=tool_calls,
                          usage=raw.get("usage", {}),
                          raw=raw, provider_id=self.config.provider_id)
