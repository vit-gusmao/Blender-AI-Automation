"""Unit: payload building dos providers (sem rede)."""
from __future__ import annotations

import json
import asyncio
import io
import urllib.error
import pytest

from agent_gateway.providers import (AnthropicProvider, GeminiProvider,
                                     OllamaProvider, OpenAICompatibleProvider,
                                     ProviderConfig)


def _config(adapter, **kw):
    return ProviderConfig("test", adapter, "http://127.0.0.1/v1",
                          "model-x", **kw)


MESSAGES = [
    {"role": "user", "content": "crie um cubo"},
    {"role": "assistant", "content": "ok",
     "tool_calls": [{"id": "c1", "name": "object_create",
                     "arguments": {"type": "CUBE"}}]},
    {"role": "tool", "tool_call_id": "c1", "name": "object_create",
     "content": '{"success": true}'},
]

TOOLS = [{"name": "object_create", "description": "cria objeto",
          "input_schema": {"type": "object"}}]


def test_openai_payload_roundtrip():
    provider = OpenAICompatibleProvider(_config("openai_compatible"))
    payload = provider.build_payload("SYS", MESSAGES, TOOLS)
    assert payload["messages"][0] == {"role": "system", "content": "SYS"}
    assert payload["messages"][1]["role"] == "user"
    call = payload["messages"][2]["tool_calls"][0]
    assert call["function"]["name"] == "object_create"
    assert json.loads(call["function"]["arguments"]) == {"type": "CUBE"}
    assert payload["messages"][3]["role"] == "tool"
    assert payload["tools"][0]["type"] == "function"

    raw = {"choices": [{"message": {
        "content": "feito",
        "tool_calls": [{"id": "c2", "function": {
            "name": "scene_get_info", "arguments": "{\"limit\": 1}"}}]}}]}
    result = provider.parse_response(raw)
    assert result.content == "feito"
    assert result.tool_calls[0].arguments == {"limit": 1}


def test_anthropic_payload_blocks():
    provider = AnthropicProvider(_config("anthropic"))
    payload = provider.build_payload("SYS", MESSAGES, TOOLS)
    assert payload["system"] == "SYS"
    assistant = payload["messages"][1]
    assert assistant["content"][1] == {"type": "tool_use", "id": "c1",
                                       "name": "object_create",
                                       "input": {"type": "CUBE"}}
    tool_result = payload["messages"][2]
    assert tool_result["content"][0]["type"] == "tool_result"

    raw = {"content": [{"type": "text", "text": "ok"},
                       {"type": "tool_use", "id": "u1",
                        "name": "blender_ping", "input": {}}]}
    result = provider.parse_response(raw)
    assert result.tool_calls[0].name == "blender_ping"


def test_gemini_payload_parts():
    provider = GeminiProvider(_config("gemini"))
    payload = provider.build_payload("SYS", MESSAGES, TOOLS)
    assert payload["systemInstruction"]["parts"][0]["text"] == "SYS"
    fn_call = payload["contents"][1]["parts"][1]["functionCall"]
    assert fn_call["name"] == "object_create"
    fn_resp = payload["contents"][2]["parts"][0]["functionResponse"]
    assert fn_resp["name"] == "object_create"

    raw = {"candidates": [{"content": {"parts": [
        {"text": "pronto"},
        {"functionCall": {"name": "scene_get_info", "args": {}}}]}}]}
    result = provider.parse_response(raw)
    assert result.content == "pronto"
    assert result.tool_calls[0].name == "scene_get_info"


def test_ollama_payload():
    provider = OllamaProvider(_config("ollama"))
    payload = provider.build_payload("SYS", MESSAGES, TOOLS)
    assert payload["stream"] is False
    assert payload["tools"][0]["function"]["name"] == "object_create"
    assert payload["messages"][2]["tool_calls"][0]["function"]["arguments"] == \
        {"type": "CUBE"}

    raw = {"message": {"content": "ok", "tool_calls": [
        {"function": {"name": "blender_ping", "arguments": {}}}]}}
    result = provider.parse_response(raw)
    assert result.tool_calls[0].name == "blender_ping"


def test_gemini_key_is_header_only(monkeypatch):
    monkeypatch.setenv('GEMINI_TEST_KEY', 'secret-value')
    provider = GeminiProvider(_config('gemini', api_key_env='GEMINI_TEST_KEY'))
    assert 'secret-value' not in provider.endpoint({}, [])
    assert '?' not in provider.endpoint({}, [])
    assert provider.build_headers()['x-goog-api-key'] == 'secret-value'


def test_provider_http_error_redacts_header_secret_and_url(monkeypatch):
    key = 'secret-test-value'
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError('http://host/?key=' + key, 403, 'Forbidden', {}, io.BytesIO(('bad ' + key).encode()))
    monkeypatch.setattr('urllib.request.urlopen', fail)
    provider = GeminiProvider(_config('gemini'))
    with pytest.raises(RuntimeError) as error:
        provider._post_sync('http://host/?key=' + key, '{}', {'x-goog-api-key': key})
    assert key not in str(error.value)
    assert key not in repr(error.value.__cause__)


