# Skill — Animação (Blender 3.6)

## Quando usar
Keyframes, actions, FCurves e NLA.

## Ferramentas reais desta implementação
- `animation_keyframe`: `object`, `data_path` (ex.: `location`,
  `rotation_euler`, `scale`), `frame` (padrão = frame atual), `index`
  (padrão `-1` = todos os canais), `value` opcional (aplica o valor antes de
  inserir o keyframe; hoje aplicado a `location`/`scale`). Cria
  `animation_data` e devolve o nome da action.
- `animation_action`: cria (`create`), atribui a um objeto (`assign_to`),
  marca `set_fake_user` ou remove (`delete`) uma action.
- `animation_fcurve`: `operation = LIST | SET_KEYFRAME | SET_INTERPOLATION |
  DELETE_KEYFRAME` sobre `action`, com `data_path`, `index`, `frame`,
  `value`, `interpolation` (`BEZIER | LINEAR | CONSTANT`).
- `animation_nla`: `operation = LIST | ADD_STRIP` (action → track, com
  `start`).

## Fluxo recomendado
1. `animation_keyframe` por frame para a animação base.
2. `animation_action` para nomear, preservar (`use_fake_user`) e reatribuir.
3. `animation_fcurve LIST` para inspecionar; depois ajuste interpolação ou
   keyframes pontuais.
4. `animation_nla` para montar strips em tracks.
5. Configure o intervalo com `render_configure` (`frame_start`, `frame_end`)
   quando o objetivo for vídeo; renderize com `render_final`
   (`animation: true`).

## Regras do Blender 3.6
- Keyframes usam `obj.keyframe_insert(data_path, frame=..., index=...)` —
  sem depender de contexto de UI.
- FCurves são localizadas por `action.fcurves.find(data_path, index=...)`;
  curva inexistente gera erro estruturado (exceto em `SET_KEYFRAME`, que a
  cria).
- Actions sem usuários são perdidas ao salvar sem `use_fake_user`
  (`animation_action` marca fake user por padrão na criação).
- `scene_validate` com `checks: ["orphan_actions"]` detecta actions órfãs;
  `fix: true` as remove — use com critério.

## Erros comuns
- Animar `rotation_euler` com o objeto em modo `QUATERNION` não produz o
  resultado esperado; ajuste `rotation_mode` com `object_transform`.
- `animation_fcurve`, `animation_nla` e o render de animação **não são
  exercitados pelo smoke test** — confira `animation_fcurve LIST` e a NLA
  após cada alteração.
