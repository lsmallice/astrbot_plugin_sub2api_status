from __future__ import annotations

import html
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from .client import ChannelSnapshot

CARDS_PER_PAGE = 5
TEXT_CARDS_PER_MESSAGE = 8
HISTORY_SIZE = 60
T = TypeVar("T")

STATUS_META = {
    "operational": ("正常", "#20c997"),
    "degraded": ("降级", "#f4b740"),
    "error": ("故障", "#ff5a65"),
    "unknown": ("未知", "#8d9bb0"),
}

PROVIDER_META = {
    "openai": ("OpenAI", "OA", "#21c99a"),
    "anthropic": ("Anthropic", "AN", "#e49a62"),
    "gemini": ("Gemini", "GE", "#5b8ff9"),
    "grok": ("Grok", "GR", "#d8dee9"),
}


def chunked(items: Sequence[T], size: int) -> tuple[tuple[T, ...], ...]:
    """Split a sequence into stable fixed-size pages.

    Args:
        items: Source sequence.
        size: Maximum number of items per page.

    Returns:
        Tuple containing every page.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    return tuple(tuple(items[i : i + size]) for i in range(0, len(items), size))


def normalize_status(value: Any) -> str:
    """Normalize monitor statuses to the four display states.

    Args:
        value: Raw status value from Sub2API.

    Returns:
        One of operational, degraded, error, or unknown.
    """
    status = str(value or "").strip().lower()
    return status if status in STATUS_META else "unknown"


def status_counts(snapshots: Iterable[ChannelSnapshot]) -> dict[str, int]:
    """Count display statuses across all monitor snapshots.

    Args:
        snapshots: Monitor snapshots.

    Returns:
        Mapping from normalized status to count.
    """
    counts = {status: 0 for status in STATUS_META}
    for snapshot in snapshots:
        counts[normalize_status(snapshot.monitor.get("primary_status"))] += 1
    return counts


def _display_width(value: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(char) in {"W", "F", "A"} else 1
        for char in value
    )


def _truncate(value: Any, max_width: int) -> str:
    text = str(value or "").strip()
    if _display_width(text) <= max_width:
        return text
    chars: list[str] = []
    width = 0
    for char in text:
        char_width = (
            2 if unicodedata.east_asian_width(char) in {"W", "F", "A"} else 1
        )
        if width + char_width > max_width - 3:
            break
        chars.append(char)
        width += char_width
    return "".join(chars) + "..."


def _provider_meta(value: Any) -> tuple[str, str, str]:
    provider = str(value or "").strip().lower()
    if provider in PROVIDER_META:
        return PROVIDER_META[provider]
    label = _truncate(provider.title() or "Other", 16)
    initials = "".join(part[:1] for part in label.split())[:2].upper() or "AI"
    return label, initials, "#58b7c8"


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _local_time(value: Any) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return "暂无记录"
    cst = timezone(timedelta(hours=8))
    return parsed.astimezone(cst).strftime("%m-%d %H:%M")


def _age_text(value: Any, now: datetime) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return "尚未检测"
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    seconds = max(0, int((now.astimezone(timezone.utc) - parsed).total_seconds()))
    if seconds < 60:
        return f"{seconds} 秒前刷新"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前刷新"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前刷新"
    return f"{seconds // 86400} 天前刷新"


def _number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return None


def _latest_ping(snapshot: ChannelSnapshot) -> int | None:
    monitor_ping = _number(snapshot.monitor.get("primary_ping_latency_ms"))
    if monitor_ping is not None:
        return monitor_ping
    if not snapshot.history:
        return None
    return _number(snapshot.history[0].get("ping_latency_ms"))


def _latest_checked_at(snapshot: ChannelSnapshot) -> Any:
    checked_at = snapshot.monitor.get("last_checked_at")
    if checked_at:
        return checked_at
    if snapshot.history:
        return snapshot.history[0].get("checked_at")
    return None


def _format_rate(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _group_summary(snapshot: ChannelSnapshot) -> str:
    group_name = str(snapshot.monitor.get("group_name") or "").strip()
    if not group_name:
        return "未关联分组"
    rate = _format_rate(snapshot.group_rate_multiplier)
    return f"{_truncate(group_name, 22)} · 默认倍率 ×{rate}"


def _history_states(snapshot: ChannelSnapshot) -> list[str | None]:
    states = [
        normalize_status(item.get("status")) for item in reversed(snapshot.history)
    ][-HISTORY_SIZE:]
    return [None] * (HISTORY_SIZE - len(states)) + states


def _availability_color(value: float) -> str:
    if value >= 80:
        return "#67d84b"
    if value >= 50:
        return "#f0c83b"
    return "#ff636d"


def render_status_svg(
    snapshots: Sequence[ChannelSnapshot],
    *,
    all_counts: dict[str, int],
    total_channels: int,
    page_number: int,
    page_count: int,
    generated_at: datetime | None = None,
    title: str = "Smallice AI · 渠道状态",
) -> str:
    """Render one page of monitor cards as a standalone SVG document.

    Args:
        snapshots: Monitor snapshots displayed on this page.
        all_counts: Global status counts for every monitor.
        total_channels: Total number of enabled monitors.
        page_number: One-based page number.
        page_count: Total image page count.
        generated_at: Rendering time, mainly for deterministic tests.
        title: Header title.

    Returns:
        Complete SVG markup.
    """
    now = generated_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    card_height = 250
    card_gap = 18
    header_height = 154
    footer_height = 64
    height = (
        header_height
        + len(snapshots) * card_height
        + max(0, len(snapshots) - 1) * card_gap
        + footer_height
    )

    overall_status = "全部正常"
    overall_color = STATUS_META["operational"][1]
    if all_counts.get("error", 0):
        overall_status = "部分故障"
        overall_color = STATUS_META["error"][1]
    elif all_counts.get("degraded", 0):
        overall_status = "服务降级"
        overall_color = STATUS_META["degraded"][1]
    elif all_counts.get("unknown", 0):
        overall_status = "状态未知"
        overall_color = STATUS_META["unknown"][1]

    parts = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="960" height="{height}" '
            f'viewBox="0 0 960 {height}" role="img" '
            f'aria-label="{html.escape(title, quote=True)}">'
        ),
        """