def test_provider_limits_response_bytes(monkeypatch):
    from agent_gateway.providers import base
    monkeypatch.setattr(base, 'MAX_RESPONSE_BYTES', 128, raising=False)
    class Response(io.BytesIO):
        def read(self, size=-1):
            assert size > 0, 'unbounded read'
            return super().read(size)
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: Response(b'{"data":"' + b'x' * 1000 + b'"}'))
    with pytest.raises(RuntimeError, match='ResponseTooLarge'):
        GeminiProvider(_config('gemini'))._post_sync('http://host', '{}', {})


@pytest.mark.parametrize('adapter,raw', [
    (GeminiProvider, {'candidates': [{'content': {'parts': [
        {'functionCall': {'name': 'a', 'args': {}}}, {'functionCall': {'name': 'b', 'args': []}}]}}]}),
    (OpenAICompatibleProvider, {'choices': [{'message': {'tool_calls': [
        {'id': 'a', 'function': {'name': 'a', 'arguments': '{}'}},
        {'id': 'b', 'function': {'name': 'b', 'arguments': 'broken'}}]}}]}),
    (AnthropicProvider, {'content': [{'type': 'tool_use', 'id': 'a', 'name': 'a', 'input': []}]}),
    (OllamaProvider, {'message': {'tool_calls': [{'function': {'name': '', 'arguments': {}}}]}}),
])
def test_malformed_parallel_calls_reject_entire_response(adapter, raw):
    with pytest.raises((ValueError, RuntimeError)):
        adapter(_config('fake')).parse_response(raw)


def test_provider_chat_redacts_returned_text_and_errors(monkeypatch):
    monkeypatch.setenv('KEY_TEST', 'a-secret-api-key')
    p = GeminiProvider(_config('gemini', api_key_env='KEY_TEST'))
    async def response(*args):
        return {'candidates': [{'content': {'parts': [{'text': 'echo a-secret-api-key'}]}}]}
    p._post_json = response
    result = asyncio.run(p.chat('', [], []))
    assert 'a-secret-api-key' not in result.content
    assert 'a-secret-api-key' not in str(result.raw)


@pytest.mark.parametrize('adapter,container,key', [
    (AnthropicProvider, 'messages', 'content'),
    (GeminiProvider, 'contents', 'parts'),
])
def test_parallel_results_form_one_following_provider_turn(adapter, container, key):
    messages = [
        {'role': 'user', 'content': 'task'},
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'a', 'name': 'first', 'arguments': {}},
            {'id': 'b', 'name': 'second', 'arguments': {}}]},
        {'role': 'tool', 'tool_call_id': 'a', 'name': 'first', 'content': '{}'},
        {'role': 'tool', 'tool_call_id': 'b', 'name': 'second', 'content': '{}'},
    ]
    payload = adapter(_config('fake')).build_payload('sys', messages, TOOLS)
    assert len(payload[container]) == 3
    assert len(payload[container][-1][key]) == 2


def test_ollama_rejects_empty_nonobject_arguments():
    raw = {'message': {'tool_calls': [{'function': {'name': 'tool', 'arguments': []}}]}}
    with pytest.raises(ValueError, match='MalformedToolCall'):
        OllamaProvider(_config('ollama')).parse_response(raw)


@pytest.mark.parametrize('adapter,raw', [
    (OpenAICompatibleProvider, {'error': {'message': 'backend unavailable'}}),
    (OpenAICompatibleProvider, {'error': {'message': 'backend unavailable'},
                               'choices': [{'message': {'content': 'done'}}]}),
    (OpenAICompatibleProvider, {}),
    (OpenAICompatibleProvider, {'choices': []}),
    (OpenAICompatibleProvider, {'choices': [{}]}),
    (OpenAICompatibleProvider, {'choices': [{'message': {}}]}),
    (OpenAICompatibleProvider, {'choices': [{'message': {'content': ''}}]}),
    (GeminiProvider, {'error': {'message': 'backend unavailable'}}),
    (GeminiProvider, {'candidates': []}),
    (GeminiProvider, {'promptFeedback': {'blockReason': 'SAFETY'}}),
    (AnthropicProvider, {'type': 'error', 'error': {'message': 'unavailable'}}),
    (AnthropicProvider, {'content': []}),
    (OllamaProvider, {'error': 'backend unavailable'}),
    (OllamaProvider, {'message': {}}),
])
def test_chat_rejects_error_envelopes_and_missing_assistant_reply(adapter, raw):
    provider = adapter(_config('fake'))
    async def response(*args):
        return raw
    provider._post_json = response
    with pytest.raises(RuntimeError, match='ProviderError|MalformedResponse'):
        asyncio.run(provider.chat('sys', [{'role': 'user', 'content': 'task'}], []))


def test_chat_accepts_native_tool_only_reply():
    provider = OpenAICompatibleProvider(_config('openai_compatible'))
    async def response(*args):
        return {'choices': [{'message': {'content': None, 'tool_calls': [
            {'id': 'a', 'function': {'name': 'x', 'arguments': '{}'}}]}}]}
    provider._post_json = response
    result = asyncio.run(provider.chat('sys', [], TOOLS))
    assert result.tool_calls[0].name == 'x'
