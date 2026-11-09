import asyncio
import json
from types import SimpleNamespace

from agent_gateway.execution_loop import ExecutionLoop
from agent_gateway.providers.base import ChatResult, ToolCall


class MCP:
    def __init__(self):
        self.calls = []

    async def list_tools(self):
        return []

    async def call_tool(self, name, arguments):
        self.calls.append(name)
        return {'content': '{"success": true}', 'parsed': {'success': True}, 'is_error': False}


class Model:
    def __init__(self, results, native=True):
        self.config = SimpleNamespace(tool_calling=native, provider_id='fake', model='fake')
        self.results = iter(results)
        self.histories = []

    async def chat(self, **kwargs):
        self.histories.append(list(kwargs['messages']))
        return next(self.results)


def test_observe_and_plan_are_context_not_orphan_results():
    model = Model([ChatResult('done')])
    loop = ExecutionLoop(model, MCP(), 'sys')
    asyncio.run(loop.run('task', {'steps': [1]}))
    assert all(m['role'] != 'tool' for m in model.histories[0])


def test_overflow_calls_all_receive_results():
    model = Model([ChatResult('', [ToolCall(str(i), 'x') for i in range(3)]), ChatResult('done')])
    mcp = MCP()
    loop = ExecutionLoop(model, mcp, 'sys', max_tool_calls_per_turn=1)
    asyncio.run(loop.run('task'))
    history = model.histories[1]
    results = [m for m in history if m['role'] == 'tool']
    assert [m['tool_call_id'] for m in results] == ['0', '1', '2']
    assert json.loads(results[-1]['content'])['error']['type'] == 'ToolCallLimit'
    assert mcp.calls.count('x') == 1


def test_text_fallback_uses_textual_exchange():
    model = Model([ChatResult('{"tool":"x","arguments":{}}'), ChatResult('done')], native=False)
    asyncio.run(ExecutionLoop(model, MCP(), 'sys').run('task'))
    history = model.histories[1]
    assert not any(m['role'] == 'tool' or m.get('tool_calls') for m in history)
    assert any('success' in m.get('content', '') for m in history)


def test_max_turns_returns_failed_outcome():
    model = Model([ChatResult('', [ToolCall('1', 'x')])])
    result = asyncio.run(ExecutionLoop(model, MCP(), 'sys', max_turns=1).run('task'))
    assert result.status == 'failed'
    assert result.stop_reason == 'max_turns'


def test_provider_exception_returns_failed_outcome():
    class Broken(Model):
        async def chat(self, **kwargs):
            raise ValueError('invalid provider payload')
    result = asyncio.run(ExecutionLoop(Broken([]), MCP(), 'sys').run('task'))
    assert result.status == 'failed'
    assert result.stop_reason == 'provider_error'


def test_error_provider_envelope_cannot_become_completed():
    from agent_gateway.providers import OpenAICompatibleProvider, ProviderConfig
    provider = OpenAICompatibleProvider(ProviderConfig('fake', 'openai_compatible', 'http://fake', 'fake'))
    async def response(*args):
        return {'error': {'message': 'backend unavailable'}}
    provider._post_json = response
    result = asyncio.run(ExecutionLoop(provider, MCP(), 'sys').run('task'))
    assert result.status == 'failed'
    assert result.stop_reason == 'provider_error'


def test_empty_chat_result_cannot_become_completed():
    result = asyncio.run(ExecutionLoop(Model([ChatResult(None)]), MCP(), 'sys').run('task'))
    assert result.status == 'failed'
    assert result.stop_reason == 'provider_error'
