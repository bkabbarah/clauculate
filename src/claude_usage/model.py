"""Dynamic interpretation of /api/oauth/usage.

Nothing here is keyed to a known window name. Every top-level key the endpoint
returns is classified by SHAPE, so a window key that ships in a future release
renders with no code change.

Observed shape (Claude Code 2.1.239, Max plan, 2026-08-21) -- for reference
only, never relied upon:

  <window key>: {utilization, resets_at, limit_dollars, used_dollars,
                 remaining_dollars} | null
  limits: [{kind, group, percent, severity, resets_at, scope, is_active}]
  extra_usage: {is_enabled, monthly_limit, used_credits, utilization, ...}
  spend: {used: {amount_minor, currency, exponent}, limit, percent, ...}
  member_dashboard_available: bool
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def parse_iso8601(value: Any) -> datetime | None:
    """Parse the endpoint UTC timestamps into aware datetimes."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_local(value: datetime | None) -> datetime | None:
    """UTC -> local wall clock. astimezone() applies the OS DST rules."""
    if value is None:
        return None
    return value.astimezone()


@dataclass
class WindowRow:
    """A rate-limit window. The key is whatever the endpoint called it."""

    key: str
    utilization: float | None
    resets_at: datetime | None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def headroom(self) -> float | None:
        if self.utilization is None:
            return None
        return max(0.0, 100.0 - self.utilization)


@dataclass
class LimitRow:
    """An entry from the limits array -- richer than the flat window keys."""

    kind: str | None
    group: str | None
    percent: float | None
    severity: str | None
    resets_at: datetime | None
    scope_label: str | None
    is_active: bool
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        base = self.kind or "limit"
        if self.scope_label:
            return base + " [" + self.scope_label + "]"
        return base


@dataclass
class GenericBlock:
    """Any other structured key, rendered as flat label/value pairs."""

    key: str
    fields: list[tuple[str, Any]]


@dataclass
class UsageSnapshot:
    windows: list[WindowRow]
    limits: list[LimitRow]
    blocks: list[GenericBlock]
    scalars: list[tuple[str, Any]]
    null_keys: list[str]
    raw: dict[str, Any]
    fetched_at: datetime

    def window(self, key: str) -> WindowRow | None:
        for w in self.windows:
            if w.key == key:
                return w
        return None

    @property
    def primary_window(self) -> WindowRow | None:
        """The 5-hour session window, if the endpoint still calls it that."""
        return self.window("five_hour")

    @property
    def worst_utilization(self) -> float | None:
        """Highest utilization across every window AND every limits entry."""
        values = [w.utilization for w in self.windows if w.utilization is not None]
        values += [x.percent for x in self.limits if x.percent is not None]
        return max(values) if values else None

    def headline_metrics(self) -> list[tuple[str, float | None, datetime | None]]:
        """The few numbers worth showing on a collapsed row.

        Session and weekly come from the flat windows when present, falling
        back to the equivalent limits[] entries. The third slot is whichever
        model-scoped cap is worst, which on Max is normally Fable.
        """
        out: list[tuple[str, float | None, datetime | None]] = []

        def from_limit(kind: str):
            for row in self.limits:
                if row.kind == kind and not row.scope_label:
                    return row
            return None

        session = self.window("five_hour") or from_limit("session")
        if session is not None:
            pct = getattr(session, "utilization", None)
            if pct is None:
                pct = getattr(session, "percent", None)
            out.append(("5h", pct, session.resets_at))

        weekly = self.window("seven_day") or from_limit("weekly_all")
        if weekly is not None:
            pct = getattr(weekly, "utilization", None)
            if pct is None:
                pct = getattr(weekly, "percent", None)
            out.append(("week", pct, weekly.resets_at))

        scoped = [r for r in self.limits if r.scope_label and r.percent is not None]
        if scoped:
            worst = max(scoped, key=lambda r: r.percent)
            out.append((worst.scope_label, worst.percent, worst.resets_at))

        return out

    @property
    def scoped_model_names(self) -> list[str]:
        """Model names the limits array reports scoped caps for (e.g. Fable)."""
        names: list[str] = []
        for row in self.limits:
            if row.scope_label and row.scope_label not in names:
                names.append(row.scope_label)
        return names


