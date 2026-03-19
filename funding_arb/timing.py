"""Timing utilities for funding rate arb — funding timestamps at 00:00, 08:00, 16:00 UTC."""

from datetime import datetime, timezone, timedelta


FUNDING_HOURS = [0, 8, 16]  # UTC hours when funding is applied


def next_funding_timestamp() -> datetime:
    """Get the next funding settlement timestamp."""
    now = datetime.now(timezone.utc)
    today = now.replace(minute=0, second=0, microsecond=0)

    for h in FUNDING_HOURS:
        candidate = today.replace(hour=h)
        if candidate > now:
            return candidate

    # Next day's first funding
    tomorrow = today + timedelta(days=1)
    return tomorrow.replace(hour=FUNDING_HOURS[0])


def prev_funding_timestamp() -> datetime:
    """Get the most recent funding settlement timestamp."""
    now = datetime.now(timezone.utc)
    today = now.replace(minute=0, second=0, microsecond=0)

    for h in reversed(FUNDING_HOURS):
        candidate = today.replace(hour=h)
        if candidate <= now:
            return candidate

    # Yesterday's last funding
    yesterday = today - timedelta(days=1)
    return yesterday.replace(hour=FUNDING_HOURS[-1])


def minutes_until_next_funding() -> float:
    """Minutes until the next funding settlement."""
    delta = next_funding_timestamp() - datetime.now(timezone.utc)
    return delta.total_seconds() / 60


def minutes_since_last_funding() -> float:
    """Minutes since the last funding settlement."""
    delta = datetime.now(timezone.utc) - prev_funding_timestamp()
    return delta.total_seconds() / 60


def just_passed_funding(within_minutes: float = 6) -> bool:
    """Check if a funding timestamp just occurred (within N minutes)."""
    return minutes_since_last_funding() <= within_minutes


def in_entry_window(window_minutes: int = 120) -> bool:
    """Check if we're within the entry window before next funding."""
    return minutes_until_next_funding() <= window_minutes


def funding_info() -> dict:
    """Summary of timing info."""
    return {
        "next_funding": next_funding_timestamp().isoformat(),
        "minutes_until": round(minutes_until_next_funding(), 1),
        "minutes_since_last": round(minutes_since_last_funding(), 1),
        "in_entry_window": in_entry_window(),
        "just_passed": just_passed_funding(),
    }
