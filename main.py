from __future__ import annotations

import asyncio
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star

from .claim import (
    is_bot_mentioned,
    is_claim_request,
    parse_binding_confirmation_code,
    parse_group_ids,
)
from .claim_store import ClaimStore
from .client import (
    BindingClient,
    BindingServiceError,
    ChannelSnapshot,
    Sub2APIClient,
    Sub2APIError,
)
from .renderer import (
    CARDS_PER_PAGE,
    TEXT_CARDS_PER_MESSAGE,
    chunked,
    format_status_text,
    render_status_svg,
    status_counts,
    svg_html_document,
)


class Main(Star):
    """Expose status and a guarded QQ-group balance gift workflow."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config
        self._fetch_lock = asyncio.Lock()
        self._cache: tuple[float, tuple[ChannelSnapshot, ...]] | None = None
        self._claim_lock = asyncio.Lock()
        self._claim_store: ClaimStore | None = None
        self._welcome_lock = asyncio.Lock()
        self._welcome_seen: dict[tuple[str, str], float] = {}

    @staticmethod
    def _as_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}

    def _gift_enabled(self) -> bool:
        return self._as_bool(self.config.get("gift_enabled", False))

    def _gift_amount(self) -> float | None:
        try:
            amount = float(self.config.get("gift_amount", 0))
        except (TypeError, ValueError):
            return None
        if not 0.01 <= amount <= 1000:
            return None
        return amount

    def _claim_registration_window_hours(self) -> float | None:
        """Return the configured new-account window; zero means disabled."""
        try:
            hours = float(self.config.get("claim_registration_window_hours", 0))
        except (TypeError, ValueError):
            return None
        if hours < 0 or hours > 87600:
            return None
        return hours

    def _welcome_enabled(self) -> bool:
        return self._as_bool(self.config.get("welcome_enabled", False))

    def _welcome_group_ids(self) -> set[str]:
        return parse_group_ids(self.config.get("welcome_group_ids"))

    def _welcome_message(self, qq_user_id: str, group_id: str) -> str:
        message = str(
            self.config.get("welcome_message")
            or (
                "欢迎加入本群！\n"
                "发送 /status 查询渠道状态，完成 QQ 绑定后可参与群内活动。"
            )
        ).strip()
        return (
            message.replace("{qq}", qq_user_id)
            .replace("{group_id}", group_id)
            .replace("{mention}", "")
            .strip()
        )

    def _claim_store_path(self) -> Path:
        configured = str(self.config.get("claim_db_path") or "").strip()
        if configured:
            return Path(configured).expanduser()
        try:
            from astrbot.api.star import StarTools

            return StarTools.get_data_dir() / "claims.sqlite3"
        except Exception:
            # Only used by isolated tests or an incomplete AstrBot bootstrap.
            return Path(tempfile.gettempdir()) / "astrbot_sub2api_status_claims.sqlite3"

    def _get_claim_store(self) -> ClaimStore:
        if self._claim_store is None:
            self._claim_store = ClaimStore(self._claim_store_path())
        return self._claim_store

    @staticmethod
    def _claim_reply(event: AstrMessageEvent, text: str):
        """Return a reply that also stops the default group conversation."""
        result = event.plain_result(text)
        stop_event = getattr(result, "stop_event", None)
        if callable(stop_event):
            stop_event()
        else:
            # Keeps isolated test doubles and older adapters compatible.
            event.stop_event()
        return result

    def _client(self) -> Sub2APIClient:
        base_url = str(self.config.get("base_url") or "").strip()
        admin_key = str(self.config.get("SUB2API_ADMIN_KEY") or "").strip()
        if not base_url:
            raise Sub2APIError("尚未配置 Sub2API Base URL")
        if not admin_key:
            raise Sub2APIError("尚未配置 SUB2API_ADMIN_KEY")
        try:
            return Sub2APIClient(base_url, admin_key)
        except ValueError as exc:
            raise Sub2APIError(str(exc)) from exc

    def _binding_client(self) -> BindingClient:
        base_url = str(
            self.config.get("binding_service_url")
            or "https://smallice.xyz/tools/api/invite"
        ).strip()
        service_key = str(self.config.get("QQ_BINDING_SERVICE_KEY") or "").strip()
        if not service_key:
            raise BindingServiceError("尚未配置 QQ_BINDING_SERVICE_KEY")
        try:
            return BindingClient(base_url, service_key)
        except ValueError as exc:
            raise BindingServiceError(str(exc)) from exc

    @staticmethod
    def _format_binding_expiry(value: str) -> str:
        """Render the sidecar expiry timestamp for Chinese-speaking users."""
        raw = str(value or "").strip()
        if not raw:
            return "未知"
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except (TypeError, ValueError):
            return raw

    async def _send_binding_link_privately(
        self, event: AstrMessageEvent, message: str
    ) -> bool:
        """Send a binding challenge through the current platform's private session."""
        sender_id = str(event.get_sender_id() or "").strip()
        platform_id_getter = getattr(event, "get_platform_id", None)
        if not sender_id or not callable(platform_id_getter):
            return False
        platform_id = str(platform_id_getter() or "").strip()
        send_message = getattr(self.context, "send_message", None)
        if not platform_id or not callable(send_message):
            return False
        try:
            from astrbot.api.event import MessageChain

            private_session = f"{platform_id}:FriendMessage:{sender_id}"
            await send_message(private_session, MessageChain().message(message))
            return True
        except Exception:
            logger.warning("QQ binding private message delivery failed")
            return False

    @filter.command("绑定")
    async def create_qq_binding(self, event: AstrMessageEvent):
        """Create a short-lived browser challenge and deliver it privately."""
        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id:
            yield event.plain_result("无法识别当前 QQ 用户，请稍后重试。")
            return
        try:
            challenge = await self._binding_client().create_challenge(sender_id)
        except BindingServiceError:
            logger.warning("QQ binding challenge creation failed")
            yield event.plain_result("绑定服务暂时不可用，请稍后重试或联系管理员。")
            return
        private_message = (
            "请打开下面的链接，并在 Smallice AI 主站登录后确认绑定：\n"
            f"{challenge.binding_url}\n"
            f"链接有效期至：{self._format_binding_expiry(challenge.expires_at)}（北京时间）\n"
            "该链接仅限你本人使用，请不要转发。"
        )
        if await self._send_binding_link_privately(event, private_message):
            yield event.plain_result(
                "绑定链接已私发，请在与机器人的私聊中查看。\n"
                "为保护账号，链接不会显示在群聊中。"
            )
            return
        yield event.plain_result(
            "绑定链接未能私发。请先私聊机器人发送任意消息，\n"
            "然后重新发送 /绑定；为保护账号，链接不会显示在群聊中。"
        )

    @filter.command("绑定确认")
    async def confirm_qq_binding(self, event: AstrMessageEvent):
        """Finish a browser binding from the same QQ identity that started it."""
        sender_id = str(event.get_sender_id() or "").strip()
        code = parse_binding_confirmation_code(event.get_message_str())
        if not sender_id:
            yield event.plain_result("无法识别当前 QQ 用户，请稍后重试。")
            return
        if not code:
            yield event.plain_result("格式：/绑定确认 6位数字验证码")
            return
        try:
            result = await self._binding_client().confirm_code(sender_id, code)
        except BindingServiceError:
            logger.warning("QQ binding code confirmation failed")
            yield event.plain_result(
                "验证码无效、已过期或绑定服务暂时不可用，请重新发送 /绑定。"
            )
            return
        if not result.bound:
            yield event.plain_result("绑定尚未完成，请重新发送 /绑定并按页面提示操作。")
            return
        yield event.plain_result(
            f"QQ 绑定成功，已绑定 Smallice AI 用户 #{result.sub2api_user_id}。\n"
            "现在可以在允许的 QQ 群中 @机器人发送“领取”。"
        )

    @filter.command("绑定状态")
    async def qq_binding_status(self, event: AstrMessageEvent):
        """Show the current QQ-to-Sub2API binding without exposing secrets."""
        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id:
            yield event.plain_result("无法识别当前 QQ 用户，请稍后重试。")
            return
        try:
            binding = await self._binding_client().lookup(sender_id)
        except BindingServiceError:
            logger.warning("QQ binding status lookup failed")
            yield event.plain_result("绑定状态暂时无法读取，请稍后重试。")
            return
        if binding is None:
            yield event.plain_result(
                "当前 QQ 尚未绑定 Sub2API 账号。\n请先发送 /绑定。"
            )
            return
        yield event.plain_result(
            f"当前 QQ 已绑定 Sub2API 用户 #{binding.id}。\n"
            "如需更换账号，请联系管理员处理，不能自行反复解绑重绑。"
        )

    async def _snapshots(self) -> tuple[ChannelSnapshot, ...]:
        """Return cached monitor data or refresh it from Sub2API.

        Returns:
            All enabled channel monitor snapshots.

        Raises:
            Sub2APIError: If configuration or the Sub2API request is invalid.
        """
        now = time.monotonic()
        if self._cache and now - self._cache[0] < 30:
            return self._cache[1]

        async with self._fetch_lock:
            now = time.monotonic()
            if self._cache and now - self._cache[0] < 30:
                return self._cache[1]

            client = self._client()
            snapshots = await client.fetch_snapshots()
            self._cache = (time.monotonic(), snapshots)
            return snapshots

    @filter.command("status")
    async def status(self, event: AstrMessageEvent):
        """查询并展示 Sub2API 渠道状态。"""
        try:
            snapshots = await self._snapshots()
        except Sub2APIError as exc:
            yield event.plain_result(f"渠道状态查询失败\n{exc}")
            return
        except Exception as exc:
            logger.warning(
                f"Sub2API status query failed with {type(exc).__name__}",
            )
            yield event.plain_result(
                "渠道状态查询失败\n服务暂时不可用，请稍后重试或联系管理员。",
            )
            return

        if not snapshots:
            yield event.plain_result("渠道状态\n当前没有已启用的渠道监控。")
            return

        generated_at = datetime.now(timezone.utc)
        counts = status_counts(snapshots)
        image_pages = chunked(snapshots, CARDS_PER_PAGE)
        image_paths: list[str] = []

        for page_number, page in enumerate(image_pages, start=1):
            svg = render_status_svg(
                page,
                all_counts=counts,
                total_channels=len(snapshots),
                page_number=page_number,
                page_count=len(image_pages),
                generated_at=generated_at,
            )
            try:
                image_path = await self.html_render(
                    svg_html_document(svg),
                    {},
                    return_url=False,
                    options={
                        "type": "png",
                        "full_page": True,
                        "omit_background": False,
                        "animations": "disabled",
                        "caret": "hide",
                        "scale": "css",
                    },
                )
                image_paths.append(image_path)
            except Exception as exc:
                logger.warning(
                    f"Sub2API status image rendering failed with {type(exc).__name__}",
                )
                image_paths.clear()
                break

        if image_paths:
            for image_path in image_paths:
                yield event.image_result(image_path)
            return

        # Text fallback is separately paginated to stay readable on IM platforms.
        text_pages = chunked(snapshots, TEXT_CARDS_PER_MESSAGE)
        for page_number, page in enumerate(text_pages, start=1):
            yield event.plain_result(
                format_status_text(
                    page,
                    all_counts=counts,
                    total_channels=len(snapshots),
                    page_number=page_number,
                    page_count=len(text_pages),
                    generated_at=generated_at,
                )
            )

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE
        | filter.EventMessageType.OTHER_MESSAGE
    )
    async def welcome_new_member(self, event: AstrMessageEvent):
        """Welcome a new member from a QQ OneBot group-increase notice."""
        if not self._welcome_enabled():
            return

        raw_event = getattr(
            getattr(event, "message_obj", None),
            "raw_message",
            None,
        )
        if not isinstance(raw_event, dict):
            return
        if (
            raw_event.get("post_type") != "notice"
            or raw_event.get("notice_type") != "group_increase"
        ):
            return
        sub_type = str(raw_event.get("sub_type") or "").strip().lower()
        if sub_type and sub_type not in {"approve", "invite"}:
            return

        group_id = str(
            raw_event.get("group_id") or event.get_group_id() or ""
        ).strip()
        qq_user_id = str(raw_event.get("user_id") or "").strip()
        allowed_groups = self._welcome_group_ids()
        if not group_id or not qq_user_id or group_id not in allowed_groups:
            return
        if qq_user_id == str(event.get_self_id() or "").strip():
            return

        now = time.monotonic()
        key = (group_id, qq_user_id)
        async with self._welcome_lock:
            self._welcome_seen = {
                seen_key: seen_at
                for seen_key, seen_at in self._welcome_seen.items()
                if now - seen_at < 600
            }
            if key in self._welcome_seen:
                return
            self._welcome_seen[key] = now

        message = self._welcome_message(qq_user_id, group_id)
        if not message:
            return
        chain = []
        if self._as_bool(self.config.get("welcome_at_member", True)):
            chain.append(At(qq=qq_user_id))
        chain.append(Plain(message))
        result = event.chain_result(chain)
        stop_event = getattr(result, "stop_event", None)
        if callable(stop_event):
            stop_event()
        yield result

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def claim_balance(self, event: AstrMessageEvent):
        """Handle ``@bot 领取`` in an allowed QQ group after QQ binding."""
        if not self._gift_enabled():
            return

        group_id = str(event.get_group_id() or "").strip()
        allowed_groups = parse_group_ids(self.config.get("gift_allowed_group_ids"))
        # An empty allowlist is intentionally fail-closed. This avoids enabling
        # a balance giveaway by merely installing or updating the plugin.
        if not group_id or not allowed_groups or group_id not in allowed_groups:
            return

        components = event.get_messages()
        if not is_bot_mentioned(components, event.get_self_id()):
            return

        if not is_claim_request(components, event.get_message_str()):
            yield self._claim_reply(
                event,
                "请先发送 /绑定，将 QQ 与你自己的 Smallice AI 账号绑定。\n"
                "绑定完成后，请 @机器人 发送“领取”。",
            )
            return

        amount = self._gift_amount()
        if amount is None:
            yield self._claim_reply(event, "领取活动暂未正确配置，请联系管理员。")
            return

        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id:
            yield self._claim_reply(event, "无法识别当前 QQ 用户，请稍后重试。")
            return

        # Serialize the local reservation and upstream side effect. This keeps
        # a pending row from being retried concurrently in this AstrBot process;
        # the same idempotency key also protects retries after a process restart.
        async with self._claim_lock:
            try:
                binding = await self._binding_client().lookup(sender_id)
                if binding is None:
                    yield self._claim_reply(
                        event,
                        "当前 QQ 尚未绑定 Sub2API 账号，请先发送 /绑定。",
                    )
                    return
                client = self._client()
                registration_window_hours = self._claim_registration_window_hours()
                if registration_window_hours is None:
                    yield self._claim_reply(
                        event,
                        "领取活动暂未正确配置，请联系管理员。",
                    )
                    return
                if registration_window_hours > 0:
                    created_at = await client.fetch_user_created_at(binding.id)
                    age_hours = (
                        datetime.now(timezone.utc) - created_at
                    ).total_seconds() / 3600
                    if age_hours < -0.05:
                        raise Sub2APIError("Sub2API 用户注册时间异常")
                    if age_hours > registration_window_hours:
                        yield self._claim_reply(
                            event,
                            "你的 Smallice AI 账号已超过本次活动的领取时间限制，"
                            "无法领取。",
                        )
                        return
                idempotency_key = f"astrbot-sub2api-gift-{sender_id}-{binding.id}"
                reservation = await asyncio.to_thread(
                    self._get_claim_store().reserve,
                    qq_user_id=sender_id,
                    sub2api_user_id=binding.id,
                    amount=amount,
                    idempotency_key=idempotency_key,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
                if not reservation.can_attempt:
                    yield self._claim_reply(
                        event,
                        "该 QQ 用户或 Sub2API 账号已经领取过本活动，"
                        "每个用户只能领取一次。",
                    )
                    return

                notes = str(self.config.get("gift_notes") or "QQ群活动赠送余额").strip()
                try:
                    await client.add_balance(
                        binding.id,
                        reservation.record.amount,
                        notes,
                        reservation.record.idempotency_key,
                    )
                except Exception as exc:
                    await asyncio.to_thread(
                        self._get_claim_store().mark_failed,
                        reservation.record.id,
                        error_code=type(exc).__name__,
                        error_message=str(exc),
                    )
                    raise

                await asyncio.to_thread(
                    self._get_claim_store().mark_credited,
                    reservation.record.id,
                    datetime.now(timezone.utc).isoformat(),
                )
            except BindingServiceError:
                logger.warning("QQ binding lookup failed during claim")
                yield self._claim_reply(
                    event,
                    "当前未能确认 QQ 绑定状态，请稍后重试。",
                )
                return
            except (Sub2APIError, OSError, RuntimeError):
                logger.warning(
                    "Sub2API gift claim failed with a protected error type",
                )
                yield self._claim_reply(
                    event,
                    "领取失败，余额未能确认到账，请稍后重试。",
                )
                return
            except Exception as exc:
                logger.warning(
                    f"Sub2API gift claim failed with {type(exc).__name__}",
                )
                yield self._claim_reply(
                    event,
                    "领取失败，服务暂时不可用，请稍后重试。",
                )
                return

        yield self._claim_reply(
            event,
            f"领取成功，已为你的 Sub2API 账号赠送 {amount:g} 余额。\n"
            "每个 QQ 用户和 Sub2API 账号仅可领取一次。",
        )
