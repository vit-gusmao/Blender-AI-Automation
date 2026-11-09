"""Localiza 'Invalid request parameters' no SDK e mostra contexto."""
import pathlib

root = pathlib.Path(r"C:\Blender\RPA\.venv\Lib\site-packages\mcp")
for path in root.rglob("*.py"):
    text = path.read_text(encoding="utf-8", errors="replace")
    if "Invalid request parameters" in text:
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if "Invalid request parameters" in line:
                print(f"== {path}:{i + 1}")
                print("\n".join(lines[max(0, i - 25):i + 5]))
                print()
