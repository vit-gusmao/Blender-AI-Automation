# Skill — Renderização (Blender 3.6)

## Quando usar
Configuração de engine, câmera, luz, saída, prévias, render final e
verificação do arquivo produzido.

## Ferramentas reais desta implementação
- `camera_configure`: câmera por nome ou a ativa; `focal_length`, `type`
  (`PERSP | ORTHO | PANO`), `clip_start/end`, `dof_enable`,
  `focus_distance`, `aperture_fstop`. Torna a câmera ativa da cena.
  Posicionamento/rotação são feitos com `object_transform`.
- `light_create`: `POINT | SUN | SPOT | AREA`, com `location`, `rotation`,
  `energy`, `color`; nome repetido é reutilizado.
- `light_configure`: `energy`, `color`, `size` (`shadow_soft_size`),
  `spot_size`, `spot_blend`, `use_shadow`.
- `render_configure`: `engine = CYCLES | BLENDER_EEVEE`, resolução e
  `resolution_percentage`, `samples` (Cycles: `scene.cycles.samples`; EEVEE:
  `taa_render_samples`), `use_gpu` (Cycles), `filepath`, `file_format`
  (`PNG`, `JPEG`, `OPEN_EXR`, `BMP`, `TIFF`, `WEBP`, `FFMPEG`),
  `frame_start/end`, `film_transparent`, `use_stamp`.
- `render_preview`: `percentage` (padrão 33) e `filepath` opcional.
- `render_final`: `filepath` e `animation: true` para sequência.
- `render_inspect` *(servidor)*: confirma existência, tamanho, formato e
  dimensões (PNG/JPEG) do arquivo — o caminho passa pela allowlist;
  relativo é resolvido em `storage/renders/`.

## Fluxo recomendado
1. `scene_get_info` para confirmar a câmera; `camera_configure` +
   `object_transform` para enquadrar.
2. `light_create` + `light_configure`.
3. `render_configure` com engine, resolução, samples, `filepath` e formato.
4. `render_preview` para iteração rápida.
5. `render_final`; depois `job_get_status` (se assíncrono) e
   **`render_inspect` antes de declarar sucesso**.

## Comportamento assíncrono real
- **Background (`--background`)**: render síncrono; o envelope final chega
  quando o arquivo foi escrito. Verificado no smoke com Cycles 120×90.
- **Interface gráfica**: render não bloqueante (`INVOKE_DEFAULT`); o comando
  fica `running` e é concluído pelos handlers `render_complete` /
  `render_cancel`. `render_cancel` produz falha estruturada
  (`RenderCancelled`).
- Se não houver câmera ativa, a ferramenta usa a primeira câmera da cena;
  sem nenhuma câmera, falha com mensagem explícita.
- `filepath` relativo é salvo em `storage/renders/`; caminhos absolutos
  passam pela allowlist (`PathNotAllowed` fora dela).
- EEVEE em background/headless pode falhar por falta de contexto gráfico —
  para automação em `--background`, prefira **CYCLES**.

## Erros comuns
- Declarar render correto sem `render_inspect` (ou sem inspeção visual) —
  proibido pelo System Prompt.
- `file_format` incompatível com a extensão de `filepath`.
- Esquecer `resolution_percentage` baixo de um preview anterior: configure
  explicitamente antes do render final.
