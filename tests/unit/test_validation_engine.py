import asyncio

from agent_gateway.validation_engine import ValidationEngine
from universal_blender_mcp.authorization import PathPolicy


def test_object_lookup_is_direct_even_beyond_first_thousand():
    class MCP:
        async def call_tool(self, name, args):
            if name == 'object_get_info' and args == {'name': 'Object_1001'}:
                return {'parsed': {'success': True, 'data': {'name': 'Object_1001'}}, 'is_error': False}
            return {'parsed': {'data': {'objects': []}}, 'is_error': False}
    result = asyncio.run(ValidationEngine(MCP()).evaluate([{'kind': 'object_exists', 'name': 'Object_1001'}]))
    assert result['passed']


def test_file_criterion_is_confined_to_policy_and_root(tmp_path):
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    (allowed / 'yes.txt').write_text('ok')
    (tmp_path / 'secret.txt').write_text('secret')
    engine = ValidationEngine(None, root=tmp_path, path_policy=PathPolicy(allow=[allowed]))
    denied = asyncio.run(engine.evaluate([{'kind': 'file_exists', 'path': str(tmp_path / 'secret.txt')}]))
    assert not denied['passed']
    assert 'PolicyDenied' in denied['results'][0]['detail']
    assert asyncio.run(engine.evaluate([{'kind': 'file_exists', 'path': 'allowed/yes.txt'}]))['passed']


def test_failed_scene_info_cannot_pass_zero_count():
    class MCP:
        async def call_tool(self, *args):
            return {'parsed': {'success': False, 'error': {'type': 'BridgeUnavailable'}}, 'is_error': True}
    assert not asyncio.run(ValidationEngine(MCP()).evaluate([{'kind': 'object_count_min', 'count': 0}]))['passed']


def test_missing_policy_fails_closed_for_files(tmp_path):
    p = tmp_path / 'exists'
    p.write_text('ok')
    assert not asyncio.run(ValidationEngine(None).evaluate([{'kind': 'file_exists', 'path': str(p)}]))['passed']


def test_empty_allowlist_cannot_probe_host_files(tmp_path):
    p = tmp_path / 'secret'
    p.write_text('ok')
    engine = ValidationEngine(None, root=tmp_path, path_policy=PathPolicy())
    assert not asyncio.run(engine.evaluate([{'kind': 'file_exists', 'path': str(p)}]))['passed']
