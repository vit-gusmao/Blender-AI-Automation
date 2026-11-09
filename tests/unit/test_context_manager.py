import json

from agent_gateway.context_manager import ContextManager
from agent_gateway import context_manager


def test_envelope_truncation_preserves_status_error_and_json():
    raw = json.dumps({'status': 'failed', 'success': False, 'error': {'type': 'Denied'}, 'data': 'x' * 20000})
    value = json.loads(context_manager.truncate_envelope(raw, 500))
    assert value['status'] == 'failed'
    assert value['error']['type'] == 'Denied'
    assert len(json.dumps(value)) < 600


def test_budget_counts_arguments_system_tools_and_removes_whole_exchange():
    c = ContextManager(max_tokens=200, keep_tool_results=0)
    c.add_user('task')
    calls = [{'id': 'a', 'name': 'tool', 'arguments': {'long': 'x' * 5000}}]
    c.add_tool_exchange('', calls, ['{}'])
    assert c.total_tokens(system='s' * 1000, tools=[{'name': 'x' * 1000}]) > 1500
    messages = c.render()
    assert messages == [{'role': 'user', 'content': 'task'}]


def test_exchange_rejects_incomplete_results_without_mutating():
    c = ContextManager()
    import pytest
    with pytest.raises(ValueError):
        c.add_tool_exchange('', [{'id': 'a', 'name': 'x'}], [])
    assert not c.messages


def test_budget_uses_actual_provider_serialization():
    c = ContextManager(max_tokens=100)
    c.add_user('task')
    import pytest
    with pytest.raises(RuntimeError, match='ContextBudgetExceeded'):
        c.render(payload_builder=lambda messages: {'provider_metadata': 'x' * 2000, 'messages': messages})


def test_truncation_bounds_large_error_and_metadata():
    raw = json.dumps({'command_id': 'id', 'status': 'failed', 'success': False,
                      'data': None, 'error': 'x' * 20000})
    result = context_manager.truncate_envelope(raw, 500)
    assert len(result) <= 500
    assert json.loads(result)['success'] is False


def test_text_fallback_truncation_removes_call_and_result_together():
    c = ContextManager(max_tokens=100)
    c.add_user('task')
    c.add_tool_exchange('x' * 2000, [{'id': 'a', 'name': 'x', 'arguments': {}}],
                        ['{"success": true}'], native=False)
    assert c.render() == [{'role': 'user', 'content': 'task'}]


def test_text_exchange_boundaries_survive_preceding_and_repeated_eviction():
    c = ContextManager(max_tokens=200)
    c.add_user('task')
    c.add_user('old scene context')
    for name in ('old1', 'old2'):
        c.add_tool_exchange('x' * 2000, [{'id': name, 'name': name, 'arguments': {}}],
                            ['{}'], native=False)
    c.add_tool_exchange('retained action', [{'id': 'new', 'name': 'new', 'arguments': {}}],
                        ['{}'], native=False)
    messages = c.render()
    assert len(messages) == 3
    assert messages[1]['role'] == 'assistant'
    assert messages[1]['content'] == 'retained action'
    assert json.loads(messages[2]['content'])['tool_results'][0]['name'] == 'new'
