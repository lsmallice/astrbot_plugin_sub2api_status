from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star

from .claim import (
    is_account_request,
    is_bot_mentioned,
    is_claim_request,
    parse_binding_confirmation_code,
    parse_group_ids,
)
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
        self._welcome_lock = asyncio.Lock()
        self._welcome_seen: dict[tuple[str, str], float] = {}

    @staticmethod
    def _as_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}

    def _claim_service_key(self) -> str:
        return str(self.config.get("QQ_CLAIM_SERVICE_KEY") or "").strip()

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

    async def _send_private_message(
        self, event: AstrMessageEvent, message: str
    ) -> bool:
        """Send a sensitive reply through the current platform's private session."""
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
            logger.warning("QQ private message delivery failed")
            return False

    async def _send_binding_link_privately(
        self, event: AstrMessageEvent, message: str
    ) -> bool:
        """Send a binding challenge through the current platform's private session."""
        return await self._send_private_message(event, message)

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

    async def _account_reply(self, event: AstrMessageEvent, message: str):
        """Keep account details out of group messages when private delivery works."""
        group_getter = getattr(event, "get_group_id", None)
        group_id = str(group_getter() or "").strip() if callable(group_getter) else ""
        if not group_id:
            yield self._claim_reply(event, message)
            return
        if await self._send_private_message(event, message):
            yield self._claim_reply(event, "账户信息已私发，请在与机器人的私聊中查看。")
            return
        yield self._claim_reply(
            event,
            "账户信息仅支持私聊查询，请先私聊机器人发送任意消息后重试。",
        )

    async def _claim_account_reply(self, event: AstrMessageEvent, status_only: bool):
        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id:
            yield self._claim_reply(event, "无法识别当前 QQ 用户，请稍后重试。")
            return
        claim_key = self._claim_service_key()
        if not claim_key:
            yield self._claim_reply(event, "领取服务暂未配置，请联系管理员。")
            return
        try:
            account = await self._binding_client().claim_account(sender_id, claim_key)
        except BindingServiceError:
            logger.warning("QQ claim account lookup failed")
            yield self._claim_reply(event, "余额和领取状态暂时无法读取，请稍后重试。")
            return
        if not account.bound:
            yield self._claim_reply(
                event, "当前 QQ 尚未绑定 Smallice AI 账号。\n请先发送 /绑定。"
            )
            return
        claim = account.claim or {}
        status_labels = {
            "pending": "处理中",
            "credited": "已领取",
            "failed": "领取失败，可重试",
        }
        status = status_labels.get(str(claim.get("status") or ""), "未领取")
        campaign = account.campaign or {}
        amount = str(claim.get("amount") or campaign.get("amount") or "0")
        created_at = self._format_binding_expiry(account.created_at)
        account_status = {
            "active": "正常",
            "disabled": "已停用",
            "banned": "已封禁",
        }.get(str(account.status or "").casefold(), "未知")
        if status_only:
            async for result in self._account_reply(
                event,
                "领取状态\n"
                f"状态：{status}\n活动额度：{amount}\n"
                f"当前余额：{account.balance or '0'}\n"
                f"账号状态：{account_status}\n注册时间：{created_at}\n"
                "每个 QQ 用户和 Sub2API 账号仅可领取一次。",
            ):
                yield result
            return
        async for result in self._account_reply(
            event,
            f"Smallice AI 账户信息\n用户 ID：#{account.sub2api_user_id}\n"
            f"当前余额：{account.balance or '0'}\n账号状态：{account_status}\n"
            f"并发上限：{account.concurrency}\n注册时间：{created_at}\n"
            f"领取状态：{status}\n本次活动额度：{amount}\n"
            "如需领取，请在允许的群内 @机器人发送“领取”。",
        ):
            yield result

    @filter.command("余额")
    async def qq_balance(self, event: AstrMessageEvent):
        async for result in self._claim_account_reply(event, False):
            yield result

    @filter.command("账户")
    async def qq_account(self, event: AstrMessageEvent):
        async for result in self._claim_account_reply(event, False):
            yield result

    @filter.command("我的余额")
    async def qq_my_balance(self, event: AstrMessageEvent):
        async for result in self._claim_account_reply(event, False):
            yield result

    @filter.command("领取状态")
    async def qq_claim_status(self, event: AstrMessageEvent):
        async for result in self._claim_account_reply(event, True):
            yield result

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
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            return

        components = event.get_messages()
        if not is_bot_mentioned(components, event.get_self_id()):
            return

        if is_account_request(components, event.get_message_str()):
            status_only = "领取状态" in event.get_message_str()
            async for result in self._claim_account_reply(event, status_only):
                yield result
            return

        if not is_claim_request(components, event.get_message_str()):
            yield self._claim_reply(
                event,
                "请先发送 /绑定，将 QQ 与你自己的 Smallice AI 账号绑定。\n"
                "绑定完成后，请 @机器人 发送“领取”。",
            )
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
                claim_key = self._claim_service_key()
                if not claim_key:
                    yield self._claim_reply(event, "领取服务暂未配置，请联系管理员。")
                    return
                binding_client = self._binding_client()
                reservation = await binding_client.reserve_claim(
                    sender_id, group_id, claim_key
                )
                claim = reservation.claim
                claim_id = int(claim.get("id") or 0)
                if claim_id <= 0:
                    raise BindingServiceError("领取记录无效")
                if not reservation.can_attempt:
                    yield self._claim_reply(
                        event,
                        "该 QQ 用户或 Sub2API 账号已经领取过本活动，"
                        "每个用户只能领取一次。",
                    )
                    return
                client = self._client()
                user_id = int(claim.get("sub2api_user_id") or 0)
                amount = float(str(claim.get("amount") or "0"))
                idempotency_key = str(claim.get("idempotency_key") or "").strip()
                if user_id <= 0 or amount <= 0 or not idempotency_key:
                    raise BindingServiceError("领取记录信息不完整")
                notes = reservation.notes or str(
                    self.config.get("gift_notes") or "QQ群活动赠送余额"
                ).strip()
                try:
                    await client.add_balance(
                        user_id,
                        amount,
                        notes,
                        idempotency_key,
                    )
                except Exception as exc:
                    await binding_client.complete_claim(
                        claim_id, "failed", claim_key,
                        error_code=type(exc).__name__, error_message=str(exc),
                    )
                    raise
                await binding_client.complete_claim(claim_id, "credited", claim_key)
            except BindingServiceError as exc:
                logger.warning(
                    f"QQ claim service failed: {exc.code or type(exc).__name__}"
                )
                message = {
                    "QQ_NOT_BOUND": "当前 QQ 尚未绑定 Sub2API 账号，请先发送 /绑定。",
                    "CLAIM_DISABLED": "领取活动暂未开放，请稍后再试。",
                    "GROUP_NOT_ALLOWED": "当前群不在领取活动范围内。",
                    "REGISTRATION_WINDOW_EXPIRED": (
                        "你的 Smallice AI 账号已超过本次活动的领取时间限制，无法领取。"
                    ),
                }.get(exc.code, "领取服务暂时不可用，请稍后重试。")
                if exc.code in {"DUPLICATE_CLAIM", "CLAIM_ALREADY_CREDITED"}:
                    message = (
                        "该 QQ 用户或 Sub2API 账号已经领取过本活动，"
                        "每个用户只能领取一次。"
                    )
                yield self._claim_reply(event, message)
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
