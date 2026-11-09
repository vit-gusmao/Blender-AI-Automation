"""Procura validate_client_request no pacote mcp_types inteiro."""
from pathlib import Path

root = Path(r"C:\Blender\RPA\.venv\Lib\site-packages\mcp_types")
for path in root.rglob("*.py"):
    text = path.read_text(encoding="utf-8", errors="replace")
    idx = text.find("def validate_client_request")
    if idx >= 0:
        print(f"== {path}")
        print(text[idx:idx + 2600])
        break
    if "validate_client_request" in text:
        print(f"(ref em {path})")
