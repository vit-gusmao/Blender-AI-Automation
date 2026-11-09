"""Unit: auditoria JSONL + rotação de logs."""
from __future__ import annotations

import json

from universal_blender_mcp.audit_logger import AuditLogger


def test_audit_writes_jsonl(tmp_path):
    logger = AuditLogger(tmp_path, log_arguments=True)
    logger.audit("tool_call", tool="object_create",
                 arguments={"type": "CUBE"})
    logger.audit("tool_result", tool="object_create", success=True)
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert records[0]["event"] == "tool_call"
    assert records[0]["arguments"] == {"type": "CUBE"}
    assert records[1]["event"] == "tool_result"
    assert "ts" in records[0]


def test_audit_hides_arguments_when_disabled(tmp_path):
    logger = AuditLogger(tmp_path, log_arguments=False)
    logger.audit("tool_call", tool="python_execute_blender",
                 arguments={"code": "secret"})
    record = json.loads(
        (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert "arguments" not in record


def test_audit_instances_have_independent_handlers(tmp_path):
    """Duas instâncias com logs_dir distintos escrevem em seus próprios arquivos."""
    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    logger_a = AuditLogger(dir_a)
    logger_b = AuditLogger(dir_b)

    logger_a.audit("tool_call", tool="a_tool", arguments={"x": 1})
    logger_b.audit("tool_call", tool="b_tool", arguments={"y": 2})
    logger_a.info("mensagem da instância a")
    logger_b.info("mensagem da instância b")

    lines_a = (dir_a / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    lines_b = (dir_b / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines_a[0])["tool"] == "a_tool"
    assert json.loads(lines_b[0])["tool"] == "b_tool"

    log_a = (dir_a / "server.log").read_text(encoding="utf-8")
    log_b = (dir_b / "server.log").read_text(encoding="utf-8")
    assert "mensagem da instância a" in log_a
    assert "mensagem da instância b" in log_b
    assert "mensagem da instância b" not in log_a
    assert "mensagem da instância a" not in log_b


def test_audit_log_all_false_writes_no_records(tmp_path):
    logger = AuditLogger(tmp_path, log_all=False)
    logger.audit("tool_call", tool="object_create", arguments={"a": 1})
    assert not (tmp_path / "audit.jsonl").exists()
