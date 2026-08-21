import sys
import types
from enum import Flag, auto

astrbot_module = types.ModuleType("astrbot")
api_module = types.ModuleType("astrbot.api")
event_module = types.ModuleType("astrbot.api.event")
message_components_module = types.ModuleType("astrbot.api.message_components")
star_module = types.ModuleType("astrbot.api.star")


class FakeLogger:
    def warning(self, _message: str) -> None:
        pass


class _EventMessageType(Flag):
    GROUP_MESSAGE = auto()
    OTHER_MESSAGE = auto()


class FakeFilter:
    class EventMessageType:
        GROUP_MESSAGE = _EventMessageType.GROUP_MESSAGE
        OTHER_MESSAGE = _EventMessageType.OTHER_MESSAGE

    @staticmethod
    def command(_name: str):
        return lambda function: function

    @staticmethod
    def event_message_type(_event_type, **_kwargs):
        return lambda function: function


class FakeStar:
    def __init__(self, context: object, config: dict):
        self.context = context
        self.config = config


class FakeAt:
    def __init__(self, qq: int | str):
        self.qq = qq


class FakePlain:
    def __init__(self, text: str):
        self.text = text


message_components_module.At = FakeAt
message_components_module.Plain = FakePlain
api_module.AstrBotConfig = dict
api_module.logger = FakeLogger()
event_module.AstrMessageEvent = object
event_module.filter = FakeFilter()
star_module.Context = object
star_module.Star = FakeStar
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules.setdefault("astrbot.api", api_module)
sys.modules.setdefault("astrbot.api.event", event_module)
sys.modules.setdefault("astrbot.api.message_components", message_components_module)
sys.modules.setdefault("astrbot.api.star", star_module)

from astrbot_plugin_sub2api_status.claim import (  # noqa: E402
    is_bot_mentioned,
    is_claim_request,
    parse_account_identifier,
    parse_binding_confirmation_code,
    parse_group_ids,
)
from astrbot_plugin_sub2api_status.claim_store import ClaimStore  # noqa: E402


def test_claim_parser_requires_bot_mention_and_one_identifier() -> None:
    components = [FakeAt("bot-1"), FakePlain("领取 user@example.com")]

    assert is_bot_mentioned(components, "bot-1")
    assert not is_bot_mentioned(components, "other-bot")
    assert parse_account_identifier(components) == "user@example.com"
    assert parse_account_identifier([FakePlain("user@example.com other")]) is None


def test_claim_parser_accepts_username_and_group_allowlist() -> None:
    assert parse_account_identifier([FakePlain("赠送 alice_01")]) == "alice_01"
    assert parse_group_ids("123, 456，789") == {"123", "456", "789"}


def test_claim_request_does_not_accept_an_arbitrary_account_identifier() -> None:
    assert is_claim_request([FakeAt("bot-1"), FakePlain("领取")])
    assert is_claim_request([FakeAt("bot-1")], "")
    assert not is_claim_request([FakeAt("bot-1"), FakePlain("user@example.com")])
    assert not is_claim_request([FakeAt("bot-1"), FakePlain("领取 user@example.com")])


def test_binding_confirmation_parser_requires_six_digits() -> None:
    assert parse_binding_confirmation_code("/绑定确认 012345") == "012345"
    assert parse_binding_confirmation_code("bind-confirm 123456") == "123456"
    assert parse_binding_confirmation_code("/绑定确认 12345") is None


def test_claim_store_prevents_reuse_by_qq_or_sub2api_user(tmp_path) -> None:
    store = ClaimStore(tmp_path / "claims.sqlite3")
    first = store.reserve(
        qq_user_id="qq-1",
        sub2api_user_id=10,
        amount=10,
        idempotency_key="gift-1",
        created_at="now",
    )
    assert first.can_attempt

    store.mark_credited(first.record.id, "done")
    same_qq = store.reserve(
        qq_user_id="qq-1",
        sub2api_user_id=11,
        amount=10,
        idempotency_key="gift-2",
        created_at="later",
    )
    same_account = store.reserve(
        qq_user_id="qq-2",
        sub2api_user_id=10,
        amount=10,
        idempotency_key="gift-3",
        created_at="later",
    )

    assert not same_qq.can_attempt
    assert not same_account.can_attempt


def test_failed_claim_reuses_same_idempotency_key(tmp_path) -> None:
    store = ClaimStore(tmp_path / "claims.sqlite3")
    first = store.reserve(
        qq_user_id="qq-1",
        sub2api_user_id=10,
        amount=10,
        idempotency_key="gift-stable",
        created_at="now",
    )
    store.mark_failed(
        first.record.id,
        error_code="timeout",
        error_message="upstream timeout",
    )

    retry = store.reserve(
        qq_user_id="qq-1",
        sub2api_user_id=10,
        amount=99,
        idempotency_key="different-key-is-ignored",
        created_at="later",
    )
    assert retry.can_attempt
    assert retry.record.idempotency_key == "gift-stable"
    assert retry.record.amount == 10
