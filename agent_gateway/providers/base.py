"""Base dos adaptadores de provedor + HTTP utilitário (stdlib)."""
from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

REQUEST_TIMEOUT_S = 120.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def redact(value: Any, secrets) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[REDACTED]')
        return value
    if isinstance(value, dict):
        return {redact(k, secrets): redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    return value


def _read_bounded(stream) -> bytes:
    data = stream.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise RuntimeError('ResponseTooLarge: provider response exceeds byte limit')
    return data


@dataclass
class ProviderConfig:
    provider_id: str
    adapter: str
    base_url: str
    model: str
    api_key_env: Optional[str] = None
    tool_calling: bool = True
    vision: bool = False
    context_window: int = 32768
    max_output: int = 4096
    parameters: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def api_key(self) -> Optional[str]:
        if not self.api_key_env:
            return None
        return os.environ.get(self.api_key_env)

    def validate(self) -> List[str]:
        errors = []
        if not self.base_url:
            errors.append(f"[{self.provider_id}] base_url ausente")
        if self.model in ("", "MODEL_ID"):
            errors.append(f"[{self.provider_id}] model não configurado "
                          "(ainda MODEL_ID)")
        if self.api_key_env and not self.api_key:
            errors.append(f"[{self.provider_id}] variável {self.api_key_env} "
                          "não definida")
        return errors


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if (not isinstance(self.id, str) or not self.id or
                not isinstance(self.name, str) or not self.name.strip() or
                not isinstance(self.arguments, dict)):
            raise ValueError('MalformedToolCall: id/name/object arguments required')


@dataclass
class ChatResult:
    content: Optional[str]
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)
    provider_id: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def validate(self) -> None:
        if self.raw.get('error') is not None:
            raise ValueError('ProviderError: provider returned an error envelope')
        if self.content is not None and not isinstance(self.content, str):
            raise ValueError('MalformedResponse: assistant content must be text')
        if not self.tool_calls and not (self.content or '').strip():
            raise ValueError('MalformedResponse: no assistant content or tool calls')


class Provider:
    """Interface única sobre APIs heterogêneas."""

    name = "base"

    def __init__(self, config: ProviderConfig):
        self.config = config

    # -- API pública -----------------------------------------------------------

    async def chat(self, system: str, messages: List[Dict[str, Any]],
                   tools: List[Dict[str, Any]]) -> ChatResult:
        secrets = [self.config.api_key]
        try:
            payload = self.build_payload(system, messages, tools)
            headers = self.build_headers()
            url = self.endpoint(payload, tools)
            raw = await self._post_json(url, payload, headers)
            if not isinstance(raw, dict):
                raise ValueError('MalformedResponse: expected JSON object')
            if raw.get('error') is not None:
                raise ValueError('ProviderError: provider returned an error envelope')
            result = self.parse_response(redact(raw, secrets))
            result.validate()
            ids = [c.id for c in result.tool_calls]
            if len(set(ids)) != len(ids):
                raise ValueError('MalformedToolCall: duplicate call ids')
            return result
        except Exception as exc:
            raise RuntimeError(redact(str(exc), secrets)) from None

    # -- hooks por adaptador ------------------------------------------------------

    def endpoint(self, payload: Dict[str, Any],
                 tools: List[Dict[str, Any]]) -> str:
        raise NotImplementedError

    def build_headers(self) -> Dict[str, str]:
        return {"Content-Type": "application/json"}

    def build_payload(self, system: str, messages: List[Dict[str, Any]],
                      tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        raise NotImplementedError

    def parse_response(self, raw: Dict[str, Any]) -> ChatResult:
        raise NotImplementedError

    # -- HTTP -----------------------------------------------------------------------

    async def _post_json(self, url: str, payload: Dict[str, Any],
                         headers: Dict[str, str]) -> Dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, default=str)
        return await asyncio.to_thread(self._post_sync, url, body, headers,
                                       timeout_s=getattr(self, 'request_timeout_s', REQUEST_TIMEOUT_S))

    @staticmethod
    def _post_sync(url: str, body: str, headers: Dict[str, str],
                   timeout_s: float = REQUEST_TIMEOUT_S) -> Dict[str, Any]:
        secrets = [v.removeprefix('Bearer ') for k, v in headers.items()
                   if k.lower() in ('authorization', 'x-api-key', 'x-goog-api-key')]
        req = urllib.request.Request(
            url, data=body.encode("utf-8"), method="POST",
            headers={"User-Agent": "universal-agent-gateway/0.1", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                value = json.loads(_read_bounded(resp).decode('utf-8'))
                if not isinstance(value, dict):
                    raise RuntimeError('MalformedResponse: expected JSON object')
                return value
        except urllib.error.HTTPError as exc:
            try:
                detail = _read_bounded(exc).decode('utf-8', 'replace')[:2000]
            finally:
                exc.close()
            raise RuntimeError(redact(f'HTTP {exc.code}: {detail}', secrets)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Transport exceptions may themselves contain the complete URL.
            raise RuntimeError(f'ProviderUnavailable: {type(exc).__name__}') from None
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError('MalformedResponse: invalid provider JSON') from None
