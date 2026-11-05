import pytest

from src.bot.card import CallbackError, decode, encode_card, encode_clarification, encode_retry, encode_site
from src.bot.card.codec import DRAFT_ACTIONS


def test_card_roundtrip():
    action = decode(encode_card("ab12cd34", 7, "st", -10))
    assert (action.name, action.arg, action.draft_id, action.version) == ("st", "-10", "ab12cd34", 7)


def test_pair_argument_roundtrip():
    assert decode(encode_card("ab12cd34", 1, "cand", "12:345")).arg == "12:345"


def test_draftless_prefixes():
    assert decode(encode_site(3)).arg == "3"
    q = decode(encode_clarification("0123456789ab", 2))
    assert (q.name, q.arg, q.turn) == ("clar", "2", "0123456789ab")
    assert decode(encode_retry("engine")).name == "retry"
    assert decode(encode_retry("sites")).name == "sites"


@pytest.mark.parametrize(
    "data",
    [
        "",
        "x1:1",
        "c1:ab:1:nope",
        "c1:ab:x:ok",
        "c1:AB!:1:ok",
        "c1:ab:1:st:1.5",
        "s1:abc",
        "q1:a:b",
        "r1:other",
        "c1:ab",
    ],
)
def test_malformed_callback_is_rejected(data):
    with pytest.raises(CallbackError):
        decode(data)


def test_too_long_is_rejected():
    with pytest.raises(CallbackError):
        encode_card("ab12cd34", 1, "cand", "9" * 80)
    with pytest.raises(CallbackError):
        decode("c1:" + "a" * 70)


def test_every_action_fits():
    for name in DRAFT_ACTIONS:
        assert len(encode_card("ab12cd34", 99999, name, "123456:123456").encode()) <= 64