def _flatten(prefix: str, value: Any, out: list[tuple[str, Any]]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(prefix + "." + k if prefix else k, v, out)
    elif isinstance(value, list):
        if not value:
            out.append((prefix, "[]"))
        for i, item in enumerate(value):
            _flatten(prefix + "[" + str(i) + "]", item, out)
    else:
        out.append((prefix, value))


def _scope_label(scope: Any) -> str | None:
    """Pull a human label out of a limits scope object, shape-agnostically."""
    if not isinstance(scope, dict):
        return None
    parts: list[str] = []
    model = scope.get("model")
    if isinstance(model, dict):
        name = model.get("display_name") or model.get("id")
        if name:
            parts.append(str(name))
    surface = scope.get("surface")
    if isinstance(surface, dict):
        name = surface.get("display_name") or surface.get("id")
        if name:
            parts.append(str(name))
    elif isinstance(surface, str) and surface:
        parts.append(surface)
    # Any other scope dimension we have never seen still shows up.
    for k, v in scope.items():
        if k in ("model", "surface") or v is None:
            continue
        parts.append(str(k) + "=" + str(v))
    return " / ".join(parts) if parts else None


def _is_window(value: Any) -> bool:
    """A rate-limit window carries both a utilization and a reset clock.

    Checking for both matters: extra_usage also has a 'utilization' key but no
    'resets_at', and it belongs in its own block rather than the window list.
    """
    return isinstance(value, dict) and "utilization" in value and "resets_at" in value


def _looks_like_limits(value: list) -> bool:
    return bool(value) and any(
        isinstance(i, dict) and ("percent" in i or "kind" in i) for i in value
    )


def parse_usage(raw: Any, fetched_at: datetime | None = None) -> UsageSnapshot:
    """Classify every top-level key by shape. Unknown keys are still rendered."""
    fetched_at = fetched_at or datetime.now(timezone.utc)

    if not isinstance(raw, dict):
        # Endpoint returned something we cannot walk at all. Preserve it.
        return UsageSnapshot(
            [], [], [], [("response", raw)], [], {"unparsed": raw}, fetched_at
        )

    windows: list[WindowRow] = []
    limits: list[LimitRow] = []
    blocks: list[GenericBlock] = []
    scalars: list[tuple[str, Any]] = []
    null_keys: list[str] = []

    for key, value in raw.items():
        if value is None:
            null_keys.append(key)
            continue

        if _is_window(value):
            util = value.get("utilization")
            windows.append(
                WindowRow(
                    key=key,
                    utilization=float(util) if isinstance(util, (int, float)) else None,
                    resets_at=parse_iso8601(value.get("resets_at")),
                    extras={
                        k: v
                        for k, v in value.items()
                        if k not in ("utilization", "resets_at") and v is not None
                    },
                )
            )
            continue

        if isinstance(value, list) and _looks_like_limits(value):
            # Recognised structurally, not by key name.
            for item in value:
                if not isinstance(item, dict):
                    continue
                pct = item.get("percent")
                limits.append(
                    LimitRow(
                        kind=item.get("kind"),
                        group=item.get("group"),
                        percent=float(pct) if isinstance(pct, (int, float)) else None,
                        severity=item.get("severity"),
                        resets_at=parse_iso8601(item.get("resets_at")),
                        scope_label=_scope_label(item.get("scope")),
                        is_active=bool(item.get("is_active")),
                        raw=item,
                    )
                )
            continue

        if isinstance(value, (dict, list)):
            flat: list[tuple[str, Any]] = []
            _flatten("", value, flat)
            blocks.append(GenericBlock(key=key, fields=flat))
            continue

        scalars.append((key, value))

    return UsageSnapshot(
        windows=windows,
        limits=limits,
        blocks=blocks,
        scalars=scalars,
        null_keys=null_keys,
        raw=raw,
        fetched_at=fetched_at,
    )
