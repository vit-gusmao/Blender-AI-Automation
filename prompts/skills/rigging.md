# Skill — Rigging e Armatures (Blender 3.6)

## Quando usar
Criação de esqueletos, edição de ossos, parenting e pesos.

## Ferramentas reais desta implementação
- `armature_create`: `name` + `bones[]` com `name`, `head`, `tail`,
  `parent` opcional, `use_connect`, `radius`; cria o objeto na cena, entra em
  Edit Mode na thread principal e retorna ao modo objeto.
- `bone_edit`: edita `head`, `tail`, `roll`, `parent`, `use_connect`; cria o
  osso se não existir; `delete: true` remove o osso.
- `rig_assign_weights`: `mesh`, `armature`,
  `mode = AUTOMATIC_WEIGHTS | ENVELOPE_WEIGHTS` (executa
  `object.parent_set` com o tipo correspondente),
  `clear_existing` para limpar vertex groups antes.

## Fluxo recomendado
1. `armature_create` com a hierarquia de ossos (pais antes dos filhos).
2. `bone_edit` para ajustar posições e roll.
3. `rig_assign_weights` para vincular a malha ao armature e gerar pesos.
4. Refine pesos com vertex groups via `python_execute_blender` quando
   necessário.
5. Teste poses com `animation_keyframe` antes de ciclos completos.

## Regras do Blender 3.6
- Edição de ossos usa `armature.data.edit_bones` em Edit Mode; a ferramenta
  gerencia a entrada/saída do modo — não envolva chamadas em outros scripts
  que mudem o modo do objeto.
- `use_connect = true` exige que o filho comece onde o pai termina.
- Pesos automáticos exigem mesh + armature selecionados e armature ativa;
  a ferramenta prepara esse contexto, mas operadores podem falhar sem janela
  3D (background) — o erro é devolvido estruturado.
- Parenting por `rig_assign_weights` também cria a relação de parentesco
  (com modificador Armature via `parent_set`).

## Erros comuns
- Ossos com nome duplicado quebram IK/constraints.
- Roll incorreto distorce rotações; revise `roll` após grandes edições.
- `rig_assign_weights` e o ciclo de rigging **não são exercitados pelo smoke
  test** — valide o resultado com `object_get_info` (bones, vertex groups)
  e inspeção visual antes de assumir sucesso.
