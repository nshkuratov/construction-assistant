"""Проверки уровня репозитория: нет жёстко зашитого каталога, сервер запускается."""

import subprocess
import sys
from pathlib import Path

from src.domain.sheet_layout import parse_grid

ROOT = Path(__file__).resolve().parent.parent
MIN_NAME_LEN = 12


def test_no_position_name_is_hard_coded_in_src(sample_grid):
    names = {p.name for p in parse_grid(sample_grid).positions if len(p.name) >= MIN_NAME_LEN}
    assert len(names) > 40  # поиск настоящий: есть много длинных названий для проверки
    hits = []
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        hits += [(path.name, n) for n in names if n in text]
    assert hits == []


def test_mcp_server_boots_and_lists_every_seeded_tool():
    code = (
        "import asyncio; from src.mcp.server import server; from src.mcp.registry import TOOL_PHASES;"
        "names={t.name for t in asyncio.run(server.list_tools())};"
        "assert names==set(TOOL_PHASES), names; print(len(names))"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "14"
