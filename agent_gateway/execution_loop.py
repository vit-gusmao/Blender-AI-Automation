"""Ciclo de execução do agente: OBSERVE → PLAN → ACT → VERIFY → REPAIR → REPORT (§9)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .context_manager import ContextManager
from .providers import Provider, ToolCall
from .tool_call_adapter import parse_json_action, tool_defs_for_provider
from .validation_engine import ValidationEngine

EventCallback = Callable[[Dict[str, Any]], Awaitable[None]]


@dataclass
class RunOutcome:
    status: str
    stop_reason: str
    report: Dict[str, Any]
    verification: Dict[str, Any] = field(default_factory=dict)


class ExecutionLoop:
    def __init__(self, provider: Provider, mcp, system_prompt: str,
                 max_turns: int = 24, max_tool_calls_per_turn: int = 8,
                 on_event: Optional[EventCallback] = None, max_events: int = 100,
                 context_max_tokens: int = 24000, validation_root=None,
                 max_event_bytes: int = 16384):
        self.provider = provider
        self.mcp = mcp
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.max_tool_calls_per_turn = max_tool_calls_per_turn
        self.on_event = on_event
        window = getattr(provider.config, 'context_window', 32768)
        output = getattr(provider.config, 'max_output', 4096)
        self.context = ContextManager(max_tokens=min(context_max_tokens, max(1, window - output)))
        self.max_events = max(1, max_events)
        self.max_event_bytes = max_event_bytes
        self.validation_root = validation_root
        self._tool_events = 0
        self.tools: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []

    async def _emit(self, kind: str, **data: Any) -> None:
        event = {"ts": time.time(), "kind": kind, **data}
        if kind == 'tool_call':
            self._tool_events += 1
        if len(json.dumps(event, ensure_ascii=False, default=str).encode()) > self.max_event_bytes:
            event = {'ts': event['ts'], 'kind': kind, 'truncated': True}
        self.events.append(event)
        del self.events[:-self.max_events]
        if self.on_event:
            await self.on_event(event)

    # -- OBSERVE ------------------------------------------------------------------

    async def observe(self, task: str) -> None:
        self.context.add_user(task)
        await self._emit("observe_start")
        for tool_name in ("blender_ping", "scene_get_info"):
            try:
                out = await self.mcp.call_tool(tool_name, {})
                from .context_manager import truncate_envelope
                self.context.add_user(
                    f"[estado inicial via {tool_name}] {truncate_envelope(out['content'], 2000)}")
            except RuntimeError as exc:
                await self._emit("observe_error", tool=tool_name, error=str(exc))
        await self._emit("observe_done")

    # -- ACT/REPAIR ------------------------------------------------------------------

    async def _execute_call(self, call: ToolCall) -> str:
        await self._emit("tool_call", name=call.name,
                         arguments=call.arguments)
        try:
            out = await self.mcp.call_tool(call.name, call.arguments)
        except RuntimeError as exc:
            await self._emit("tool_error", name=call.name, error=str(exc))
            return json.dumps({"success": False,
                               "error": {"type": "MCPError", "message": str(exc)}},
                              ensure_ascii=False)
        await self._emit("tool_result", name=call.name,
                         is_error=out["is_error"],
                         preview=out["content"][:400])
        return out["content"]

    async def run(self, task: str,
                  plan: Optional[Dict[str, Any]] = None) -> RunOutcome:
        """Executa a tarefa e devolve o relatório final (REPORT)."""
        started = time.time()
        self.tools = tool_defs_for_provider(await self.mcp.list_tools())
        await self._emit("start", tools=len(self.tools))

        await self.observe(task)
        if plan and not plan.get("fallback"):
            self.context.add_user(
                "PLANO APROVADO (siga esta ordem; marque progresso):\n"
                + json.dumps(plan, ensure_ascii=False, indent=1)[:4000])

        final_text: Optional[str] = None
        stop_reason = "max_turns"
        turn = 0
        while turn < self.max_turns:
            turn += 1
            await self._emit("turn_start", turn=turn)
            try:
                result = await self.provider.chat(
                    system=self.system_prompt,
                    messages=self.context.render(self.system_prompt, self.tools,
                        payload_builder=(lambda messages: self.provider.build_payload(
                            self.system_prompt, messages, self.tools))
                        if hasattr(self.provider, 'build_payload') else None),
                    tools=self.tools)
                result.validate()
            except Exception as exc:
                await self._emit("provider_error", error=str(exc), turn=turn)
                stop_reason = "provider_error"
                final_text = f"Falha no provedor: {exc}"
                break

            calls = list(result.tool_calls)
            if not calls and self.provider.config.tool_calling is False:
                # nível 2/3 (§7.3): extrai ação JSON do texto
                parsed = parse_json_action(result.content or "")
                if parsed:
                    calls = [parsed]
                    await self._emit("fallback_action", name=parsed.name)

            if not calls:
                final_text = result.content or "(sem resposta do modelo)"
                stop_reason = "completed"
                self.context.add_assistant(result.content, None)
                break

            results = []
            for index, call in enumerate(calls):
                content = (await self._execute_call(call) if index < self.max_tool_calls_per_turn
                           else json.dumps({'success': False, 'status': 'failed',
                                            'error': {'type': 'ToolCallLimit', 'message': 'turn call limit exceeded'}}))
                results.append(content)
                parsed_ok = content.strip().startswith("{")
                if parsed_ok:
                    try:
                        envelope = json.loads(content)
                        if envelope.get("success") is False and \
                                (envelope.get("error") or {}).get("type") in (
                                "Unauthorized", "PolicyDenied"):
                            await self._emit(
                                "permission_denied", name=call.name,
                                reason=(envelope.get("error") or {}).get("message"))
                    except json.JSONDecodeError:
                        pass
            self.context.add_tool_exchange(result.content,
                [{'id': c.id, 'name': c.name, 'arguments': c.arguments} for c in calls],
                results, native=self.provider.config.tool_calling)

        report = {
            "report": final_text or "limite de turnos atingido sem resposta final",
            "stop_reason": stop_reason,
            "turns": turn,
            "tool_events": self._tool_events,
            "duration_s": round(time.time() - started, 1),
            "provider": self.provider.config.provider_id,
            "model": self.provider.config.model,
        }
        await self._emit("done", **{k: v for k, v in report.items()
                                    if k != "events"})
        return RunOutcome('completed' if stop_reason == 'completed' else 'failed',
                          stop_reason, report)

    # -- VERIFY ------------------------------------------------------------------------

    async def verify(self, criteria: List[Dict[str, Any]]) -> Dict[str, Any]:
        engine = ValidationEngine(self.mcp, root=self.validation_root)
        outcome = await engine.evaluate(criteria)
        await self._emit("verify", passed=outcome["passed"])
        return outcome
