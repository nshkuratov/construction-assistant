"""Поддельный исполняемый файл ``codex`` для тестов. Поведение задаёт ``mode`` рядом со скриптом.

Он пишет в ``record.json``, что ему дал движок (argv, ключи env, cwd, конфиг). Он читает готовый
``$CODEX_HOME/config.toml`` и запускает инструменты MCP через ``src.mcp.cli`` с окружением из
конфига, поэтому неверный конфиг роняет тест.
"""

import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

here = Path(__file__).resolve().parent
mode = (here / "mode").read_text().strip()
home = Path(os.environ["CODEX_HOME"])
cfg = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
server = cfg["mcp_servers"]["construction-assistant"]
(here / "record.json").write_text(
    json.dumps(
        {
            "argv": sys.argv[1:],
            "env_keys": sorted(os.environ),
            "cwd": os.getcwd(),
            "agents_md": Path("AGENTS.md").is_file(),
            "config": cfg,
        }
    )
)


def tool(name, **args):
    env = {"PATH": os.environ["PATH"], **server["env"]}
    out = subprocess.run(
        [server["command"], "-m", "src.mcp.cli", name, "--json", json.dumps(args)],
        env=env,
        cwd=server["cwd"],
        capture_output=True,
        text=True,
        check=False,
    )
    return json.loads(out.stdout)


def last_message(text):
    args = sys.argv
    Path(args[args.index("-o") + 1]).write_text(text, encoding="utf-8")


if mode == "normal":
    tool("get_session")
    assert tool("finish_turn", outcome="answer", message="Готово.")["ok"]
    last_message("done")
elif mode == "no_finish":
    tool("select_site", site="Школа")
    last_message("Просто текст без вызова finish_turn")
elif mode == "finish_then_crash":
    tool("finish_turn", outcome="answer", message="Ответ.")
    sys.exit(3)
elif mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    (here / "child.pid").write_text(str(child.pid))
    tool("select_site", site="Школа")
    time.sleep(600)
elif mode == "crash":
    (here / "runs").open("a").write("x")
    tool("select_site", site="Школа")
    sys.exit(2)
elif mode == "crash_once":
    runs = here / "runs"
    first = not runs.exists()
    runs.open("a").write("x")
    if first:
        tool("select_site", site="Школа")
        sys.exit(2)
    tool("finish_turn", outcome="answer", message="Со второго раза.")
    last_message("ok")
