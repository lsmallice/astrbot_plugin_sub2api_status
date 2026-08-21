from __future__ import annotations

import re
from collections.abc import Iterable

from astrbot.api.message_components import At, Plain

_EMAIL_RE = re.compile(r"^[^@]{1,80}@[^@]{1,190}$")
_CLAIM_PREFIXES = {"领取", "赠送", "claim", "gift"}
_ACCOUNT_PREFIXES = {
    "余额",
    "账户",
    "账号",
    "我的余额",
    "我的账户",
    "领取状态",
    "claim-status",
    "account",
}
_COMMAND_PREFIXES = _CLAIM_PREFIXES | _ACCOUNT_PREFIXES


def parse_group_ids(value: object) -> set[str]:
    """Parse the comma/newline separated group allowlist."""
    return {
        item.strip() for item in re.split(r"[,，\s]+", str(value or "")) if item.strip()
    }


def is_bot_mentioned(components: Iterable[object], self_id: object) -> bool:
    expected = str(self_id or "").strip()
    return bool(expected) and any(
        isinstance(component, At) and str(component.qq).strip() == expected
        for component in components
    )


def is_claim_request(components: Iterable[object], fallback: str = "") -> bool:
    """Return whether a message asks the bot to perform the group claim."""
    plain_text = " ".join(
        component.text.strip()
        for component in components
        if isinstance(component, Plain) and component.text.strip()
    )
    text = plain_text or fallback
    text = re.sub(r"\[At:?[^\]]*\]", " ", text, flags=re.IGNORECASE)
    tokens = [token.strip(" \t\r\n,，。.!！:：") for token in text.split()]
    tokens = [token for token in tokens if token]
    return not tokens or len(tokens) == 1 and tokens[0].casefold() in _CLAIM_PREFIXES


def is_account_request(components: Iterable[object], fallback: str = "") -> bool:
    """Return whether a mention asks for the sender's own account details."""
    plain_text = " ".join(
        component.text.strip()
        for component in components
        if isinstance(component, Plain) and component.text.strip()
    )
    text = plain_text or fallback
    text = re.sub(r"\[At:?[^\]]*\]", " ", text, flags=re.IGNORECASE)
    tokens = [token.strip(" \t\r\n,，。.!！:：") for token in text.split()]
    tokens = [token for token in tokens if token]
    return len(tokens) == 1 and tokens[0].casefold() in _ACCOUNT_PREFIXES


def parse_binding_confirmation_code(value: object) -> str | None:
    """Extract the six-digit code used to finish a browser binding."""
    text = str(value or "").strip()
    if re.fullmatch(r"\d{6}", text):
        return text
    match = re.fullmatch(r"/?(?:绑定确认|bind-confirm)\s+(\d{6})", text, re.IGNORECASE)
    return match.group(1) if match else None


def parse_account_identifier(
    components: Iterable[object], fallback: str = ""
) -> str | None:
    """Extract one email/username from the text after the bot mention."""
    plain_text = " ".join(
        component.text.strip()
        for component in components
        if isinstance(component, Plain) and component.text.strip()
    )
    text = plain_text or fallback
    text = re.sub(r"\[At:?[^\]]*\]", " ", text, flags=re.IGNORECASE)
    tokens = [token.strip(" \t\r\n,，。.!！:：") for token in text.split()]
    tokens = [token for token in tokens if token]
    while tokens and tokens[0].casefold() in _COMMAND_PREFIXES:
        tokens.pop(0)
    if len(tokens) != 1:
        return None

    identifier = tokens[0]
    if len(identifier) > 190 or identifier.startswith("/"):
        return None
    if _EMAIL_RE.fullmatch(identifier):
        return identifier
    if any(char.isspace() for char in identifier):
        return None
    if not re.fullmatch(r"[\w.\-\u4e00-\u9fff]{2,64}", identifier, re.UNICODE):
        return None
    return identifier
