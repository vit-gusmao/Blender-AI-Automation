"""Planejamento antes de modificar a cena (§9.1)."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

PLANNING_PROMPT = """\
Você está planejando uma tarefa no Blender 3.6 executada por ferramentas MCP.

TAREFA:
{task}

ESTADO ATUAL DA CENA:
{scene}

Responda SOMENTE com um objeto JSON válido, sem texto extra:
{{
  "steps": [
    {{"id": 1, "description": "...", "tools": ["object_create"],
      "depends_on": [], "success_check": "..."}}
  ],
  "risks": ["..."],
  "notes": "..."
}}
"""


class PlanningEngine:
    def __init__(self, provider, max_steps: int = 20, fallback: str = 'fail'):
        self.provider = provider
        self.max_steps = max_steps
        if fallback not in ('fail', 'direct'):
            raise ValueError('planner fallback must be fail or direct')
        self.fallback = fallback

    @staticmethod
    def _direct(task, reason):
        return {'steps': [{'id': 1, 'description': task, 'tools': [], 'depends_on': [],
                           'success_check': 'explicit task verification'}],
                'fallback': True, 'mode': 'direct', 'fallback_reason': reason}

    async def plan(self, task: str, scene_summary: Dict[str, Any],
                   tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        tool_names = [t["name"] for t in tools][:60]
        prompt = PLANNING_PROMPT.format(
            task=task,
            scene=json.dumps(scene_summary, ensure_ascii=False, default=str)[:3000],
        ) + f"\nFERRAMENTAS DISPONÍVEIS: {', '.join(tool_names)}"

        try:
            result = await self.provider.chat(
                system="Você é um planejador de tarefas 3D. Responda apenas JSON.",
                messages=[{"role": "user", "content": prompt}], tools=[])
        except Exception:
            if self.fallback == 'direct':
                return self._direct(task, 'planner_provider_error')
            raise
        from .tool_call_adapter import parse_json_action  # nível 2/3 §7.3
        fallback = parse_json_action(result.content or "")
        data: Dict[str, Any] = {}
        text = result.content or ""
        if text.strip().startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = {}
        if not data and fallback and fallback.name == "steps":
            data = fallback.arguments
        steps = data.get("steps") if isinstance(data, dict) else None
        if (not isinstance(steps, list) or not steps or len(steps) > self.max_steps
                or any(not isinstance(step, dict) or not isinstance(step.get('description'), str)
                       or not step['description'].strip() for step in steps)):
            if self.fallback != 'direct':
                raise RuntimeError('PlannerInvalidOutput: expected a nonempty list of valid steps')
            # sem plano parseável → etapa única (execução direta)
            return self._direct(task, 'planner_invalid_output')
        return {"steps": steps[:self.max_steps], "fallback": False,
                "risks": data.get("risks", []),
                "notes": data.get("notes", "")}
