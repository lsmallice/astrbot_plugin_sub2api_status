import sys
import types
from datetime import datetime, timedelta, timezone
from enum import Flag, auto
from types import SimpleNamespace
from unittest.mock import AsyncMock


class FakeLogger:
    def warning(self, _message: str) -> None:
        pass


class FakeStar:
    def __init__(self, context: object, config: dict[str, str]) -> None:
        self.context = context
        self.config = config


class _EventMessageType(Flag):
    GROUP_MESSAGE = auto()
    OTHER_MESSAGE = auto()


class FakeFilter:
    @staticmethod
    def command(_name: str):
        return lambda function: function

    class EventMessageType:
        GROUP_MESSAGE = _EventMessageType.GROUP_MESSAGE
        OTHER_MESSAGE = _EventMessageType.OTHER_MESSAGE

    @staticmethod
    def event_message_type(_event_type, **_kwargs):
        return lambda function: function


astrbot_module = types.ModuleType("astrbot")
api_module = types.ModuleType("astrbot.api")
event_module = types.ModuleType("astrbot.api.event")
star_module = types.ModuleType("astrbot.api.star")
api_module.AstrBotConfig = dict
api_module.logger = FakeLogger()
event_module.AstrMessageEvent = object
event_module.filter = FakeFilter()
message_components_module = types.ModuleType("astrbot.api.message_components")


class FakeAt:
    def __init__(self, qq: int | str):
        self.qq = qq


class FakePlain:
    def __init__(self, text: str):
        self.text = text


class FakeMessageChain:
    def __init__(self) -> None:
        self.chain: list[FakePlain] = []

    def message(self, text: str) -> "FakeMessageChain":
        self.chain.append(FakePlain(text))
        return self


setattr(sys.modules["astrbot.api.event"], "MessageChain", FakeMessageChain)
message_components_module.At = FakeAt
message_components_module.Plain = FakePlain
star_module.Context = object
star_module.Star = FakeStar
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules.setdefault("astrbot.api", api_module)
sys.modules.setdefault("astrbot.api.event", event_module)
sys.modules.setdefault("astrbot.api.message_components", message_components_module)
sys.modules.setdefault("astrbot.api.star", star_module)

from astrbot_plugin_sub2api_status.claim import At as ClaimAt  # noqa: E402
from astrbot_plugin_sub2api_status.claim import Plain as ClaimPlain  # noqa: E402
from astrbot_plugin_sub2api_status.client import ChannelSnapshot  # noqa: E402
from astrbot_plugin_sub2api_status.main import Main  # noqa: E402


class FakeEvent:
    def image_result(self, path: str) -> tuple[str, str]:
        return "image", path

    def plain_result(self, text: str) -> tuple[str, str]:
        return "text", text

    def chain_result(self, chain: list[object]) -> tuple[str, list[object]]:
        return "chain", chain


class BindingEvent(FakeEvent):
    def __init__(self, sender_id: str = "qq-openid") -> None:
        self._sender_id = sender_id

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_platform_id(self) -> str:
        return "qq_official"


class ClaimEvent(FakeEvent):
    def __init__(self, sender_id: str = "qq-1") -> None:
        self.stopped = False
        self._sender_id = sender_id

    def get_group_id(self) -> str:
        return "group-1"

    def get_self_id(self) -> str:
        return "bot-1"

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_messages(self) -> list[object]:
        return [ClaimAt(qq="bot-1"), ClaimPlain(text="领取")]

    def get_message_str(self) -> str:
        return "领取"

    def stop_event(self) -> None:
        self.stopped = True


class WelcomeEvent(FakeEvent):
    def __init__(self, *, group_id: str = "group-1", user_id: str = "qq-new") -> None:
        self.message_obj = SimpleNamespace(
            raw_message={
                "post_type": "notice",
                "notice_type": "group_increase",
                "sub_type": "approve",
                "group_id": group_id,
                "user_id": user_id,
            }
        )
        self._self_id = "bot-1"

    def get_group_id(self) -> str:
        return str(self.message_obj.raw_message["group_id"])

    def get_self_id(self) -> str:
        return self._self_id


