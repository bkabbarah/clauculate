"""Presentation helpers shared by the tray and the panel."""

from __future__ import annotations

from datetime import datetime, timezone

# Utilization thresholds driving every colour decision in the app.
GREEN_MAX = 50.0
AMBER_MAX = 80.0

COLOR_GREEN = "#2e9e4f"
COLOR_AMBER = "#d99100"
COLOR_RED = "#cc3333"
COLOR_GREY = "#8a8a8a"


def color_for(utilization: float | None) -> str:
    if utilization is None:
        return COLOR_GREY
    if utilization < GREEN_MAX:
        return COLOR_GREEN
    if utilization <= AMBER_MAX:
        return COLOR_AMBER
    return COLOR_RED


def format_percent(value: float | None) -> str:
    if value is None:
        return "--"
    if float(value).is_integer():
        return "%d%%" % int(value)
    return "%.1f%%" % value


def format_duration(seconds: float | None) -> str:
    """Compact duration: '2h 14m', '45m', '30s'."""
    if seconds is None:
        return "--"
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%ds" % seconds
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return "%dd %dh" % (days, hours)
    if hours:
        return "%dh %dm" % (hours, minutes)
    return "%dm" % minutes


def format_age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 60:
        return "%ds ago" % int(seconds)
    return format_duration(seconds) + " ago"


def format_reset_relative(resets_at: datetime | None, now: datetime | None = None) -> str:
    if resets_at is None:
        return "no reset time"
    now = now or datetime.now(timezone.utc)
    delta = (resets_at - now).total_seconds()
    if delta <= 0:
        return "reset due"
    return "resets in " + format_duration(delta)


def format_reset_absolute(resets_at: datetime | None) -> str:
    """Local wall-clock time, with a day qualifier so it is never ambiguous."""
    if resets_at is None:
        return "--"
    local = resets_at.astimezone()
    today = datetime.now().astimezone().date()
    delta_days = (local.date() - today).days
    clock = local.strftime("%I:%M %p").lstrip("0")
    if delta_days == 0:
        return "today " + clock
    if delta_days == 1:
        return "tomorrow " + clock
    if delta_days == -1:
        return "yesterday " + clock
    return local.strftime("%b %d ") + clock


def format_value(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
