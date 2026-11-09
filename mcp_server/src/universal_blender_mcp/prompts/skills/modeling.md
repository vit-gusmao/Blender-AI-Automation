# Skill — Modelagem (Blender 3.6)

## Quando usar
Criação e edição de geometria: primitivas, malhas editadas, modificadores, booleans, validação de topologia.

## Ferramentas reais desta implementação
- `object_create`: tipos suportados `CUBE`, `UV_SPHERE`, `IO_SPHERE`,
  `CYLINDER`, `CONE`, `TORUS`, `PLANE`, `MONKEY`, `EMPTY`, `ARMATURE`,
  `CAMERA` (não existe `SPHERE` — use `UV_SPHERE`). Nome repetido é
  reutilizado (idempotência).
- `object_transform`: `location`, `rotation` (radianos XYZ), `scale`;
  `rotation_mode` opcional (em `QUATERNION`, a rotação informada em radianos
  XYZ é convertida para quaternion).
- `mesh_edit`: `operations` é uma lista de `{"op": ..., "params": {...}}`
  aplicada em sequência via bmesh na thread principal.
- `mesh_apply_modifier`: `action = ADD | APPLY | REMOVE`; `ADD` exige `type`;
  `params` só define atributos que realmente existem no modificador.
- `mesh_boolean`: `operation = UNION | DIFFERENCE | INTERSECT`,
  `solver = EXACT | FAST`; o operando é removido por padrão
  (`delete_operand: false` para preservá-lo).
- `mesh_validate`: chama `mesh.validate()` e devolve contagens +
  `had_errors`.
- `scene_validate` / `object_compare`: validação estrutural e comparação de
  objetos (transform, geometry, materials, modifiers).

## Fluxo recomendado
1. `scene_get_info` / `scene_list_objects` para entender a cena.
2. Criar bases com `object_create` e posicionar com `object_transform`.
3. Refinar com `mesh_edit` e `mesh_apply_modifier`.
4. Combinar formas com `mesh_boolean`.
5. Finalizar com `mesh_validate` e `scene_validate` antes de salvar/exportar.

## Operações de `mesh_edit` suportadas
`EXTRUDE_REGION`, `EXTRUDE_FACES`, `EXTRUDE_VERTICES`, `INSET_FACES`
(`thickness`, `depth`), `BEVEL` (`width`, `segments`, `profile`),
`SUBDIVIDE_EDGES`/`SUBDIVIDE_FACE` (`cuts`), `DELETE_FACES/EDGES/VERTICES`,
`MERGE_VERTICES` (`distance`), `RECALC_NORMALS`, `FLIP_NORMALS`,
`SMOOTH_VERTICES` (`factor`), `TRIANGULATE`, `FILL_HOLE` (`sides`),
`SHADE_SMOOTH`, `SHADE_FLAT`, `SET_ORIGIN_TO_GEOMETRY`, `MIRROR_X/Y/Z`.

**`LOOP_CUT` não é suportado** (retorna `NotImplementedError` estruturado):
exige contexto interativo de edição. Alternativas: `SUBDIVIDE_EDGES` ou
`python_execute_blender` com bmesh.

## Regras do Blender 3.6
- Prefira `bpy.data` e `bmesh` a `bpy.ops` quando possível (§5.1 do plano).
- `bpy.ops` exige contexto correto; o addon aplica `temp_override` e seleção
  quando necessário, mas em `--background` operadores dependentes de janela
  podem falhar — o erro volta estruturado.
- Nomes: use prefixo do projeto (ex.: `Espada_Lamina`) para idempotência e
  rastreio.
- `bpy.ops` no 3.6 cria wrappers dinâmicos para qualquer nome: operador
  inexistente só falha no `poll()`/execução ("could not be found"). Não
  presuma que um caminho existe por não dar erro no `getattr`.

## Erros comuns
- Boolean com solver inadequado gera NGons — valide com `mesh_validate`
  depois (`EXACT` é o padrão da ferramenta).
- `mesh_edit` opera em modo objeto e atualiza a malha ao final; não assuma
  estado de `Edit Mode` preservado entre chamadas.
- Extrude sem deslocamento (`offset = 0`) cria geometria coincidente;
  informe `offset` quando quiser deslocar a região extrudada.
