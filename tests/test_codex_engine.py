"""Рендер конфига CodexEngine, окружение движка, флаги и фабрика."""

import os
import sys
import tomllib
from pathlib import Path

import pytest

import src.engine.codex_engine as codex_engine
from src.config import Config
from src.engine import make_engine
from src.engine.base import TurnContext
from src.engine.codex_engine import CodexEngine, harness_env, render_config
from src.engine.fake_engine import FakeEngine


@pytest.fixture
def ctx(chat):
    return TurnContext(chat, "turn1", "текст")


def test_rendered_config_is_valid_toml_with_deepseek_and_absolute_mcp_env(ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "FRICTION_LOG", "logs/friction.jsonl")
    cfg = tomllib.loads(render_config(ctx, Path("data")))
    provider = cfg["model_providers"]["deepseek"]
    assert cfg["model_provider"] == "deepseek"
    assert provider["base_url"] == "https://api.deepseek.com/v1"
    assert provider["env_key"] == "DEEPSEEK_API_KEY"
    assert "wire_api" not in provider
    assert cfg["model"] == Config.CODEX_MODEL
    server = cfg["mcp_servers"]["construction-assistant"]
    assert server["command"] == sys.executable
    assert server["args"] == ["-m", "src.mcp"]
    env = server["env"]
    for key in ("CA_CHAT_DIR", "CA_DATA_DIR", "FRICTION_LOG", "RUN_TELEMETRY_LOG", "PYTHONPATH"):
        assert Path(env[key]).is_absolute(), key
    assert env["CA_CHAT_DIR"] == str(ctx.store.dir.resolve())
    assert env["CA_SKIP_DOTENV"] == "1"
    assert (Path(env["PYTHONPATH"]) / "src" / "mcp").is_dir()
    assert env["CA_TZ"] == Config.CA_TZ
    assert server["default_tools_approval_mode"] == "approve"


def test_rendered_config_is_least_privilege(ctx):
    cfg = tomllib.loads(render_config(ctx, Path("data")))
    assert cfg["sandbox_mode"] == "read-only"
    assert cfg["approval_policy"] == "never"
    assert cfg["web_search"] == "disabled"
    assert cfg["features"]["shell_tool"] is False
    assert cfg["features"]["multi_agent"] is False


def test_harness_env_carries_only_the_api_key_and_basics(monkeypatch, tmp_path):
    for name in ("TELEGRAM_BOT_TOKEN", "GOOGLE_SERVICE_ACCOUNT_FILE", "BITRIX_WEBHOOK_URL"):
        monkeypatch.setenv(name, "secret")
    monkeypatch.setattr(Config, "DEEPSEEK_API_KEY", "k")
    env = harness_env(tmp_path)
    expected_keys = {"CODEX_HOME", "DEEPSEEK_API_KEY", "HOME", "PATH"}
    if sys.platform == "win32":
        # Windows needs these OS variables to start subprocesses; app secrets stay excluded.
        windows_keys = {"SYSTEMROOT", "USERPROFILE", "APPDATA", "PATHEXT", "TEMP", "TMP", "COMSPEC"}
        expected_keys.update(windows_keys & os.environ.keys())
    assert set(env) == expected_keys
    assert env["DEEPSEEK_API_KEY"] == "k"
    assert env["CODEX_HOME"] == str(tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="fake codex is a POSIX script")
async def test_codex_receives_the_expected_flags_and_a_clean_env(chat, tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return 0, False

    monkeypatch.setattr(codex_engine, "run_process", fake_run)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret")
    monkeypatch.setattr(Config, "DEEPSEEK_API_KEY", "k")
    await CodexEngine(data_dir=str(chat.dir.parent.parent), log_path=str(tmp_path / "e.jsonl")).handle(42, "привет")
    cmd = seen["cmd"]
    assert cmd[1:3] == ["exec", "--skip-git-repo-check"]
    assert cmd[cmd.index("-s") + 1] == "read-only"
    assert "--json" in cmd and "-o" in cmd
    assert "<<<FOREMAN_MESSAGE\nпривет\nFOREMAN_MESSAGE>>>" in cmd[-1]
    assert "TELEGRAM_BOT_TOKEN" not in seen["env"]
    assert not Path(seen["cwd"]).exists(), "the run dir must be removed after the run"


def test_make_engine_default_is_codex():
    assert isinstance(make_engine(), CodexEngine)


def test_make_engine_unknown_backend_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(Config, "ENGINE_BACKEND", "gpt")
    with pytest.raises(ValueError, match="Unknown ENGINE_BACKEND"):
        make_engine()




def test_fake_engine_is_an_engine():
    assert hasattr(FakeEngine(), "handle")
