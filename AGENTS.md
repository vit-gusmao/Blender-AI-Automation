# AGENTS.md — Instruções para agentes (Codex / Claude Code / Cursor / OpenCode)

Este projeto implementa o plano `docs/Plan-Auto-Blender.txt`. Agentes que
operam este repositório devem seguir as regras abaixo. O `README.md` contém a
referência completa de instalação, ferramentas, configuração e limitações; a
matriz de evidências do programa de correção está em
`docs/superpowers/plans/2026-11-08-audit-fix-evidence.md`.

## Contexto do sistema

- **Alvo**: Blender 3.6 no Windows (verificado com
  `C:\Blender\blender 3.6\blender.exe`; `bl_info` anuncia `(3, 6, 0)`). Use
  somente a [API do Blender 3.6](https://docs.blender.org/api/3.6/) — nunca a
  documentação de 4.x/5.x.
- **Dois Pythons separados** (§3.2): interno (3.10, embutido no Blender, só
  stdlib + bpy/bmesh/mathutils) e externo (venv `.venv`, SDK MCP etc.;
  neste ambiente, Python 3.15). Nunca instale pacotes no Python do Blender;
  nunca importe `bpy` no processo externo — toda interação com uma sessão
  aberta passa pela Bridge.
- **Thread principal** (§5.1): nenhum `bpy` fora da thread principal do
  Blender. O addon processa a fila via `bpy.app.timers`
  (`dispatcher.tick`), alimentada por `bridge_client` (thread de rede); o
  desligamento faz `join` dessa thread.

## Como operar o Blender por este repo

1. Ferramentas MCP do servidor `universal_blender_mcp` (**55 ferramentas** do
   §6.1; **49 operações** no dispatcher do addon; 6 resolvidas no servidor).
   Descubra com `blender_get_capabilities` ou `python -m
   universal_blender_mcp inspect` (o `inspect` é livre de efeitos colaterais).
2. Prefira **ferramentas estruturadas**; use `python_execute_blender` apenas
   para o que elas não cobrem (nível `advanced`, aprovação explícita
   `approval: true` na chamada, sujeito à política e ao scan estático do
   addon). `requested_by` NUNCA é aprovação.
3. Contrato de resultado: envelope `{command_id, status, success, data,
   scene_revision, error}`. Todo handler local devolve esse envelope. Trate
   `success: false` + `error.type` como fonte única de verdade — nunca repita
   o mesmo comando sem investigar antes.
4. Valide após cada alteração relevante (`scene_list_objects`,
   `object_get_info`, `scene_validate`, `render_inspect`) e corrija falhas
   recuperáveis (ciclo OBSERVE→PLAN→ACT→VERIFY→REPAIR→REPORT, §9).
5. Antes de operações destrutivas (`object_delete`, `project_open`), crie
   checkpoint com `project_checkpoint` (o servidor também agenda checkpoint
   automático quando `auto_checkpoint` está ativo).

## Preservação e segurança

- Não sobrescreva `.blend` originais; trabalhe em cópias/checkpoints.
- Não exclua objetos/materiais/actions fora do escopo da tarefa.
- Caminhos autorizados ficam em `config/policies.yaml` — allowlist
  **fail-closed** (vazia nega tudo), aplicada no servidor para ferramentas e
  recursos; o addon pode restringir ainda mais pelas preferências. Caminhos
  `//…` são resolvidos contra o projeto `.blend` ativo e recusados sem esse
  contexto.
- Tokens ficam em `storage/bridge_token` (Bridge), `storage/http_token` ou
  `BLENDER_MCP_HTTP_TOKEN` (MCP HTTP) e `BLENDER_GATEWAY_TOKEN` /
  `storage/gateway_token` (Gateway) — nunca em arquivos versionados.
- `addon_operator_invoke` com `mode = INVOKE` arbitrário exige nível
  `advanced` e aprovação explícita.
- Nunca tente contornar os níveis de autorização; eles são aplicados no
  servidor (ferramentas **e** recursos `blender://`) e no addon, não apenas
  descritos neste arquivo.
- `python_execute_blender` não é sandbox: é execução controlada por política,
  com scan estático de padrões perigosos; o timeout da Bridge não interrompe
  código já em execução dentro do Blender.

## Padrões de código

- `blender_addon/` e a Bridge (`bridge_server.py`): **apenas stdlib**;
  compatível com Python 3.10. Imports novos no addon são verificados por
  `tests/compatibility/test_addon_static.py`.
- `blender_addon/universal_mcp/__init__.py` **não importa `bpy` no topo**
  (submódulos puros precisam ser importáveis fora do Blender para testes).
- `mcp_server/`: pacote instalável (`pip install -e mcp_server`), depende do
  SDK MCP v2 (2.3.0 validado). O servidor registra handlers na **API nova**
  (`add_request_handler(method, ParamsClass, _compat(handler))`, com
  `params_type` = classes `*RequestParams`; listagens usam
  `PaginatedRequestParams`) **e** nos decoradores legados. Recursos
  `blender://` são construídos com `TextResourceContents` (o dataclass
  `ReadResourceContents` do SDK atual não aceita `uri=`/`text=`) e passam pelo
  gate único `authorize_request`. Ao atualizar o SDK, rode
  `tests/integration/test_mcp_stdio.py` e
  `tests/integration/test_network_auth.py`.
- O transporte MCP HTTP exige Bearer (`storage/http_token` ou
  `BLENDER_MCP_HTTP_TOKEN`); fora do loopback o token deve estar
  explicitamente configurado. stdio continua sem Bearer (fronteira = nível de
  sessão).
- Estrutura de pastas segue §4 do plano (acréscimo: `agent_gateway/
  mcp_client.py`, exigido pelo fluxo §13.5); nomes de ferramentas do §6.1 são
  **contrato** — não renomeie sem atualizar tests + docs.
- Testes: pytest. Suba `tests/` junto com qualquer mudança de comportamento;
  `tests/blender_background` roda no Blender real.

## Fluxo de trabalho

1. Leia o plano (§ relevante) antes de mudar arquitetura.
2. Rode `python -m universal_blender_mcp inspect` e a suíte de testes antes
   de declarar conclusão (suíte externa atual: **400 testes**; smoke do
   Blender: **22 verificações**).
3. Reporte fielmente: ações executadas vs. planejadas, arquivos gerados,
   verificações e pendências.

## Diagnóstico e limitações práticas

- `python -m universal_blender_mcp inspect` lista ferramentas e nível de
  sessão **sem criar storage/token/jobs.db/logs e sem subir a Bridge**;
  `bridge` sobe só a Bridge; `jobs` consulta o histórico SQLite.
- `scripts/test-connection.ps1` executa initialize/tools-list/tools-call reais
  contra uma Bridge isolada com deadline e exit code determinístico
  (regressões em `tests/compatibility/test_powershell_scripts.py`); os
  diagnósticos `diag_*.py` compartilham cliente com prazo e encerramento
  garantidos.
- `LOOP_CUT` em `mesh_edit` retorna erro estruturado (contexto interativo);
  use `SUBDIVIDE_EDGES` ou `python_execute_blender` com bmesh.
- `bpy.ops` no Blender 3.6 cria wrappers dinâmicos para qualquer nome:
  verifique operadores com `poll()` (o dispatcher faz isso na maioria dos
  caminhos; restam dois operadores sem verificação exata em todos eles);
  `vertex_groups` pertence ao **objeto**, não ao `Mesh`.
- Cancelamento de jobs vale apenas para itens enfileirados (transição única,
  idempotente); execuções em andamento terminam ou estouram o timeout da
  Bridge.
- A compatibilidade com GUI (painel, render assíncrono) e com LLMs reais
  (chamadas faturáveis) ainda não foi exercitada — ver
  `Limitações conhecidas` no README.
