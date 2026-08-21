from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp


class Sub2APIError(RuntimeError):
    """Raised when Sub2API cannot provide a valid monitor response."""


class Sub2APIUserNotFound(Sub2APIError):
    """The requested email/username does not identify an active user."""


class Sub2APIUserAmbiguous(Sub2APIError):
    """The requested identifier matches more than one active user."""


class BindingServiceError(RuntimeError):
    """Raised when the QQ binding sidecar rejects or cannot process a request."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ChannelSnapshot:
    """A monitor and its recent primary-model history."""

    monitor: dict[str, Any]
    history: tuple[dict[str, Any], ...]
    history_available: bool = True
    group_rate_multiplier: float | None = None


@dataclass(frozen=True)
class Sub2APIUser:
    """The minimal user data needed by the gift workflow."""

    id: int
    email: str
    username: str


@dataclass(frozen=True)
class BindingChallenge:
    binding_url: str
    expires_at: str


@dataclass(frozen=True)
class BoundSub2APIUser:
    id: int
    email: str = ""
    username: str = ""


@dataclass(frozen=True)
class BindingConfirmation:
    bound: bool
    pending: bool = False
    sub2api_user_id: int | None = None
    confirmation_code: str = ""


@dataclass(frozen=True)
class QQClaimAccount:
    bound: bool
    sub2api_user_id: int | None = None
    balance: str = ""
    status: str = ""
    concurrency: int = 0
    created_at: str = ""
    claim: dict[str, Any] | None = None
    campaign: dict[str, Any] | None = None


@dataclass(frozen=True)
class QQClaimReservation:
    claim: dict[str, Any]
    can_attempt: bool
    notes: str = ""


def normalize_base_url(raw_url: str) -> str:
    """Validate and normalize a configured Sub2API base URL.

    Args:
        raw_url: Root URL entered in the AstrBot plugin configuration.

    Returns:
        A normalized URL without a trailing slash.

    Raises:
        ValueError: If the URL is unsafe or malformed.
    """
    value = raw_url.strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("Base URL 必须是有效的 http:// 或 https:// 地址")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Base URL 不能包含账号、密码、查询参数或锚点")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def admin_api_url(base_url: str, path: str) -> str:
    """Build a Sub2API administrator endpoint URL.

    Args:
        base_url: A normalized Sub2API root URL.
        path: Administrator API path below ``/api/v1``.

    Returns:
        The complete administrator endpoint URL.
    """
    prefix = base_url if base_url.endswith("/api/v1") else f"{base_url}/api/v1"
    return f"{prefix}/{path.lstrip('/')}"


def binding_api_url(base_url: str, path: str) -> str:
    """Build an endpoint URL for the external QQ binding sidecar."""
    return f"{base_url.rstrip('/')}/api/binding/{path.lstrip('/')}"


def claim_api_url(base_url: str, path: str) -> str:
    """Build an endpoint URL for the guarded QQ claim API."""
    return f"{base_url.rstrip('/')}/api/{path.lstrip('/')}"


class Sub2APIClient:
    """Small asynchronous client for Sub2API channel monitor endpoints."""

    def __init__(
        self,
        base_url: str,
        admin_key: str,
        *,
        timeout_seconds: int = 15,
        history_limit: int = 60,
        history_concurrency: int = 8,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.admin_key = admin_key.strip()
        self.timeout_seconds = timeout_seconds
        self.history_limit = history_limit
        self.history_concurrency = history_concurrency

    async def _request_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Request and validate a standard Sub2API JSON response.

        Args:
            session: Authenticated aiohttp session.
            url: Complete request URL.
            params: Optional query parameters.

        Returns:
            The decoded response object.

        Raises:
            Sub2APIError: If transport, authentication, or payload validation fails.
        """
        try:
            headers = extra_headers or {}
            async with session.request(
                method,
                url,
                params=params,
                json=body,
                headers=headers,
                allow_redirects=False,
            ) as response:
                if response.status == 401:
                    raise Sub2APIError("管理员 Key 无效、已失效或尚未配置")
                if response.status == 403:
                    raise Sub2APIError("管理员 Key 没有访问渠道监控的权限")
                if response.status < 200 or response.status >= 300:
                    raise Sub2APIError(f"Sub2API 返回 HTTP {response.status}")
                payload = await response.json(content_type=None)
        except Sub2APIError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise Sub2APIError("无法连接 Sub2API，请检查 Base URL 和网络") from exc
        except ValueError as exc:
            raise Sub2APIError("Sub2API 返回了无法解析的响应") from exc

        if not isinstance(payload, dict):
            raise Sub2APIError("Sub2API 响应体格式不正确")
        if payload.get("code") != 0:
            message = str(payload.get("message") or "未知错误").strip()
            raise Sub2APIError(f"Sub2API 查询失败：{message[:120]}")
        return payload

    async def _get_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._request_json(session, url, params=params)

    async def fetch_user_created_at(self, user_id: int) -> datetime:
        """Fetch the official registration time for one Sub2API user."""
        if user_id <= 0:
            raise Sub2APIError("Sub2API 用户 ID 无效")
        if not self.admin_key:
            raise Sub2APIError("尚未配置 SUB2API_ADMIN_KEY")

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        headers = {
            "Accept": "application/json",
            "User-Agent": "astrbot-plugin-sub2api-status/1.4.1",
            "x-api-key": self.admin_key,
        }
        user_url = admin_api_url(self.base_url, f"admin/users/{user_id}")
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
            trust_env=True,
        ) as session:
            payload = await self._get_json(session, user_url)

        data = payload.get("data")
        if not isinstance(data, dict):
            raise Sub2APIError("Sub2API 用户响应缺少 data")
        raw_created_at = str(data.get("created_at") or "").strip()
        if not raw_created_at:
            raise Sub2APIError("Sub2API 用户信息缺少注册时间")
        try:
            created_at = datetime.fromisoformat(raw_created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise Sub2APIError("Sub2API 用户注册时间格式不正确") from exc
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return created_at.astimezone(timezone.utc)

    async def find_user(self, identifier: str) -> Sub2APIUser:
        """Resolve one active user by an exact email or username match.

        Sub2API's search endpoint is intentionally fuzzy. The exact match
        check here prevents an input such as ``alice`` from silently selecting
        the first result returned by the administrator list API.
        """
        identifier = identifier.strip()
        if not identifier:
            raise Sub2APIUserNotFound("账号不能为空")
        if not self.admin_key:
            raise Sub2APIError("尚未配置 SUB2API_ADMIN_KEY")

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        headers = {
            "Accept": "application/json",
            "User-Agent": "astrbot-plugin-sub2api-status/1.4.1",
            "x-api-key": self.admin_key,
        }
        users_url = admin_api_url(self.base_url, "admin/users")
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
            trust_env=True,
        ) as session:
            payload = await self._get_json(
                session,
                users_url,
                params={
                    "page": 1,
                    "page_size": 100,
                    "search": identifier,
                    "status": "active",
                    "include_subscriptions": "false",
                },
            )

        data = payload.get("data")
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise Sub2APIError("Sub2API 用户列表缺少 data.items")

        needle = identifier.casefold()
        matches = [
            item
            for item in items
            if isinstance(item, dict)
            and (
                str(item.get("email") or "").strip().casefold() == needle
                or str(item.get("username") or "").strip().casefold() == needle
            )
        ]
        if not matches:
            raise Sub2APIUserNotFound("未找到对应的有效账号")
        if len(matches) != 1:
            raise Sub2APIUserAmbiguous("账号匹配到多个用户，请改用邮箱领取")

        item = matches[0]
        try:
            user_id = int(item["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise Sub2APIError("Sub2API 用户信息格式不正确") from exc
        return Sub2APIUser(
            id=user_id,
            email=str(item.get("email") or "").strip(),
            username=str(item.get("username") or "").strip(),
        )

    async def add_balance(
        self,
        user_id: int,
        amount: float,
        notes: str,
        idempotency_key: str,
    ) -> None:
        """Add balance through the official idempotent admin endpoint."""
        if not self.admin_key:
            raise Sub2APIError("尚未配置 SUB2API_ADMIN_KEY")
        if amount <= 0:
            raise Sub2APIError("赠送额度必须大于 0")
        if not idempotency_key.strip():
            raise Sub2APIError("领取幂等键不能为空")

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        headers = {
            "Accept": "application/json",
            "User-Agent": "astrbot-plugin-sub2api-status/1.4.1",
            "x-api-key": self.admin_key,
        }
        balance_url = admin_api_url(self.base_url, f"admin/users/{user_id}/balance")
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
            trust_env=True,
        ) as session:
            await self._request_json(
                session,
                balance_url,
                method="POST",
                body={
                    "balance": amount,
                    "operation": "add",
                    "notes": notes.strip(),
                },
                extra_headers={"Idempotency-Key": idempotency_key.strip()},
            )

    async def fetch_snapshots(self) -> tuple[ChannelSnapshot, ...]:
        """Fetch all monitors, histories, and matching default group rates.

        Returns:
            Monitor snapshots in the order returned by Sub2API.

        Raises:
            Sub2APIError: If the monitor list cannot be queried.
        """
        if not self.admin_key:
            raise Sub2APIError("尚未配置 SUB2API_ADMIN_KEY")

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        headers = {
            "Accept": "application/json",
            "User-Agent": "astrbot-plugin-sub2api-status/1.4.1",
            "x-api-key": self.admin_key,
        }
        list_url = admin_api_url(self.base_url, "admin/channel-monitors")

        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
            trust_env=True,
        ) as session:
            monitors: list[dict[str, Any]] = []
            page = 1
            while True:
                payload = await self._get_json(
                    session,
                    list_url,
                    params={"page": page, "page_size": 100, "enabled": "true"},
                )
                data = payload.get("data")
                if not isinstance(data, dict) or not isinstance(
                    data.get("items"),
                    list,
                ):
                    raise Sub2APIError("Sub2API 渠道列表缺少 data.items")
                monitors.extend(
                    item for item in data["items"] if isinstance(item, dict)
                )

                try:
                    pages = max(1, int(data.get("pages", 1)))
                except (TypeError, ValueError) as exc:
                    raise Sub2APIError("Sub2API 分页信息格式不正确") from exc
                if page >= pages:
                    break
                page += 1
                if page > 1000:
                    raise Sub2APIError("Sub2API 返回的渠道分页数量异常")

            group_rates_task = asyncio.create_task(self._fetch_group_rates(session))
            semaphore = asyncio.Semaphore(self.history_concurrency)

            async def load_history(monitor: dict[str, Any]) -> ChannelSnapshot:
                timeline = monitor.get("timeline")
                if isinstance(timeline, list):
                    history = tuple(item for item in timeline if isinstance(item, dict))
                    return ChannelSnapshot(monitor, history)

                monitor_id = monitor.get("id")
                primary_model = str(monitor.get("primary_model") or "").strip()
                if not isinstance(monitor_id, int) or monitor_id <= 0:
                    return ChannelSnapshot(monitor, (), False)

                history_url = admin_api_url(
                    self.base_url,
                    f"admin/channel-monitors/{monitor_id}/history",
                )
                params: dict[str, Any] = {"limit": self.history_limit}
                if primary_model:
                    params["model"] = primary_model
                try:
                    async with semaphore:
                        payload = await self._get_json(
                            session,
                            history_url,
                            params=params,
                        )
                    data = payload.get("data")
                    items = data.get("items") if isinstance(data, dict) else None
                    if not isinstance(items, list):
                        return ChannelSnapshot(monitor, (), False)
                    history = tuple(item for item in items if isinstance(item, dict))
                    return ChannelSnapshot(monitor, history)
                except Sub2APIError:
                    # One failed history request must not hide the whole status.
                    return ChannelSnapshot(monitor, (), False)

            snapshots_without_rates = await asyncio.gather(
                *(load_history(item) for item in monitors)
            )
            group_rates = await group_rates_task
            return tuple(
                ChannelSnapshot(
                    snapshot.monitor,
                    snapshot.history,
                    snapshot.history_available,
                    self._group_rate_for(snapshot.monitor, group_rates),
                )
                for snapshot in snapshots_without_rates
            )

    async def _fetch_group_rates(
        self,
        session: aiohttp.ClientSession,
    ) -> dict[tuple[str, str], float]:
        """Fetch active group rates without making them required for status output."""
        groups_url = admin_api_url(self.base_url, "admin/groups/all")
        try:
            payload = await self._get_json(session, groups_url)
        except Sub2APIError:
            return {}

        groups = payload.get("data")
        if not isinstance(groups, list):
            return {}

        rates: dict[tuple[str, str], float] = {}
        duplicates: set[tuple[str, str]] = set()
        for group in groups:
            if not isinstance(group, dict):
                continue
            name = str(group.get("name") or "").strip()
            platform = str(group.get("platform") or "").strip().lower()
            try:
                rate = float(group.get("rate_multiplier"))
            except (TypeError, ValueError):
                continue
            if not name or not platform or rate < 0:
                continue
            key = (platform, name)
            if key in rates:
                duplicates.add(key)
                continue
            rates[key] = rate

        for key in duplicates:
            rates.pop(key, None)
        return rates

    @staticmethod
    def _group_rate_for(
        monitor: dict[str, Any],
        group_rates: dict[tuple[str, str], float],
    ) -> float | None:
        """Match a monitor to one unambiguous group by provider and full name."""
        provider = str(monitor.get("provider") or "").strip().lower()
        group_name = str(monitor.get("group_name") or "").strip()
        if not provider or not group_name:
            return None
        return group_rates.get((provider, group_name))


