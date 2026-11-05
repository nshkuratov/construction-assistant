"""Бэкенд Codex: ``codex exec`` с провайдером DeepSeek, в локальном для запуска домашнем каталоге и рабочей папке."""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from src.config import Config
from src.engine.base import BaseEngine, HarnessResult, TurnContext, run_process
from src.engine.prompt import build_prompt

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = Path(__file__).with_name("codex_config.template.toml")
PLAYBOOK = Path(__file__).with_name("workdir") / "AGENTS.md"
_WINDOWS_ENV = ("SYSTEMROOT", "USERPROFILE", "APPDATA", "PATHEXT", "TEMP", "TMP", "COMSPEC")


def _q(value: str | Path) -> str:
    """Базовая строка TOML. Строка JSON подходит (экранирование кавычек и обратных косых совпадает)."""
    return json.dumps(str(value), ensure_ascii=False)


def render_config(ctx: TurnContext, data_dir: Path) -> str:
    """``config.toml`` для конкретного запуска. Все пути, переданные MCP-серверу, абсолютные."""
    values = {
        "MODEL": _q(Config.CODEX_MODEL),
        "REASONING_EFFORT": _q(Config.CODEX_REASONING_EFFORT),
        "PYTHON": _q(sys.executable),
        "REPO_ROOT": _q(REPO_ROOT),
        "CHAT_DIR": _q(ctx.store.dir.resolve()),
        "DATA_DIR": _q(data_dir.resolve()),
        "FRICTION_LOG": _q(Path(Config.FRICTION_LOG).resolve()),
        "TELEMETRY_LOG": _q(Path(Config.RUN_TELEMETRY_LOG).resolve()),
        "TZ": _q(Config.CA_TZ),
        "HISTORY_TURNS": _q(Config.HISTORY_TURNS),
        "SNAPSHOT_MAX_AGE_S": _q(Config.SNAPSHOT_MAX_AGE_S),
        "ENABLE_TELEMETRY": _q("true" if Config.ENABLE_RUN_TELEMETRY else "false"),
        "MAX_CALLS": _q(Config.MCP_MAX_TOOL_CALLS_PER_RUN),
    }
    text = TEMPLATE.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace(f"__{key}__", value)
    return text


def harness_env(codex_home: Path) -> dict[str, str]:
    """Собирается с нуля: ни один секрет Telegram, Google или Bitrix не попадает в харнесс."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "CODEX_HOME": str(codex_home),
        "DEEPSEEK_API_KEY": Config.DEEPSEEK_API_KEY,
    }
    if sys.platform == "win32":
        env.update({k: os.environ[k] for k in _WINDOWS_ENV if k in os.environ})
    return env


def _codex_argv() -> list[str]:
    """Команда codex с найденным исполняемым файлом (npm-файл ``codex.cmd`` на Windows требует ``which``)."""
    head, *rest = Config.CODEX_COMMAND.split()
    return [shutil.which(head) or head, *rest]


def read_last_message(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


class CodexEngine(BaseEngine):
    backend = "codex"

    def run_harness(self, ctx: TurnContext) -> HarnessResult:
        data_dir = Path(self.data_dir if self.data_dir is not None else Config.CA_DATA_DIR)
        run_dir = Path(tempfile.mkdtemp(prefix="ca_codex_"))
        try:
            home, work = run_dir / "home", run_dir / "work"
            home.mkdir()
            work.mkdir()
            (home / "config.toml").write_text(render_config(ctx, data_dir), encoding="utf-8")
            shutil.copyfile(PLAYBOOK, work / "AGENTS.md")
            last = run_dir / "last_message.txt"
            cmd = [
                *_codex_argv(),
                "exec",
                "--skip-git-repo-check",
                "--ignore-rules",
                "--ephemeral",
                "-C",
                str(work),
                "-s",
                "read-only",
                "--json",
                "-o",
                str(last),
                build_prompt(ctx.text, ctx.turn_id),
            ]
            code, timed_out = run_process(
                cmd,
                env=harness_env(home),
                cwd=work,
                timeout_s=Config.ENGINE_TURN_TIMEOUT_S,
                stdout_path=run_dir / "events.jsonl",
                stderr_path=run_dir / "stderr.txt",
            )
            return HarnessResult(exit_code=code, final_message=read_last_message(last), timed_out=timed_out)
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)
