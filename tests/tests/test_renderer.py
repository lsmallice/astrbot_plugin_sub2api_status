from datetime import datetime, timezone

from astrbot_plugin_sub2api_status.client import ChannelSnapshot
from astrbot_plugin_sub2api_status.renderer import (
    CARDS_PER_PAGE,
    chunked,
    format_status_text,
    render_status_svg,
    status_counts,
)


def make_snapshot(index: int, status: str = "operational") -> ChannelSnapshot:
    monitor = {
        "id": index,
        "name": f"渠道 <{index}>",
        "provider": "openai" if index % 2 else "grok",
        "group_name": f"分组 {index}",
        "primary_model": f"model-{index}",
        "primary_status": status,
        "primary_latency_ms": 1000 + index,
        "availability_7d": 98.5 - index,
        "last_checked_at": "2026-07-31T08:20:00Z",
        "extra_models": [f"extra-{index}"],
        "api_key_masked": "admin-secret-must-not-appear",
        "endpoint": "https://private.example.invalid",
    }
    history = (
        {
            "status": status,
            "ping_latency_ms": 20 + index,
            "checked_at": "2026-07-31T08:20:00Z",
        },
    )
    return ChannelSnapshot(monitor, history, group_rate_multiplier=0.16)


def test_dynamic_channel_count_splits_into_image_pages() -> None:
    snapshots = tuple(make_snapshot(index) for index in range(1, 13))

    pages = chunked(snapshots, CARDS_PER_PAGE)

    assert [len(page) for page in pages] == [5, 5, 2]


def test_svg_escapes_values_and_does_not_expose_credentials() -> None:
    snapshots = (
        make_snapshot(1),
        make_snapshot(2, "degraded"),
        make_snapshot(3, "error"),
    )
    generated_at = datetime(2026, 7, 31, 8, 30, tzinfo=timezone.utc)

    svg = render_status_svg(
        snapshots,
        all_counts=status_counts(snapshots),
        total_channels=len(snapshots),
        page_number=1,
        page_count=1,
        generated_at=generated_at,
    )

    assert "渠道 &lt;1&gt;" in svg
    assert "正常 1" in svg
    assert "降级 1" in svg
    assert "故障 1" in svg
    assert "21" in svg
    assert "分组 1 · 默认倍率 ×0.16" in svg
    assert "admin-secret-must-not-appear" not in svg
    assert "private.example.invalid" not in svg


def test_text_fallback_is_readable_and_complete() -> None:
    snapshots = (
        make_snapshot(1),
        make_snapshot(2, "degraded"),
        make_snapshot(3, "error"),
    )

    text = format_status_text(
        snapshots,
        all_counts=status_counts(snapshots),
        total_channels=len(snapshots),
        page_number=1,
        page_count=1,
        generated_at=datetime(2026, 7, 31, 8, 30, tzinfo=timezone.utc),
    )

    assert "渠道 3 | 正常 1 | 降级 1 | 故障 1 | 未知 0" in text
    assert "[正常] 渠道 <1>" in text
    assert "[降级] 渠道 <2>" in text
    assert "[故障] 渠道 <3>" in text
    assert "端点 PING 21 ms" in text
    assert "分组 分组 1 · 默认倍率 ×0.16" in text
    assert "历史" in text
    assert "admin-secret-must-not-appear" not in text
