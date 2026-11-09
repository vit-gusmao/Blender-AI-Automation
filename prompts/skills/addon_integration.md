# Skill — Integração com Addons do Blender (3.6)

## Quando usar
Descobrir e invocar operadores de addons instalados, importadores/
exportadores extras e ferramentas de modding — sem alterar o servidor.

## Ferramenta real desta implementação
`addon_operator_invoke` (nível `project_ops`, risco alto) com `mode`:

| mode | Entrada | Resultado |
|---|---|---|
| `LIST_ADDONS` | — | Addons ativos em `bpy.context.preferences.addons` |
| `LIST_OPERATORS` | `filter` opcional (prefixo, ex.: `object.`) | Até 300 `bl_idname` de operadores registrados |
| `OPERATOR_INFO` | `operator` (bl_idname, ex.: `object.modifier_add`) | Propriedades com tipo, descrição e default |
| `INVOKE` | `operator` + `props` | Executa com `EXEC_DEFAULT` e override de contexto 3D quando disponível |

## Fluxo recomendado
1. `LIST_ADDONS` para confirmar o addon ativo.
2. `LIST_OPERATORS` com `filter` para descobrir os operadores.
3. `OPERATOR_INFO` para verificar propriedades válidas antes de invocar.
4. `INVOKE` com `props` — somente propriedades que existem no operador são
   passadas (as demais são ignoradas). Confira o campo `result`
   (`{'FINISHED'}` etc.) e valide o efeito na cena.

## Regras do Blender 3.6
- Operadores podem depender de contexto (área, região, modo, seleção). O
  addon aplica `temp_override` com window/screen/area VIEW_3D/region quando
  disponível; em background, operadores dependentes de janela podem falhar
  com erro estruturado.
- Addons desativados não expõem operadores.
- `INVOKE` usa execução direta (não abre diálogos de `INVOKE_DEFAULT`).
- `addon_operator_invoke` está em `DESTRUCTIVE_OPERATIONS` do addon: com
  `auto_checkpoint` ativo, um checkpoint é criado antes da invocação.
- O mesmo mecanismo de resolução com `poll()` vale para importadores/
  exportadores usados por `asset_import`/`asset_export` (ex.: OBJ em 3.6 usa
  `wm.obj_import`/`wm.obj_export`, com fallback para os operadores legados).

## Erros comuns
- Propriedades inexistentes são **filtradas silenciosamente** — use
  `OPERATOR_INFO` e confira o efeito real depois.
- Operador desconhecido gera erro `NotFound` com o `bl_idname`.
- Importadores de terceiros podem registrar handlers globais; evite
  invocá-los em lote sem validação.
