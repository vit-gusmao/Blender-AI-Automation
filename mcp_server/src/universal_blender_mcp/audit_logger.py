"""Auditoria e logging do servidor (storage/logs/audit.jsonl + server.log)."""
from __future__ import annotations

import json
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


class AuditLogger:
    """Auditoria JSONL + log rotativo com estado POR INSTÂNCIA.

    Cada instância possui logger e handler próprios: duas instâncias com
    ``logs_dir`` diferentes nunca compartilham server.log (BUG-024/025).
    ``log_all=False`` desliga a escrita de registros de auditoria;
    ``log_arguments=False`` oculta o campo ``arguments`` dos registros.
    """

    _instance_counter = 0

    def __init__(self, logs_dir: Path, level: str = "INFO",
                 log_arguments: bool = True, log_all: bool = True):
        AuditLogger._instance_counter += 1
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.audit_path = self.logs_dir / "audit.jsonl"
        self.log_arguments = log_arguments
        self.log_all = log_all
        self._logger = logging.getLogger(
            f"universal_blender_mcp.audit_{AuditLogger._instance_counter}")
        self._logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        self._logger.propagate = False
        handler = RotatingFileHandler(
            self.logs_dir / "server.log", maxBytes=2_000_000, backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(_FORMAT))
        self._logger.addHandler(handler)

    def audit(self, event: str, **fields: Any) -> None:
        if not self.log_all:
            return
        record = {"ts": time.time(), "event": event}
        record.update(fields)
        if not self.log_arguments:
            record.pop("arguments", None)
        try:
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            self._logger.exception("falha ao gravar auditoria")
        self._logger.info("audit %s %s", event, record.get("tool") or record.get("operation") or "")

    def info(self, msg: str, *args: Any) -> None:
        self._logger.info(msg, *args)

    def error(self, msg: str, *args: Any) -> None:
        self._logger.error(msg, *args)
