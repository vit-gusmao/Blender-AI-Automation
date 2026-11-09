import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from agent_gateway.mcp_client import MCPStdioClient


def server(mode):
    code = '''import json, sys
for line in sys.stdin:
    req = json.loads(line)
    if "id" not in req:
        continue
    if req["method"] == "initialize":
        print(json.dumps({"jsonrpc":"2.0", "id":req["id"], "result":{"serverInfo":{}}}), flush=True)
        continue
    MODE
'''.replace('MODE', mode)
    return MCPStdioClient(command=[sys.executable, '-u', '-c', code])


@pytest.mark.parametrize('mode', ['break', 'print("invalid json", flush=True)'])
def test_reader_exit_or_malformed_response_fails_every_pending_promptly(mode):
    async def scenario():
        client = server(mode)
        await client.start()
        try:
            tasks = [asyncio.create_task(client._request('request', timeout_s=10)) for _ in range(2)]
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)
            assert all(isinstance(result, RuntimeError) for result in results)
            assert not client._pending
        finally:
            await client.close()
    asyncio.run(scenario())


def test_request_timeout_cleans_registry():
    async def scenario():
        client = server('continue')
        await client.start()
        try:
            with pytest.raises(TimeoutError):
                await client._request('request', timeout_s=.02)
            assert not client._pending
        finally:
            await client.close()
    asyncio.run(scenario())


def test_close_fails_pending_and_awaits_process_reader_and_stderr():
    async def scenario():
        client = server('continue')
        await client.start()
        pending = asyncio.create_task(client._request('request'))
        await asyncio.sleep(.02)
        await client.close()
        with pytest.raises(RuntimeError, match='closed'):
            await asyncio.wait_for(pending, .2)
        assert client._process.returncode is not None
        assert client._reader_task.done()
        assert client._stderr_task.done()
        await client.close()
    asyncio.run(scenario())


def test_writes_and_notifications_are_serialized():
    async def scenario():
        client = MCPStdioClient()
        class Writer:
            active = False
            def write(self, data):
                assert not self.active, 'overlapping writers'
                self.active = True
            async def drain(self):
                await asyncio.sleep(.005)
                self.active = False
        client._process = SimpleNamespace(stdin=Writer(), returncode=None)
        await asyncio.gather(*(client._notify('note') for _ in range(5)))
    asyncio.run(scenario())


def test_tool_transport_timeout_includes_margin():
    async def scenario():
        client = MCPStdioClient()
        captured = {}
        async def request(method, params, timeout_s):
            captured['timeout'] = timeout_s
            return {'content': []}
        client._request = request
        await client.call_tool('render', {'timeout_s': 400}, timeout_s=30)
        assert captured['timeout'] >= 410
    asyncio.run(scenario())