<style>
text {
  font-family: Inter, "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
  letter-spacing: 0;
}
.title { fill: #f5f8ff; font-size: 30px; font-weight: 700; }
.muted { fill: #8795aa; font-size: 14px; }
.name { fill: #f3f6fc; font-size: 23px; font-weight: 700; }
.sub { fill: #9aa7ba; font-size: 15px; }
.metric-label { fill: #7f8da3; font-size: 13px; }
.metric-value { fill: #edf2fb; font-size: 24px; font-weight: 700; }
.status-text { font-size: 14px; font-weight: 700; }
</style>
""",
        f'<rect width="960" height="{height}" fill="#08111f"/>',
        (
            '<rect x="18" y="18" width="924" height="112" rx="20" '
            'fill="#101b2d" stroke="#24334a"/>'
        ),
        '<circle cx="55" cy="55" r="16" fill="#20c997" fill-opacity=".16"/>',
        (
            '<path d="M48 55h14M55 48v14" stroke="#35d6aa" '
            'stroke-width="2.5" stroke-linecap="round"/>'
        ),
        f'<text x="84" y="62" class="title">{html.escape(_truncate(title, 42))}</text>',
        (
            f'<text x="84" y="90" class="muted">已启用 {total_channels} 个渠道 · '
            f'实时读取 Sub2API 渠道监控</text>'
        ),
        (
            f'<rect x="786" y="39" width="116" height="38" rx="19" '
            f'fill="{overall_color}" fill-opacity=".14"/>'
        ),
        f'<circle cx="807" cy="58" r="5" fill="{overall_color}"/>',
        (
            f'<text x="821" y="63" class="status-text" fill="{overall_color}">'
            f"{overall_status}</text>"
        ),
    ]

    summary_x = 84
    for key in ("operational", "degraded", "error", "unknown"):
        count = all_counts.get(key, 0)
        if key == "unknown" and count == 0:
            continue
        label, color = STATUS_META[key]
        parts.extend(
            [
                f'<circle cx="{summary_x}" cy="109" r="4" fill="{color}"/>',
                (
                    f'<text x="{summary_x + 11}" y="114" class="muted">'
                    f"{label} {count}</text>"
                ),
            ]
        )
        summary_x += 92

    for index, snapshot in enumerate(snapshots):
        monitor = snapshot.monitor
        y = header_height + index * (card_height + card_gap)
        status = normalize_status(monitor.get("primary_status"))
        status_label, status_color = STATUS_META[status]
        provider_label, provider_initials, provider_color = _provider_meta(
            monitor.get("provider")
        )
        name = html.escape(_truncate(monitor.get("name") or "未命名渠道", 34))
        model = html.escape(
            _truncate(monitor.get("primary_model") or "未配置主模型", 30)
        )
        group_summary = html.escape(_group_summary(snapshot))
        latency = _number(monitor.get("primary_latency_ms"))
        ping = _latest_ping(snapshot)
        try:
            availability = max(
                0.0,
                min(100.0, float(monitor.get("availability_7d") or 0)),
            )
        except (TypeError, ValueError):
            availability = 0.0
        availability_color = _availability_color(availability)
        extra_models = monitor.get("extra_models")
        extra_count = len(extra_models) if isinstance(extra_models, list) else 0

        parts.extend(
            [
                (
                    f'<rect x="36" y="{y}" width="888" height="{card_height}" '
                    'rx="18" fill="#111c2e" stroke="#26364d"/>'
                ),
                (
                    f'<rect x="62" y="{y + 24}" width="50" height="50" rx="13" '
                    f'fill="{provider_color}" fill-opacity=".14"/>'
                ),
                (
                    f'<text x="87" y="{y + 56}" text-anchor="middle" '
                    f'font-size="15" font-weight="800" fill="{provider_color}">'
                    f"{html.escape(provider_initials)}</text>"
                ),
                f'<text x="130" y="{y + 45}" class="name">{name}</text>',
                (
                    f'<rect x="814" y="{y + 26}" width="82" height="32" rx="16" '
                    f'fill="{status_color}" fill-opacity=".14"/>'
                ),
                f'<circle cx="834" cy="{y + 42}" r="4" fill="{status_color}"/>',
                (
                    f'<text x="847" y="{y + 47}" class="status-text" '
                    f'fill="{status_color}">{status_label}</text>'
                ),
                (
                    f'<rect x="130" y="{y + 57}" width="70" height="24" rx="7" '
                    f'fill="{provider_color}" fill-opacity=".13"/>'
                ),
                (
                    f'<text x="165" y="{y + 74}" text-anchor="middle" font-size="12" '
                    f'font-weight="700" fill="{provider_color}">'
                    f"{html.escape(provider_label)}</text>"
                ),
                f'<text x="212" y="{y + 74}" class="sub">{model}</text>',
                (
                    f'<text x="896" y="{y + 74}" text-anchor="end" '
                    f'font-size="13" fill="#b8c4d6">{group_summary}</text>'
                ),
                (
                    f'<line x1="62" y1="{y + 94}" x2="898" '
                    f'y2="{y + 94}" stroke="#243249"/>'
                ),
                f'<text x="62" y="{y + 120}" class="metric-label">对话延迟</text>',
                (
                    f'<text x="62" y="{y + 151}" class="metric-value">'
                    f'{latency if latency is not None else "--"}'
                    '<tspan font-size="13" fill="#8492a7"> ms</tspan></text>'
                ),
                f'<text x="256" y="{y + 120}" class="metric-label">端点 PING</text>',
                (
                    f'<text x="256" y="{y + 151}" class="metric-value">'
                    f'{ping if ping is not None else "--"}'
                    '<tspan font-size="13" fill="#8492a7"> ms</tspan></text>'
                ),
                f'<text x="596" y="{y + 120}" class="metric-label">7 天可用率</text>',
                (
                    f'<text x="896" y="{y + 151}" text-anchor="end" '
                    f'font-size="34" font-weight="800" fill="{availability_color}">'
                    f"{availability:.2f}<tspan font-size=\"18\">%</tspan></text>"
                ),
            ]
        )
        if extra_count:
            parts.append(
                f'<text x="896" y="{y + 174}" text-anchor="end" '
                f'class="muted">+ {extra_count} 个模型</text>'
            )

        parts.extend(
            [
                (
                    f'<line x1="62" y1="{y + 180}" x2="898" '
                    f'y2="{y + 180}" stroke="#243249"/>'
                ),
                (
                    f'<text x="62" y="{y + 205}" '
                    f'class="metric-label">最近 60 次检测</text>'
                ),
                (
                    f'<text x="898" y="{y + 205}" text-anchor="end" '
                    f'class="metric-label">'
                    f"{html.escape(_age_text(_latest_checked_at(snapshot), now))}"
                    "</text>"
                ),
            ]
        )

        history_states = _history_states(snapshot)
        gap = 3
        bar_area_width = 836
        bar_width = (bar_area_width - gap * (HISTORY_SIZE - 1)) / HISTORY_SIZE
        for bar_index, history_status in enumerate(history_states):
            x = 62 + bar_index * (bar_width + gap)
            color = "#26344a"
            if history_status is not None:
                color = STATUS_META[history_status][1]
            parts.append(
                f'<rect x="{x:.2f}" y="{y + 218}" '
                f'width="{bar_width:.2f}" height="15" rx="3" '
                f'fill="{color}"/>'
            )

    footer_y = height - 31
    generated_cst = now.astimezone(timezone(timedelta(hours=8))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    parts.extend(
        [
            (
                f'<text x="36" y="{footer_y}" class="muted">'
                f"数据来自 Sub2API · {generated_cst}</text>"
            ),
            (
                f'<text x="924" y="{footer_y}" text-anchor="end" class="muted">'
                f"第 {page_number} / {page_count} 页</text>"
            ),
            "</svg>",
        ]
    )
    return "".join(parts)


def format_status_text(
    snapshots: Sequence[ChannelSnapshot],
    *,
    all_counts: dict[str, int],
    total_channels: int,
    page_number: int,
    page_count: int,
    generated_at: datetime | None = None,
    title: str = "Smallice AI · 渠道状态",
) -> str:
    """Format one readable plain-text status page.

    Args:
        snapshots: Monitor snapshots displayed in this message.
        all_counts: Global status counts for every monitor.
        total_channels: Total number of enabled monitors.
        page_number: One-based text page number.
        page_count: Total text page count.
        generated_at: Formatting time.
        title: Header title.

    Returns:
        A platform-friendly status report.
    """
    now = generated_at or datetime.now(timezone.utc)
    lines = [
        title,
        (
            f"渠道 {total_channels} | 正常 {all_counts.get('operational', 0)} | "
            f"降级 {all_counts.get('degraded', 0)} | "
            f"故障 {all_counts.get('error', 0)} | "
            f"未知 {all_counts.get('unknown', 0)}"
        ),
        f"第 {page_number}/{page_count} 页",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    for index, snapshot in enumerate(snapshots):
        monitor = snapshot.monitor
        status = normalize_status(monitor.get("primary_status"))
        status_label = STATUS_META[status][0]
        provider_label = _provider_meta(monitor.get("provider"))[0]
        latency = _number(monitor.get("primary_latency_ms"))
        ping = _latest_ping(snapshot)
        try:
            availability = max(
                0.0,
                min(100.0, float(monitor.get("availability_7d") or 0)),
            )
        except (TypeError, ValueError):
            availability = 0.0
        extra_models = monitor.get("extra_models")
        extra_count = len(extra_models) if isinstance(extra_models, list) else 0
        history = "".join(
            {
                "operational": "O",
                "degraded": "D",
                "error": "X",
                "unknown": "?",
            }.get(state or "", "·")
            for state in _history_states(snapshot)[-30:]
        )

        lines.extend(
            [
                (
                    f"[{status_label}] "
                    f"{_truncate(monitor.get('name') or '未命名渠道', 44)}"
                ),
                (
                    f"{provider_label} · "
                    f"{_truncate(monitor.get('primary_model') or '未配置主模型', 54)}"
                    + (f" · +{extra_count} 个模型" if extra_count else "")
                ),
                f"分组 {_group_summary(snapshot)}",
                (
                    f"对话延迟 {latency if latency is not None else '--'} ms | "
                    f"端点 PING {ping if ping is not None else '--'} ms"
                ),
                (
                    f"7 天可用率 {availability:.2f}% | "
                    f"检测 {_local_time(_latest_checked_at(snapshot))}"
                ),
                f"历史 {history}",
            ]
        )
        if index != len(snapshots) - 1:
            lines.append("────────────────────")

    generated_cst = now.astimezone(timezone(timedelta(hours=8))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    lines.extend(
        [
            "━━━━━━━━━━━━━━━━━━━━",
            "历史图例：O 正常 / D 降级 / X 故障 / ? 未知 / · 无记录",
            f"更新时间：{generated_cst}",
        ]
    )
    return "\n".join(lines)


def svg_html_document(svg: str) -> str:
    """Wrap SVG markup in a deterministic screenshot document.

    Args:
        svg: Complete SVG markup.

    Returns:
        HTML document suitable for AstrBot's HTML renderer.
    """
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<style>html,body{margin:0;padding:0;width:960px;background:#08111f;"
        "overflow:hidden}svg{display:block}</style></head><body>"
        f"{svg}</body></html>"
    )
