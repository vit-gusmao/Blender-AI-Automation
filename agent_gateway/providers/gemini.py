"""Adaptador Google Gemini (generateContent + functionCall)."""
from __future__ import annotations

from typing import Any, Dict, List

from .base import ChatResult, Provider, ToolCall


class GeminiProvider(Provider):
    name = "gemini"

    def endpoint(self, payload, tools) -> str:
        base = self.config.base_url.rstrip("/")
        model = self.config.model
        return f"{base}/models/{model}:generateContent"

    def build_headers(self) -> Dict[str, str]:
        return {'Content-Type': 'application/json', 'x-goog-api-key': self.config.api_key or ''}

    def build_payload(self, system: str, messages: List[Dict[str, Any]],
                      tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        contents: List[Dict[str, Any]] = []
        for msg in messages:
            role = msg["role"]
            if role == "assistant":
                parts: List[Dict[str, Any]] = []
                if msg.get("content"):
                    parts.append({"text": msg["content"]})
                for c in msg.get("tool_calls", []):
                    parts.append({"functionCall": {"name": c["name"],
                                                   "args": c.get("arguments", {})}})
                contents.append({"role": "model",
                                 "parts": parts or [{"text": ""}]})
            elif role == "tool":
                part = {'functionResponse': {'name': msg.get('name', 'tool'),
                                            'response': {'result': msg.get('content', '')}}}
                if (contents and contents[-1]['role'] == 'user'
                        and all('functionResponse' in p for p in contents[-1]['parts'])):
                    contents[-1]['parts'].append(part)
                else:
                    contents.append({'role': 'user', 'parts': [part]})
            else:
                contents.append({"role": "user",
                                 "parts": [{"text": msg.get("content", "")}]})

        payload: Dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"maxOutputTokens": self.config.max_output,
                                 **(self.config.parameters or {})},
        }
        if tools and self.config.tool_calling:
            payload["tools"] = [{
                "functionDeclarations": [
                    {"name": t["name"],
                     "description": t.get("description", ""),
                     "parameters": t.get("input_schema", {"type": "object"})}
                    for t in tools
                ],
            }]
        return payload

    def parse_response(self, raw: Dict[str, Any]) -> ChatResult:
        candidates = raw.get("candidates") or [{}]
        parts = (candidates[0].get("content", {}) or {}).get("parts", [])
        text_bits: List[str] = []
        tool_calls: List[ToolCall] = []
        for i, part in enumerate(parts):
            if "text" in part:
                text_bits.append(part["text"])
            elif "functionCall" in part:
                call = part["functionCall"]
                tool_calls.append(ToolCall(id=f"call_{i}",
                                           name=call.get("name", ""),
                                           arguments=call.get("args", {})))
        usage = raw.get("usageMetadata", {})
        return ChatResult(content="\n".join(text_bits) or None,
                          tool_calls=tool_calls, usage=usage, raw=raw,
                          provider_id=self.config.provider_id)
