from collections.abc import AsyncIterator

import pytest
from aiohttp import web

from astrbot_plugin_sub2api_status.client import (
    Sub2APIClient,
    Sub2APIError,
    Sub2APIUserAmbiguous,
    Sub2APIUserNotFound,
    admin_api_url,
    binding_api_url,
    normalize_base_url,
)


@pytest.fixture
async def sub2api_server(unused_tcp_port: int) -> AsyncIterator[tuple[str, list[int]]]:
    requested_pages: list[int] = []
    history_requests: list[int] = []

    async def monitors(request: web.Request) -> web.Response:
        assert request.headers["x-api-key"] == "admin-test-key"
        page = int(request.query["page"])
        requested_pages.append(page)
        items = [
            {
                "id": page,
                "name": f"Channel {page}",
                "provider": "openai",
                "primary_model": f"model-{page}",
                "primary_status": "operational",
                "group_name": f"Group {page}",
            }
        ]
        return web.json_response(
            {
                "code": 0,
                "message": "success",
                "data": {
                    "items": items,
                    "total": 2,
                    "page": page,
                    "page_size": 100,
                    "pages": 2,
                },
            }
        )

    async def history(request: web.Request) -> web.Response:
        monitor_id = int(request.match_info["monitor_id"])
        history_requests.append(monitor_id)
        assert request.query["limit"] == "60"
        assert request.query["model"] == f"model-{monitor_id}"
        return web.json_response(
            {
                "code": 0,
                "message": "success",
                "data": {
                    "items": [
                        {
                            "status": "operational",
                            "latency_ms": 100 + monitor_id,
                            "ping_latency_ms": 20 + monitor_id,
                        }
                    ]
                },
            }
        )

    async def groups(request: web.Request) -> web.Response:
        assert request.headers["x-api-key"] == "admin-test-key"
        return web.json_response(
            {
                "code": 0,
                "message": "success",
                "data": [
                    {
                        "id": 11,
                        "name": "Group 1",
                        "platform": "openai",
                        "rate_multiplier": 0.16,
                    },
                    {
                        "id": 12,
                        "name": "Group 2",
                        "platform": "openai",
                        "rate_multiplier": 0.08,
                    },
                ],
            }
        )

    app = web.Application()
    app.router.add_get("/api/v1/admin/channel-monitors", monitors)
    app.router.add_get(
        "/api/v1/admin/channel-monitors/{monitor_id}/history",
        history,
    )
    app.router.add_get("/api/v1/admin/groups/all", groups)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()
    try:
        yield f"http://127.0.0.1:{unused_tcp_port}", requested_pages
    finally:
        await runner.cleanup()


async def test_fetches_every_monitor_page_and_history(
    sub2api_server: tuple[str, list[int]],
) -> None:
    base_url, requested_pages = sub2api_server
    client = Sub2APIClient(base_url, "admin-test-key")

    snapshots = await client.fetch_snapshots()

    assert requested_pages == [1, 2]
    assert [snapshot.monitor["id"] for snapshot in snapshots] == [1, 2]
    assert snapshots[0].history[0]["ping_latency_ms"] == 21
    assert snapshots[1].history[0]["ping_latency_ms"] == 22
    assert snapshots[0].group_rate_multiplier == 0.16
    assert snapshots[1].group_rate_multiplier == 0.08


async def test_uses_embedded_timeline_without_history_request(
    unused_tcp_port: int,
) -> None:
    history_requests = 0

    async def monitors(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "code": 0,
                "message": "success",
                "data": {
                    "items": [
                        {
                            "id": 24,
                            "name": "GPT-PRO-稳定",
                            "provider": "openai",
                            "group_name": "GPT-PRO【稳定分组】",
                            "primary_model": "gpt-5.6-sol",
                            "primary_status": "operational",
                            "primary_ping_latency_ms": 20,
                            "timeline": [
                                {
                                    "status": "operational",
                                    "ping_latency_ms": 20,
                                    "checked_at": "2026-08-02T17:24:04Z",
                                }
                            ],
                        }
                    ],
                    "pages": 1,
                },
            }
        )

    async def history(_request: web.Request) -> web.Response:
        nonlocal history_requests
        history_requests += 1
        return web.json_response({"code": 0, "data": {"items": []}})

    async def groups(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "code": 0,
                "message": "success",
                "data": [
                    {
                        "id": 1,
                        "name": "GPT-PRO【稳定分组】",
                        "platform": "openai",
                        "rate_multiplier": 0.16,
                    }
                ],
            }
        )

    app = web.Application()
    app.router.add_get("/api/v1/admin/channel-monitors", monitors)
    app.router.add_get(
        "/api/v1/admin/channel-monitors/{monitor_id}/history",
        history,
    )
    app.router.add_get("/api/v1/admin/groups/all", groups)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()
    try:
        snapshots = await Sub2APIClient(
            f"http://127.0.0.1:{unused_tcp_port}",
            "admin-test-key",
        ).fetch_snapshots()
    finally:
        await runner.cleanup()

    assert history_requests == 0
    assert snapshots[0].history[0]["checked_at"] == "2026-08-02T17:24:04Z"
    assert snapshots[0].group_rate_multiplier == 0.16


def test_group_match_requires_exact_platform_and_name() -> None:
    rates = {
        ("openai", "Same Name"): 0.16,
        ("grok", "Same Name"): 0.5,
    }

    assert (
        Sub2APIClient._group_rate_for(
            {"provider": "openai", "group_name": "Same Name"},
            rates,
        )
        == 0.16
    )
    assert (
        Sub2APIClient._group_rate_for(
            {"provider": "openai", "group_name": "same name"},
            rates,
        )
        is None
    )


