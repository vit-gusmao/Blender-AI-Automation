# Skill — Materiais e Shaders (Blender 3.6)

## Quando usar
Criação de materiais, atribuição, propriedades de superfície, UV, texturas e
nodes de shader/geometry/compositing.

## Ferramentas reais desta implementação
- `material_create`: cria ou reutiliza o material e configura o Principled
  BSDF. Campos aceitos: `base_color` (RGBA), `metallic`, `roughness`,
  `emission_color`, `emission_strength`, `alpha`.
- `material_assign`: `object`, `material`, `slot` opcional. Sem `slot`, o
  material é adicionado como novo slot; com `slot`, slots intermediários são
  criados se necessário.
- `material_set_property`: `property` simples (atributo do material, ex.:
  `diffuse_color`, `blend_method`) ou `principled.<Nome do Input>`
  (ex.: `principled.Roughness`).
- `texture_load`: carrega imagem como textura; o caminho passa pela allowlist
  do servidor (e pela restrição de caminhos do addon, se ativa).
- `texture_assign`: cria/reutiliza um `ShaderNodeTexImage` e liga `Color` ao
  input indicado do Principled (`input`, padrão `Base Color`);
  `non_color: true` define colorspace `Non-Color`.
- `uv_unwrap`: `method = SMART_PROJECT | SPHERE_PROJECT | CYLINDER_PROJECT |
  CUBE_PROJECT | UNWRAP`, com `margin` e `angle_limit`.
- `nodes_list`, `nodes_create`, `nodes_connect`, `nodes_set_input` operam em
  `tree_type = ShaderNodeTree` (nome do material), `GeometryNodeTree` (nome de
  node group existente) ou `CompositorNodeTree` (cena atual).

## Fluxo recomendado
1. `material_create` → `material_assign`.
2. `material_set_property` para ajustes finos.
3. `nodes_list` no material para descobrir o grafo real antes de editar.
4. `nodes_create` + `nodes_connect` + `nodes_set_input` para procedural.
5. Texturas de arquivo: `texture_load` + `texture_assign`; use `non_color`
   para normal/roughness maps.
6. Valide visualmente com `render_preview` + `render_inspect`.

## Regras do Blender 3.6
- Principled BSDF 3.6 usa `Base Color`, `Metallic`, `Roughness`, `IOR`,
  `Emission`, `Emission Strength` (nomes antigos, não os de 4.x).
- EEVEE e Cycles têm suportes distintos (ex.: transmissão em EEVEE exige
  configurações adicionais de refração).
- Sockets de `nodes_connect`/`nodes_set_input` podem ser nome ou índice;
  nome inexistente gera erro `NotFound`/`KeyError` estruturado.
- `GeometryNodeTree` exige node group já existente — a ferramenta não cria
  grupos novos implicitamente.
- Imagens não empacotadas fazem o .blend depender de arquivos externos;
  `project_save` com `pack: true` empacota os recursos.

## Erros comuns
- Conectar sockets sem verificar tipos (Value→Color exige conversão).
- Usar nomes de inputs do Blender 4.x em 3.6 (ex.: mudanças do Principled).
- Assumir que `texture_load` aceita qualquer caminho: fora da allowlist o
  servidor responde `PathNotAllowed`.