def make_snapshots(count: int) -> tuple[ChannelSnapshot, ...]:
    return tuple(
        ChannelSnapshot(
            {
                "id": index,
                "name": f"Channel {index}",
                "provider": "openai",
                "primary_model": f"model-{index}",
                "primary_status": "operational",
                "primary_latency_ms": index,
                "availability_7d": 99,
            },
            (),
        )
        for index in range(1, count + 1)
    )


async def collect_results(plugin: Main) -> list[tuple[str, str]]:
    return [result async for result in plugin.status(FakeEvent())]


async def test_status_sends_every_image_page(monkeypatch) -> None:
    plugin = Main(object(), {})
    snapshots = make_snapshots(7)
    monkeypatch.setattr(plugin, "_snapshots", AsyncMock(return_value=snapshots))
    plugin.html_render = AsyncMock(side_effect=["page-1.png", "page-2.png"])

    results = await collect_results(plugin)

    assert results == [
        ("image", "page-1.png"),
        ("image", "page-2.png"),
    ]
    assert plugin.html_render.await_count == 2


async def test_render_failure_sends_only_complete_text_fallback(monkeypatch) -> None:
    plugin = Main(object(), {})
    snapshots = make_snapshots(9)
    monkeypatch.setattr(plugin, "_snapshots", AsyncMock(return_value=snapshots))
    plugin.html_render = AsyncMock(
        side_effect=["page-1.png", RuntimeError("renderer unavailable")]
    )

    results = await collect_results(plugin)

    assert [kind for kind, _value in results] == ["text", "text"]
    assert "第 1/2 页" in results[0][1]
    assert "Channel 1" in results[0][1]
    assert "Channel 8" in results[0][1]
    assert "第 2/2 页" in results[1][1]
    assert "Channel 9" in results[1][1]


async def test_claim_credits_once_and_stops_group_event(tmp_path, monkeypatch) -> None:
    config = {
        "base_url": "https://example.com",
        "SUB2API_ADMIN_KEY": "admin-test-key",
        "gift_enabled": True,
        "gift_amount": 10,
        "gift_allowed_group_ids": "group-1",
        "binding_service_url": "http://binding.test",
        "QQ_BINDING_SERVICE_KEY": "binding-test-key",
        "claim_db_path": str(tmp_path / "claims.sqlite3"),
    }
    plugin = Main(object(), config)
    client = SimpleNamespace(
        add_balance=AsyncMock(),
    )
    binding_client = SimpleNamespace(
        lookup=AsyncMock(return_value=SimpleNamespace(id=42)),
    )
    monkeypatch.setattr(plugin, "_client", lambda: client)
    monkeypatch.setattr(plugin, "_binding_client", lambda: binding_client)

    first_event = ClaimEvent()
    first_results = [result async for result in plugin.claim_balance(first_event)]
    second_event = ClaimEvent()
    second_results = [result async for result in plugin.claim_balance(second_event)]

    assert first_results == [
        (
            "text",
            "领取成功，已为你的 Sub2API 账号赠送 10 余额。\n"
            "每个 QQ 用户和 Sub2API 账号仅可领取一次。",
        )
    ]
    assert "已经领取过" in second_results[0][1]
    assert first_event.stopped
    assert second_event.stopped
    client.add_balance.assert_awaited_once()


async def test_binding_link_is_sent_to_private_session(monkeypatch) -> None:
    config = {
        "binding_service_url": "http://binding.test",
        "QQ_BINDING_SERVICE_KEY": "binding-test-key",
    }
    send_message = AsyncMock()
    plugin = Main(SimpleNamespace(send_message=send_message), config)
    challenge = SimpleNamespace(
        binding_url="https://smallice.xyz/tools/qq-bind/secret-nonce",
        expires_at="2026-08-21T12:00:00Z",
    )
    binding_client = SimpleNamespace(create_challenge=AsyncMock(return_value=challenge))
    monkeypatch.setattr(plugin, "_binding_client", lambda: binding_client)

    results = [result async for result in plugin.create_qq_binding(BindingEvent())]

    assert "已私发" in results[0][1]
    send_message.assert_awaited_once()
    session, chain = send_message.await_args.args
    assert session == "qq_official:FriendMessage:qq-openid"
    assert "secret-nonce" in chain.chain[0].text
    assert "secret-nonce" not in results[0][1]


