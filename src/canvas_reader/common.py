"""Helpers shared by the Canvas Reader tools: links, local times, Canvas errors."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "America/Los_Angeles"

AUTH_ERROR_MESSAGE = (
    "Canvas rejected the API token; it may have expired or been revoked. "
    "Create a new token in Canvas (Account > Settings > New access token), "
    "put it in the credential file, and restart Canvas Reader."
)

_HTTP_STATUS = re.compile(r"HTTP error:\s*(\d{3})")
_ERROR_KINDS = {401: "auth", 403: "forbidden", 404: "not_found", 429: "rate_limited"}


def timezone_name() -> str:
    return os.environ.get("TIMEZONE") or DEFAULT_TIMEZONE


def output_timezone() -> ZoneInfo:
    """The configured display timezone (validated at startup)."""
    try:
        return ZoneInfo(timezone_name())
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def parse_canvas_time(value: Any) -> datetime | None:
    """Parse a Canvas ISO 8601 timestamp; naive values are treated as UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def local_iso(value: Any, tz: ZoneInfo) -> str | None:
    """ISO 8601 in the display timezone, e.g. 2026-10-07T23:59:59-07:00."""
    parsed = parse_canvas_time(value)
    return parsed.astimezone(tz).isoformat(timespec="seconds") if parsed else None


def local_display(value: Any, tz: ZoneInfo) -> str | None:
    """Reader-ready local time with weekday and zone, e.g. Tue, Oct 7, 2026, 11:59 PM PDT."""
    parsed = parse_canvas_time(value)
    if not parsed:
        return None
    local = parsed.astimezone(tz)
    hour = local.hour % 12 or 12
    return (f"{local:%a}, {local:%b} {local.day}, {local.year}, "
            f"{hour}:{local:%M} {local:%p} {local.tzname()}")


def canvas_web_root(api_url: str) -> tuple[str, str, str]:
    """Scheme, host and any institution path prefix of the Canvas web interface."""
    parsed = urlsplit(api_url)
    prefix = re.sub(r"/api/v\d+/?$", "", parsed.path.rstrip("/"))
    return parsed.scheme, parsed.netloc, prefix


def canvas_link(api_url: str, path: str, supplied: Any = None) -> str:
    """A Canvas web link with no query string or fragment.

    A link Canvas supplied (html_url) is kept when it uses HTTPS on the
    configured Canvas host; otherwise the link is built from ``path``. Query
    strings are always dropped because they can carry verifiers or tokens.
    """
    scheme, netloc, prefix = canvas_web_root(api_url)
    if isinstance(supplied, str):
        try:
            candidate = urlsplit(supplied)
        except ValueError:
            candidate = None
        if (candidate and candidate.scheme == "https" and candidate.netloc == netloc
                and not candidate.username and candidate.path.startswith("/")):
            return urlunsplit((candidate.scheme, candidate.netloc, candidate.path, "", ""))
    return urlunsplit((scheme, netloc, f"{prefix}{path}", "", ""))


def canvas_error_summary(response: Any) -> str:
    """A short, detail-free description of a canvas-mcp failure, e.g. "Canvas returned HTTP 503"."""
    text = str(response.get("error")) if isinstance(response, dict) else ""
    match = _HTTP_STATUS.search(text)
    if match:
        return f"Canvas returned HTTP {match.group(1)}"
    if text.startswith("Request failed"):
        return "the connection to Canvas failed"
    return "Canvas returned an unexpected response"


def canvas_error_kind(response: Any) -> str | None:
    """None for a usable response, else auth, forbidden, not_found, rate_limited or other.

    canvas-mcp reports failures as ``{"error": "HTTP error: 401, Details: ..."}``.
    The details are never returned to the caller; only the status class is used.
    """
    if isinstance(response, dict) and "error" in response:
        match = _HTTP_STATUS.search(str(response.get("error")))
        return _ERROR_KINDS.get(int(match.group(1)), "other") if match else "other"
    return None
