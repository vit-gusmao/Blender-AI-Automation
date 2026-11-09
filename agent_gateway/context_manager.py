"""Provider-valid conversation exchanges and structural payload budgeting."""
from __future__ import annotations
import copy
import json
from typing import Any, Dict, List, Optional


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def truncate_envelope(content: str, max_chars: int = 6000) -> str:
    if len(content) <= max_chars:
        return content
    try:
        value = json.loads(content)
    except (ValueError, TypeError):
        value = None
    if not isinstance(value, dict):
        return json.dumps({'truncated': True, 'preview': content[:max(0, max_chars - 100)]}, ensure_ascii=False)
    value['data'] = {'truncated': True}
    value['truncated'] = True
    if len(json.dumps(value)) > max_chars:
        value = {k: v for k, v in value.items() if k in
                 ('command_id', 'status', 'success', 'error', 'scene_revision', 'truncated')}
        if len(json.dumps(value)) > max_chars:
            error = value.get('error')
            if error is not None:
                value['error'] = {'type': str(error.get('type', 'TruncatedError') if isinstance(error, dict) else 'TruncatedError')[:max_chars // 8],
                                  'message': str(error.get('message', '') if isinstance(error, dict) else error)[:max_chars // 3]}
            for key in ('command_id', 'status', 'scene_revision'):
                if isinstance(value.get(key), str):
                    value[key] = value[key][:max_chars // 8]
    return json.dumps(value, ensure_ascii=False)


class ContextManager:
    def __init__(self, max_tokens: int = 24_000, keep_tool_results: int = 12):
        self.max_tokens = max_tokens
        self.keep_tool_results = keep_tool_results
        self.messages: List[Dict[str, Any]] = []
        # Half-open spans stay outside the provider-visible messages. Both
        # native and textual exchanges must be evicted as one unit.
        self._exchange_bounds: Dict[int, int] = {}

    def add_user(self, content: str) -> None:
        self.messages.append({'role': 'user', 'content': content})

    def add_assistant(self, content: Optional[str], tool_calls=None) -> None:
        self.messages.append({'role': 'assistant', 'content': content or '', 'tool_calls': tool_calls or []})

    def add_tool_result(self, call_id: str, name: str, content: str) -> None:
        self.messages.append({'role': 'tool', 'tool_call_id': call_id,
                              'name': name, 'content': truncate_envelope(content)})

    def add_tool_exchange(self, content, calls, results, native: bool = True) -> None:
        ids = [c['id'] for c in calls]
        if len(calls) != len(results) or len(set(ids)) != len(ids):
            raise ValueError('each distinct tool call requires exactly one result')
        start = len(self.messages)
        if native:
            self.add_assistant(content, calls)
            for call, result in zip(calls, results):
                self.add_tool_result(call['id'], call['name'], result)
        else:
            self.add_assistant(content or json.dumps({'actions': calls}), None)
            self.add_user(json.dumps({'tool_results': [
                {'name': c['name'], 'result': truncate_envelope(r)} for c, r in zip(calls, results)]}, ensure_ascii=False))
        self._exchange_bounds[start] = len(self.messages)

    def total_tokens(self, system: str = '', tools=None, payload_builder=None) -> int:
        payload = (payload_builder(self.messages) if payload_builder else
                   {'system': system, 'messages': self.messages, 'tools': tools or []})
        return estimate_tokens(json.dumps(payload, ensure_ascii=False, default=str))

    def truncate(self, system: str = '', tools=None, payload_builder=None) -> None:
        while len(self.messages) > 1 and self.total_tokens(system, tools, payload_builder) > self.max_tokens:
            end = self._exchange_bounds.get(1, 2)
            if self.messages[1].get('tool_calls'):
                while end < len(self.messages) and self.messages[end]['role'] == 'tool':
                    end += 1
            del self.messages[1:end]
            removed = end - 1
            self._exchange_bounds = {start - removed: stop - removed
                                     for start, stop in self._exchange_bounds.items()
                                     if start >= end}

    def render(self, system: str = '', tools=None, payload_builder=None) -> List[Dict[str, Any]]:
        self.truncate(system, tools, payload_builder)
        if self.total_tokens(system, tools, payload_builder) > self.max_tokens:
            raise RuntimeError('ContextBudgetExceeded: system, tools and task exceed budget')
        return copy.deepcopy(self.messages)