async def test_binding_link_is_never_leaked_when_private_delivery_fails(
    monkeypatch,
) -> None:
    config = {
        "binding_service_url": "http://binding.test",
        "QQ_BINDING_SERVICE_KEY": "binding-test-key",
    }
    send_message = AsyncMock(side_effect=RuntimeError("private unavailable"))
    plugin = Main(SimpleNamespace(send_message=send_message), config)
    challenge = SimpleNamespace(
        binding_url="https://smallice.xyz/tools/qq-bind/secret-nonce",
        expires_at="2026-08-21T12:00:00Z",
    )
    binding_client = SimpleNamespace(create_challenge=AsyncMock(return_value=challenge))
    monkeypatch.setattr(plugin, "_binding_client", lambda: binding_client)

    results = [result async for result in plugin.create_qq_binding(BindingEvent())]

    assert "未能私发" in results[0][1]
    assert "secret-nonce" not in results[0][1]


async def test_claim_requires_a_binding(monkeypatch, tmp_path) -> None:
    config = {
        "base_url": "https://example.com",
        "SUB2API_ADMIN_KEY": "admin-test-key",
        "gift_enabled": True,
        "gift_amount": 10,
        "gift_allowed_group_ids": "group-1",
        "binding_service_url": "http://binding.test",
        "QQ_BINDING_SERVICE_KEY": "binding-test-key",
        "claim_db_path": str(tmp_path / "claims.sqlite3"),
    }
    plugin = Main(object(), config)
    binding_client = SimpleNamespace(lookup=AsyncMock(return_value=None))
    monkeypatch.setattr(plugin, "_binding_client", lambda: binding_client)

    results = [result async for result in plugin.claim_balance(ClaimEvent())]

    assert "尚未绑定" in results[0][1]
    binding_client.lookup.assert_awaited_once_with("qq-1")


async def test_claim_rejects_account_outside_registration_window(
    tmp_path, monkeypatch
) -> None:
    config = {
        "base_url": "https://example.com",
        "SUB2API_ADMIN_KEY": "admin-test-key",
        "gift_enabled": True,
        "gift_amount": 10,
        "gift_allowed_group_ids": "group-1",
        "claim_registration_window_hours": 24,
        "binding_service_url": "http://binding.test",
        "QQ_BINDING_SERVICE_KEY": "binding-test-key",
        "claim_db_path": str(tmp_path / "claims.sqlite3"),
    }
    plugin = Main(object(), config)
    client = SimpleNamespace(
        add_balance=AsyncMock(),
        fetch_user_created_at=AsyncMock(
            return_value=datetime.now(timezone.utc) - timedelta(hours=25)
        ),
    )
    binding_client = SimpleNamespace(
        lookup=AsyncMock(return_value=SimpleNamespace(id=42)),
    )
    monkeypatch.setattr(plugin, "_client", lambda: client)
    monkeypatch.setattr(plugin, "_binding_client", lambda: binding_client)

    results = [result async for result in plugin.claim_balance(ClaimEvent())]

    assert "超过本次活动的领取时间限制" in results[0][1]
    client.add_balance.assert_not_awaited()


async def test_welcome_mentions_new_member_and_deduplicates() -> None:
    config = {
        "welcome_enabled": True,
        "welcome_group_ids": "group-1",
        "welcome_at_member": True,
        "welcome_message": "欢迎加入，QQ：{qq}，群号：{group_id}",
    }
    plugin = Main(object(), config)

    first_results = [
        result async for result in plugin.welcome_new_member(WelcomeEvent())
    ]
    second_results = [
        result async for result in plugin.welcome_new_member(WelcomeEvent())
    ]

    assert len(first_results) == 1
    assert first_results[0][0] == "chain"
    assert first_results[0][1][0].qq == "qq-new"
    assert first_results[0][1][1].text == "欢迎加入，QQ：qq-new，群号：group-1"
    assert second_results == []
