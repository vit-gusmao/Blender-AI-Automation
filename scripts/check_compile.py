"""Verificação rápida: py_compile em todos os .py do projeto."""
import pathlib
import py_compile
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if not ROOT.is_dir():
    raise SystemExit(f"project root is not a directory: {ROOT}")
SKIP = {".superpowers", ".venv", "dist", "storage", "__pycache__"}

bad, n = [], 0
for p in sorted(ROOT.rglob("*.py")):
    if any(part in SKIP for part in p.parts):
        continue
    n += 1
    try:
        py_compile.compile(str(p), doraise=True)
    except Exception as exc:  # noqa: BLE001
        bad.append(f"{p}: {exc}")

print(f"checked={n} failures={len(bad)}")
for line in bad:
    print("FAIL", line)
sys.exit(1 if bad else 0)
