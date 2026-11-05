"""Токен бота не должен попасть в строку лога (httpx пишет URL запросов, а в URL Telegram есть токен)."""

import logging

import httpx
import pytest

from src.bot.__main__ import configure_logging

TOKEN = "123456789:AAFakeTokenForTheLogTest_abcdefghij"


@pytest.fixture
def restore_logging():
    saved = {name: logging.getLogger(name).level for name in ("", "httpx", "httpcore")}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


def _transport(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


def test_telegram_request_does_not_log_the_token(caplog, restore_logging):
    configure_logging("INFO")
    with caplog.at_level(logging.DEBUG):
        logging.getLogger().setLevel(logging.INFO)
        with httpx.Client(transport=httpx.MockTransport(_transport)) as client:
            client.post(f"https://api.telegram.org/bot{TOKEN}/getUpdates")
    assert TOKEN not in caplog.text


def test_app_logs_still_pass_at_info(caplog, restore_logging):
    configure_logging("INFO")
    with caplog.at_level(logging.INFO):
        logging.getLogger("src.bot.app").info("started")
    assert "started" in caplog.text


def test_token_in_a_message_is_masked(caplog, restore_logging):
    configure_logging("INFO")
    with caplog.at_level(logging.INFO):
        logging.getLogger("src.bot.app").warning("call failed: https://api.telegram.org/bot%s/x", TOKEN)
    assert TOKEN not in caplog.text
    assert "bot***" in caplog.text
