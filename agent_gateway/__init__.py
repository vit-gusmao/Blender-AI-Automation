"""Universal Agent Gateway — harness próprio independente de modelo (§7.2).

Conecta qualquer LLM (OpenAI-compatível, Anthropic, Gemini, Ollama, LM
Studio, endpoints custom) às ferramentas do Universal Blender MCP Server
via cliente MCP stdio. Ciclo OBSERVE→PLAN→ACT→VERIFY→REPAIR→REPORT (§9).

Uso: ``python -m agent_gateway``  (HTTP em 127.0.0.1:8767)
"""
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
_MCP_SRC = REPO_ROOT / "mcp_server" / "src"
if str(_MCP_SRC) not in sys.path:
    sys.path.insert(0, str(_MCP_SRC))

__version__ = "0.1.0"