class BindingClient:
    """Client for the sidecar that owns QQ-to-Sub2API bindings."""

    def __init__(
        self,
        base_url: str,
        service_key: str,
        *,
        timeout_seconds: int = 10,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.service_key = service_key.strip()
        self.timeout_seconds = timeout_seconds

    async def _request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        service_header: str = "X-Binding-Service-Key",
        service_key: str | None = None,
    ) -> dict[str, Any]:
        active_key = (service_key or self.service_key).strip()
        if not active_key:
            raise BindingServiceError("尚未配置 QQ_BINDING_SERVICE_KEY")
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        headers = {
            "Accept": "application/json",
            "User-Agent": "astrbot-plugin-sub2api-status/1.4.1",
            service_header: active_key,
        }
        try:
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers=headers,
                trust_env=True,
            ) as session:
                async with session.request(
                    method,
                    url,
                    params=params,
                    json=body,
                    allow_redirects=False,
                ) as response:
                    payload = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise BindingServiceError("无法连接 QQ 绑定服务，请稍后重试") from exc

        if not isinstance(payload, dict):
            raise BindingServiceError("QQ 绑定服务返回了无效响应")
        if (
            response.status < 200
            or response.status >= 300
            or payload.get("ok") is not True
        ):
            message = str(payload.get("message") or "QQ 绑定服务请求失败").strip()
            raise BindingServiceError(message[:160], str(payload.get("code") or ""))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise BindingServiceError("QQ 绑定服务响应缺少 data")
        return data

    async def create_challenge(self, qq_user_id: str) -> BindingChallenge:
        data = await self._request_json(
            binding_api_url(self.base_url, "challenges"),
            method="POST",
            body={"qq_user_id": qq_user_id.strip()},
        )
        binding_url = str(data.get("binding_url") or "").strip()
        expires_at = str(data.get("expires_at") or "").strip()
        if not binding_url or not expires_at:
            raise BindingServiceError("QQ 绑定服务返回的链接不完整")
        return BindingChallenge(binding_url=binding_url, expires_at=expires_at)

    async def lookup(self, qq_user_id: str) -> BoundSub2APIUser | None:
        data = await self._request_json(
            binding_api_url(self.base_url, "lookup"),
            params={"qq_user_id": qq_user_id.strip()},
        )
        if not bool(data.get("bound")):
            return None
        try:
            user_id = int(data["sub2api_user_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BindingServiceError("QQ 绑定服务返回的账号信息不完整") from exc
        return BoundSub2APIUser(
            id=user_id,
            email=str(data.get("email_snapshot") or "").strip(),
            username=str(data.get("username_snapshot") or "").strip(),
        )

    async def confirm_code(
        self,
        qq_user_id: str,
        confirmation_code: str,
    ) -> BindingConfirmation:
        data = await self._request_json(
            binding_api_url(self.base_url, "confirm-code"),
            method="POST",
            body={
                "qq_user_id": qq_user_id.strip(),
                "confirmation_code": confirmation_code.strip(),
            },
        )
        user_id = data.get("sub2api_user_id")
        try:
            parsed_user_id = int(user_id) if user_id is not None else None
        except (TypeError, ValueError) as exc:
            raise BindingServiceError("QQ 绑定服务返回的账号信息不完整") from exc
        return BindingConfirmation(
            bound=bool(data.get("bound")),
            pending=bool(data.get("pending")),
            sub2api_user_id=parsed_user_id,
            confirmation_code=str(data.get("confirmation_code") or "").strip(),
        )

    async def claim_account(
        self, qq_user_id: str, claim_service_key: str
    ) -> QQClaimAccount:
        data = await self._request_json(
            claim_api_url(self.base_url, "bot/qq/account"),
            params={"qq_user_id": qq_user_id.strip()},
            service_header="X-QQ-Claim-Service-Key",
            service_key=claim_service_key,
        )
        raw_user_id = data.get("sub2api_user_id")
        try:
            user_id = int(raw_user_id) if raw_user_id is not None else None
        except (TypeError, ValueError) as exc:
            raise BindingServiceError("QQ 领取服务返回的账号信息不完整") from exc
        return QQClaimAccount(
            bound=bool(data.get("bound")),
            sub2api_user_id=user_id,
            balance=str(data.get("balance") or ""),
            status=str(data.get("status") or "").strip(),
            concurrency=int(data.get("concurrency") or 0),
            created_at=str(data.get("created_at") or ""),
            claim=data.get("claim") if isinstance(data.get("claim"), dict) else None,
            campaign=(
                data.get("campaign")
                if isinstance(data.get("campaign"), dict)
                else None
            ),
        )

    async def reserve_claim(
        self, qq_user_id: str, group_id: str, claim_service_key: str
    ) -> QQClaimReservation:
        data = await self._request_json(
            claim_api_url(self.base_url, "bot/qq/claims/reserve"),
            method="POST",
            body={"qq_user_id": qq_user_id.strip(), "group_id": group_id.strip()},
            service_header="X-QQ-Claim-Service-Key",
            service_key=claim_service_key,
        )
        claim = data.get("claim")
        if not isinstance(claim, dict) or not claim.get("id"):
            raise BindingServiceError("QQ 领取服务返回的领取记录不完整")
        return QQClaimReservation(
            claim=claim,
            can_attempt=bool(data.get("can_attempt")),
            notes=str(data.get("notes") or "").strip(),
        )

    async def complete_claim(
        self,
        claim_id: int,
        status: str,
        claim_service_key: str,
        *,
        error_code: str = "",
        error_message: str = "",
    ) -> dict[str, Any]:
        data = await self._request_json(
            claim_api_url(self.base_url, f"bot/qq/claims/{claim_id}/complete"),
            method="POST",
            body={
                "status": status,
                "error_code": error_code,
                "error_message": error_message[:500],
            },
            service_header="X-QQ-Claim-Service-Key",
            service_key=claim_service_key,
        )
        claim = data.get("claim")
        if not isinstance(claim, dict):
            raise BindingServiceError("QQ 领取服务返回的完成记录不完整")
        return claim
