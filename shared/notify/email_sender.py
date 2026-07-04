#!/usr/bin/env python3
"""tradingagent email sender with multi-provider fallback."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from html import escape
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib import error, request

from .email_templates import CHANNELS, get_channel

DEFAULT_ENV_FILE = Path(os.environ.get("MARKETGRAPH_ENV_FILE", "/opt/marketgraph/.env"))
FALLBACK_ENV_FILES = (DEFAULT_ENV_FILE, Path("/opt/investment/MarketGraph/deploy/marketgraph_cron.env"))
EMAIL_LOG = Path(__file__).resolve().parent / "logs" / "emails_sent.jsonl"
LOCAL_FALLBACK_DIR = Path(__file__).resolve().parent / "logs" / "email_fallback"
REQUEST_TIMEOUT = 20
RATE_LIMIT_WINDOW_SECONDS = 5 * 60
RATE_LIMIT_STATE: Path | None = None
ALLOWED_ENV_KEYS = {
    "CLOUDFLARE_EMAIL_API_TOKEN",
    "CLOUDFLARE_ACCOUNT_ID",
    "CF_EMAIL_API_TOKEN",
    "CF_EMAIL_ACCOUNT_ID",
}
ENV_ALIASES = {
    "CF_EMAIL_API_TOKEN": "CLOUDFLARE_EMAIL_API_TOKEN",
    "CF_EMAIL_ACCOUNT_ID": "CLOUDFLARE_ACCOUNT_ID",
}
DEFAULT_SUBJECTS = {
    "pre_market_plan": "tradingagent 盘前规划",
    "trading_signal": "tradingagent 交易信号",
    "midday_review": "tradingagent 午盘复盘",
    "closing_plan": "tradingagent 尾盘规划",
    "daily_report": "tradingagent 日报",
    "weekly_report": "tradingagent 周报",
    "trade_receipt": "tradingagent 交易回执",
    "capital_plan": "tradingagent 资金规划",
    "strategy_invalidation": "tradingagent 策略失效通知",
    "emergency_alert": "tradingagent 紧急告警",
    "system_health": "tradingagent 系统健康",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_env_value(raw: str) -> str:
    value = raw.strip()
    if value and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _append_email_log(record: dict[str, Any]) -> None:
    EMAIL_LOG.parent.mkdir(parents=True, exist_ok=True)
    with EMAIL_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _rate_limit_state_path() -> Path:
    return RATE_LIMIT_STATE or (EMAIL_LOG.parent / "email_rate_limit.json")


def _load_rate_limit_state() -> dict[str, str]:
    path = _rate_limit_state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_rate_limit_state(state: dict[str, str]) -> None:
    path = _rate_limit_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _rate_limit_key(
    *,
    to: str,
    subject: str,
    channel: str,
    rate_limit_type: str | None,
) -> str:
    email_type = str(rate_limit_type or subject or "unknown").strip() or "unknown"
    return "|".join([channel or "trading", to.strip().lower(), email_type])


def _check_and_mark_rate_limit(
    *,
    to: str,
    subject: str,
    channel: str,
    rate_limit_type: str | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    now = now or datetime.now(timezone.utc)
    key = _rate_limit_key(to=to, subject=subject, channel=channel, rate_limit_type=rate_limit_type)
    state = _load_rate_limit_state()
    last_at = _parse_iso(str(state.get(key, "")))
    if last_at is not None:
        elapsed = (now - last_at).total_seconds()
        if 0 <= elapsed < RATE_LIMIT_WINDOW_SECONDS:
            next_allowed = last_at.timestamp() + RATE_LIMIT_WINDOW_SECONDS
            return {
                "status": "rate_limited",
                "provider": "rate_limit",
                "to": to,
                "subject": subject,
                "channel": channel,
                "rate_limit_type": rate_limit_type or subject,
                "attempted_at": _now_iso(),
                "next_allowed_at": datetime.fromtimestamp(next_allowed, tz=timezone.utc).isoformat(timespec="seconds"),
            }
    state[key] = now.isoformat(timespec="seconds")
    _save_rate_limit_state(state)
    return None


def _resolve_from_address(channel: str, from_addr: str | None) -> str:
    if from_addr:
        return from_addr
    return CHANNELS.get(channel, CHANNELS["trading"])["from"]


def _html_from_text(body: str) -> str:
    return (
        "<!DOCTYPE html><html><body>"
        f"<pre style=\"font-family: -apple-system, 'PingFang SC', sans-serif; white-space: pre-wrap;\">{escape(body)}</pre>"
        "</body></html>"
    )


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if not body:
                return {"status_code": getattr(resp, "status", 200)}
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                return {"status_code": getattr(resp, "status", 200), "raw": body}
            if isinstance(parsed, dict):
                parsed.setdefault("status_code", getattr(resp, "status", 200))
                return parsed
            return {"status_code": getattr(resp, "status", 200), "raw": parsed}
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"http {exc.code}: {detail or exc.reason}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"url error: {exc.reason}") from exc


def load_env_from_file(env_path: Path | None = None) -> list[str]:
    """Load known email env vars for cron environments that start with a thin env."""
    env_paths = [env_path] if env_path is not None else list(dict.fromkeys(FALLBACK_ENV_FILES))
    loaded: list[str] = []
    for current_path in env_paths:
        if current_path is None or not current_path.exists():
            continue
        for raw_line in current_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key not in ALLOWED_ENV_KEYS or key in os.environ:
                continue
            os.environ[key] = _normalize_env_value(value)
            loaded.append(key)
            alias = ENV_ALIASES.get(key)
            if alias and alias not in os.environ:
                os.environ[alias] = os.environ[key]
                loaded.append(alias)
    for source, alias in ENV_ALIASES.items():
        if source in os.environ and alias not in os.environ:
            os.environ[alias] = os.environ[source]
            loaded.append(alias)
    return loaded


def _send_via_cloudflare(
    to: str,
    subject: str,
    body: str,
    html_body: str,
    from_addr: str,
) -> dict[str, Any]:
    token = os.getenv("CLOUDFLARE_EMAIL_API_TOKEN") or os.getenv("CF_EMAIL_API_TOKEN")
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID") or os.getenv("CF_EMAIL_ACCOUNT_ID")
    if not token or not account_id:
        raise RuntimeError("missing Cloudflare credentials")

    response = _post_json(
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}/email/sending/send",
        {"Authorization": f"Bearer {token}"},
        {
            "from": from_addr,
            "to": [to],
            "subject": subject,
            "text": body,
            "html": html_body,
        },
    )
    if response.get("success") is False:
        raise RuntimeError(str(response.get("errors") or response))
    result = response.get("result") if isinstance(response.get("result"), dict) else {}
    return {
        "provider": "cloudflare",
        "message_id": result.get("id") or response.get("message_id") or f"cf-{uuid.uuid4().hex[:12]}",
        "status_code": response.get("status_code", 200),
    }


def _send_via_deadsimple(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise RuntimeError("DeadSimple fallback removed; Cloudflare email routing is the only API sender")


def _send_via_smtp(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise RuntimeError("SMTP fallback removed; local file save is the only fallback")


def _save_local_email(
    to: str,
    subject: str,
    body: str,
    html_body: str,
    from_addr: str,
    errors: list[str],
) -> dict[str, Any]:
    LOCAL_FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
    path = LOCAL_FALLBACK_DIR / f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.json"
    payload = {
        "saved_at": _now_iso(),
        "to": to,
        "from": from_addr,
        "subject": subject,
        "body": body,
        "html_body": html_body,
        "errors": errors,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "provider": "local_file",
        "saved_to": str(path),
    }


def send_email(
    to: str,
    subject: str,
    body: str,
    html_body: str | None = None,
    *,
    channel: str = "trading",
    from_addr: str | None = None,
    rate_limit_type: str | None = None,
) -> dict[str, Any]:
    """Send email through Cloudflare, then save locally if Cloudflare is unavailable."""
    loaded_env = load_env_from_file()
    resolved_from = _resolve_from_address(channel, from_addr)
    rendered_html = html_body or _html_from_text(body)
    errors_seen: list[str] = []
    limited = _check_and_mark_rate_limit(
        to=to,
        subject=subject,
        channel=channel,
        rate_limit_type=rate_limit_type,
    )
    if limited is not None:
        limited["from"] = resolved_from
        limited["loaded_env_keys"] = loaded_env
        _append_email_log(limited)
        return limited
    try:
        dispatch = _send_via_cloudflare(to, subject, body, rendered_html, resolved_from)
        result = {
            "status": "sent",
            "provider": dispatch.get("provider", "cloudflare"),
            "message_id": dispatch.get("message_id", ""),
            "status_code": dispatch.get("status_code"),
            "to": to,
            "from": resolved_from,
            "subject": subject,
            "channel": channel,
            "rate_limit_type": rate_limit_type or subject,
            "attempted_at": _now_iso(),
            "loaded_env_keys": loaded_env,
        }
        _append_email_log(result)
        return result
    except Exception as exc:
        errors_seen.append(f"cloudflare: {exc}")
        errors_seen.append("deadsimple: removed from delivery chain")
        errors_seen.append("smtp: removed from delivery chain")

    saved = _save_local_email(to, subject, body, rendered_html, resolved_from, errors_seen)
    result = {
        "status": "saved_local",
        "provider": saved["provider"],
        "saved_to": saved["saved_to"],
        "to": to,
        "from": resolved_from,
        "subject": subject,
        "channel": channel,
        "rate_limit_type": rate_limit_type or subject,
        "attempted_at": _now_iso(),
        "loaded_env_keys": loaded_env,
        "errors": errors_seen,
    }
    _append_email_log(result)
    return result


def _channel_key_for_template(template_name: str) -> str:
    template_channel = get_channel(template_name)
    template_from = template_channel.get("from")
    for key, config in CHANNELS.items():
        if config.get("from") == template_from:
            return key
    return "trading"


def _default_subject(template_name: str, data: dict[str, Any]) -> str:
    suffix = str(data.get("date") or data.get("trade_date") or data.get("week_range") or "").strip()
    base = DEFAULT_SUBJECTS.get(template_name, f"tradingagent {template_name}")
    return f"{base} {suffix}".strip()


def render_template_html(template_name: str, data: dict[str, Any]) -> str:
    module = import_module(f"{__package__}.email_templates.{template_name}")
    render = getattr(module, "render")
    return str(render(data))


def send_template_email(
    template_name: str,
    data: dict[str, Any],
    *,
    to: str | None = None,
    subject: str | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    html_body = render_template_html(template_name, data)
    channel = channel or _channel_key_for_template(template_name)
    recipient = to or CHANNELS.get(channel, get_channel(template_name))["to"]
    resolved_subject = subject or _default_subject(template_name, data)
    plain_body = str(data.get("summary") or f"{resolved_subject}\n请查看 HTML 邮件内容。")
    try:
        return send_email(recipient, resolved_subject, plain_body, html_body, channel=channel, rate_limit_type=template_name)
    except TypeError as exc:
        if "rate_limit_type" not in str(exc):
            raise
        return send_email(recipient, resolved_subject, plain_body, html_body, channel=channel)
