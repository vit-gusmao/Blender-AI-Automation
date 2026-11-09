"""Unit: settings/TOML/YAML (parsers stdlib com fallback)."""
from __future__ import annotations

import sys
from pathlib import Path

from universal_blender_mcp.settings import (_mini_toml, _mini_yaml,
                                            find_project_root, load_settings,
                                            load_toml, load_yaml)


def test_mini_toml_parses_app_config():
    text = Path(__file__).parents[2].joinpath("config", "app.toml").read_text(
        encoding="utf-8")
    data = _mini_toml(text)
    assert data["bridge"]["host"] == "127.0.0.1"
    assert data["bridge"]["port"] == 8765
    assert data["server"]["autostart_bridge"] is True
    assert data["server"]["transport"] == "stdio"
    assert data["external_runtime_limits"]["timeout_s"] == 300
    assert "powershell" in data["external_runtimes"]


def test_mini_yaml_parses_providers():
    text = Path(__file__).parents[2].joinpath("config", "providers.yaml").read_text(
        encoding="utf-8")
    data = _mini_yaml(text)
    providers = data["providers"]
    assert providers["local_ollama"]["adapter"] == "ollama"
    assert providers["local_ollama"]["base_url"] == "http://127.0.0.1:11434"
    assert providers["cloud_anthropic"]["api_key_env"] == "ANTHROPIC_API_KEY"
    assert data["roles"]["reviewer"] == "cloud_openai"


def test_mini_yaml_handles_comments_and_lists():
    data = _mini_yaml("# comentário\na:\n  - x\n  - y\nb: 2\n")
    assert data == {"a": ["x", "y"], "b": 2}


def test_load_settings_from_tmp_root(tmp_root):
    settings = load_settings(tmp_root)
    assert settings.bridge_port == 8765
    assert settings.storage_root == tmp_root / "storage"
    assert settings.storage_root.exists()
    assert settings.providers_cfg["providers"]["local_ollama"]["model"] == "qwen3"
    assert settings.policies_cfg["authorization"]["default_level"] == "scene_write"


def test_bridge_token_generated_once(settings):
    token1 = settings.bridge_token()
    assert len(token1) >= 32
    token2 = settings.bridge_token()
    assert token1 == token2  # persistido, não regenerado


def test_find_project_root_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BLENDER_MCP_ROOT", str(tmp_path))
    assert find_project_root() == tmp_path.resolve()


def test_mini_toml_accepts_apostrophes_inside_double_quotes():
    data = _mini_toml('name = "O\'Brien\'s blend"\nnum = 2\n')
    assert data["name"] == "O'Brien's blend"
    assert data["num"] == 2


def test_bridge_token_concurrent_creation_is_atomic(settings, tmp_root):
    """Corrida em bridge_token() gera exatamente um token compartilhado."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    settings.token_file.parent.mkdir(parents=True, exist_ok=True)
    barrier = threading.Barrier(8)

    def call(_index):
        barrier.wait(timeout=5)
        return settings.bridge_token()

    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(call, range(8)))

    assert len(set(tokens)) == 1, f"tokens divergentes: {len(set(tokens))}"
    assert settings.token_file.read_text(encoding="utf-8").strip() == tokens[0]


def test_bridge_token_adopts_file_content_after_race(settings, tmp_root,
                                                     monkeypatch):
    """F3: o vencedor da escrita adota o conteúdo do arquivo se divergiu."""
    settings.token_file.parent.mkdir(parents=True, exist_ok=True)
    calls = {"n": 0}

    def scripted_read():
        calls["n"] += 1
        if calls["n"] == 1:
            return ""  # entrada: arquivo ausente
        return "token-de-outro-escritor"  # pós-escrita: outro escritor venceu

    monkeypatch.setattr(settings, "_read_token", scripted_read)
    token = settings.bridge_token()
    assert token == "token-de-outro-escritor"


def test_http_auth_token_self_heals_empty_remnant(settings, tmp_root):
    """F2: resquício vazio de http_token não pode bloquear a subida."""
    settings.http_token_file.parent.mkdir(parents=True, exist_ok=True)
    settings.http_token_file.write_text("", encoding="utf-8")
    token = settings.http_auth_token(generate=True)
    assert token, "http_auth_token retornou vazio com arquivo vazio remanescente"
    assert settings.http_token_file.read_text(encoding="utf-8").strip() == token


def test_server_consumes_polling_override(settings):
    from universal_blender_mcp.server import create_server

    settings.autostart_bridge = False
    settings.poll_interval_s = 0.73
    server = create_server(settings)
    try:
        assert server.blender_ctx.bridge.poll_interval_s == 0.73
    finally:
        server.blender_ctx.jobs.close()


def test_server_applies_audit_policy(tmp_root):
    from universal_blender_mcp.server import create_server
    from universal_blender_mcp.settings import load_settings

    policies = tmp_root / "config" / "policies.yaml"
    text = policies.read_text(encoding="utf-8")
    text = text.replace("log_all: true", "log_all: false")
    text = text.replace("log_arguments: true", "log_arguments: false")
    policies.write_text(text, encoding="utf-8")

    settings = load_settings(tmp_root)
    settings.autostart_bridge = False
    server = create_server(settings)
    try:
        assert server.blender_ctx.audit.log_all is False
        assert server.blender_ctx.audit.log_arguments is False
    finally:
        server.blender_ctx.jobs.close()


def test_inspect_creates_no_storage_token_or_logs(tmp_path):
    import json
    import os
    import subprocess

    repo = Path(__file__).parents[2]
    env = {**os.environ, "BLENDER_MCP_ROOT": str(tmp_path),
           "PYTHONPATH": str(repo / "mcp_server" / "src")}
    proc = subprocess.run(
        [sys.executable, "-m", "universal_blender_mcp", "inspect"],
        capture_output=True, text=True, env=env, cwd=str(repo), timeout=120)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["count"] >= 55
    assert "session_level" in payload
    created = sorted(p.name for p in tmp_path.iterdir())
    assert "storage" not in created, f"inspect criou: {created}"
    assert not (tmp_path / "storage" / "bridge_token").exists()


def test_prompts_fall_back_to_packaged_resources(tmp_path):
    from universal_blender_mcp.prompts import get_prompt

    messages = get_prompt("blender_task", {"objective": "um cubo"}, tmp_path)
    content = messages[0]["content"]
    assert "não encontrado" not in content
    assert "um cubo" in content


def test_packaged_prompts_match_repository_prompts():
    from importlib import resources

    repo = Path(__file__).parents[2]
    pkg = resources.files("universal_blender_mcp") / "prompts"
    for rel in ("task_prompt.template.md", "skills/modeling.md",
                "skills/materials.md", "skills/rigging.md",
                "skills/animation.md", "skills/rendering.md"):
        packaged = pkg.joinpath(*rel.split("/")).read_text(encoding="utf-8")
        assert packaged == (repo / "prompts" / rel).read_text(encoding="utf-8")