def test_normalizes_base_url_and_api_path() -> None:
    assert normalize_base_url("https://example.com/") == "https://example.com"
    assert (
        admin_api_url("https://example.com/api/v1", "admin/channel-monitors")
        == "https://example.com/api/v1/admin/channel-monitors"
    )
    assert (
        binding_api_url("https://smallice.xyz/tools/api/invite", "challenges")
        == "https://smallice.xyz/tools/api/invite/challenges"
    )
    assert (
        binding_api_url("http://127.0.0.1:8789", "challenges")
        == "http://127.0.0.1:8789/api/binding/challenges"
    )


@pytest.mark.parametrize(
    "value",
    [
        "ftp://example.com",
        "https://user:secret@example.com",
        "https://example.com?token=secret",
    ],
)
def test_rejects_unsafe_base_urls(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_base_url(value)


async def test_requires_admin_key() -> None:
    with pytest.raises(Sub2APIError, match="SUB2API_ADMIN_KEY"):
        await Sub2APIClient("https://example.com", "").fetch_snapshots()


async def test_fetch_user_created_at_reads_official_registration_time(
    unused_tcp_port: int,
) -> None:
    async def user(request: web.Request) -> web.Response:
        assert request.match_info["user_id"] == "42"
        assert request.headers["x-api-key"] == "admin-test-key"
        return web.json_response(
            {
                "code": 0,
                "message": "success",
                "data": {"id": 42, "created_at": "2026-08-21T00:00:00Z"},
            }
        )

    app = web.Application()
    app.router.add_get("/api/v1/admin/users/{user_id}", user)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()
    try:
        created_at = await Sub2APIClient(
            f"http://127.0.0.1:{unused_tcp_port}",
            "admin-test-key",
        ).fetch_user_created_at(42)
    finally:
        await runner.cleanup()

    assert created_at.isoformat() == "2026-08-21T00:00:00+00:00"


async def test_group_api_failure_does_not_hide_monitor_status(
    unused_tcp_port: int,
) -> None:
    async def monitors(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "code": 0,
                "message": "success",
                "data": {
                    "items": [
                        {
                            "id": 1,
                            "name": "Channel 1",
                            "provider": "openai",
                            "group_name": "Group 1",
                            "primary_status": "operational",
                            "timeline": [],
                        }
                    ],
                    "pages": 1,
                },
            }
        )

    async def groups(_request: web.Request) -> web.Response:
        return web.json_response({"code": 500, "message": "group service unavailable"})

    app = web.Application()
    app.router.add_get("/api/v1/admin/channel-monitors", monitors)
    app.router.add_get("/api/v1/admin/groups/all", groups)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()
    try:
        snapshots = await Sub2APIClient(
            f"http://127.0.0.1:{unused_tcp_port}",
            "admin-test-key",
        ).fetch_snapshots()
    finally:
        await runner.cleanup()

    assert len(snapshots) == 1
    assert snapshots[0].monitor["primary_status"] == "operational"
    assert snapshots[0].group_rate_multiplier is None


async def test_find_user_requires_exact_match(unused_tcp_port: int) -> None:
    async def users(request: web.Request) -> web.Response:
        assert request.query["search"] == "alice@example.com"
        assert request.query["status"] == "active"
        return web.json_response(
            {
                "code": 0,
                "message": "success",
                "data": {
                    "items": [
                        {"id": 1, "email": "alice@example.com", "username": "alice"},
                        {
                            "id": 2,
                            "email": "alice@example.com.backup",
                            "username": "other",
                        },
                    ],
                    "pages": 1,
                },
            }
        )

    app = web.Application()
    app.router.add_get("/api/v1/admin/users", users)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()
    try:
        user = await Sub2APIClient(
            f"http://127.0.0.1:{unused_tcp_port}", "admin-test-key"
        ).find_user("alice@example.com")
    finally:
        await runner.cleanup()

    assert user.id == 1
    assert user.email == "alice@example.com"


async def test_find_user_reports_not_found_and_ambiguous(
    unused_tcp_port: int,
) -> None:
    async def users(request: web.Request) -> web.Response:
        identifier = request.query["search"]
        items = (
            []
            if identifier == "missing"
            else [
                {"id": 1, "email": "same", "username": "same"},
                {"id": 2, "email": "other", "username": "same"},
            ]
        )
        return web.json_response({"code": 0, "data": {"items": items}})

    app = web.Application()
    app.router.add_get("/api/v1/admin/users", users)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()
    try:
        client = Sub2APIClient(f"http://127.0.0.1:{unused_tcp_port}", "admin-test-key")
        with pytest.raises(Sub2APIUserNotFound):
            await client.find_user("missing")
        with pytest.raises(Sub2APIUserAmbiguous):
            await client.find_user("same")
    finally:
        await runner.cleanup()


async def test_add_balance_uses_official_body_and_idempotency_key(
    unused_tcp_port: int,
) -> None:
    async def balance(request: web.Request) -> web.Response:
        assert request.headers["x-api-key"] == "admin-test-key"
        assert request.headers["Idempotency-Key"] == "gift-stable"
        assert await request.json() == {
            "balance": 12.5,
            "operation": "add",
            "notes": "QQ群活动赠送余额",
        }
        return web.json_response({"code": 0, "message": "success", "data": {}})

    app = web.Application()
    app.router.add_post("/api/v1/admin/users/42/balance", balance)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()
    try:
        await Sub2APIClient(
            f"http://127.0.0.1:{unused_tcp_port}", "admin-test-key"
        ).add_balance(42, 12.5, "QQ群活动赠送余额", "gift-stable")
    finally:
        await runner.cleanup()
