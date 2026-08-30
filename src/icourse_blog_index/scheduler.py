"""Pure scheduling rules for bootstrap boundaries and maintenance revisits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from .models import BlogStatus, CheckResult, ProfileStatus, UserRecord
from .utils import UTC, parse_utc, utc_now


@dataclass(frozen=True, slots=True)
class SchedulePolicy:
    """Maintenance intervals approved by the repository design."""

    blog_present_days: int = 30
    blog_absent_days: int = 90
    hidden_days: int = 180
    missing_days: int = 180
    unknown_days: int = 1
    pending_hours: int = 24
    retry_base_hours: int = 24
    retry_max_hours: int = 72
    recent_change_days: int = 7
    boundary_missing_count: int = 256

    def __post_init__(self) -> None:
        for name in (
            "blog_present_days",
            "blog_absent_days",
            "hidden_days",
            "missing_days",
            "unknown_days",
            "pending_hours",
            "retry_base_hours",
            "retry_max_hours",
            "recent_change_days",
            "boundary_missing_count",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.retry_max_hours < self.retry_base_hours:
            raise ValueError("retry_max_hours cannot be below retry_base_hours")


DEFAULT_POLICY = SchedulePolicy()
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _instant(value: str | None, fallback: datetime = _EPOCH) -> datetime:
    return fallback if value is None else parse_utc(value)


def _confirmed_interval(record: UserRecord, policy: SchedulePolicy) -> timedelta:
    if record.profile_status is ProfileStatus.PUBLIC:
        if record.blog_status is BlogStatus.PRESENT:
            return timedelta(days=policy.blog_present_days)
        if record.blog_status is BlogStatus.ABSENT:
            return timedelta(days=policy.blog_absent_days)
        return timedelta(days=policy.unknown_days)
    if record.profile_status is ProfileStatus.HIDDEN:
        return timedelta(days=policy.hidden_days)
    if record.profile_status is ProfileStatus.MISSING:
        return timedelta(days=policy.missing_days)
    return timedelta(days=policy.unknown_days)


def due_at(record: UserRecord, policy: SchedulePolicy = DEFAULT_POLICY) -> datetime:
    """Return the next eligible attempt time for a canonical record."""

    if record.last_checked_at is None:
        return _EPOCH
    checked = parse_utc(record.last_checked_at)
    if record.pending_observation is not None:
        return checked + timedelta(hours=policy.pending_hours)
    if record.last_check_result is not CheckResult.OK:
        exponent = max(0, record.consecutive_failures - 1)
        retry_hours = min(policy.retry_base_hours * (2**exponent), policy.retry_max_hours)
        return checked + timedelta(hours=retry_hours)

    confirmed = _instant(record.last_confirmed_at, checked)
    regular_due = confirmed + _confirmed_interval(record, policy)
    first_checked = _instant(record.first_checked_at)
    change_times = [
        parse_utc(value)
        for value in (record.profile_changed_at, record.blog_changed_at)
        # Baseline timestamps document when the initial state was established;
        # they are not later changes and must not enqueue the whole bootstrap
        # dataset for a seven-day revisit.
        if value is not None and parse_utc(value) > first_checked
    ]
    if change_times:
        changed_due = max(change_times) + timedelta(days=policy.recent_change_days)
        # Only the first post-change revisit is accelerated.  Once a successful
        # confirmation occurred after the change, the normal interval applies.
        if confirmed <= max(change_times):
            return min(regular_due, changed_due)
    return regular_due


def is_due(
    record: UserRecord,
    now: datetime | str | None = None,
    policy: SchedulePolicy = DEFAULT_POLICY,
) -> bool:
    instant = utc_now() if now is None else parse_utc(now)
    return due_at(record, policy) <= instant


def _priority(record: UserRecord, policy: SchedulePolicy) -> tuple[int, datetime, int]:
    if record.pending_observation is not None:
        category = 0
    elif record.last_check_result is not CheckResult.OK:
        category = 1
    elif record.profile_status is ProfileStatus.UNKNOWN:
        category = 2
    elif record.blog_status is BlogStatus.PRESENT:
        category = 3
    elif record.profile_status is ProfileStatus.PUBLIC:
        category = 4
    elif record.profile_status is ProfileStatus.HIDDEN:
        category = 5
    else:
        category = 6
    return category, due_at(record, policy), record.id


def select_due_users(
    records: Iterable[UserRecord],
    *,
    now: datetime | str | None = None,
    limit: int = 500,
    policy: SchedulePolicy = DEFAULT_POLICY,
) -> list[UserRecord]:
    """Select a stable, fair maintenance batch.

    Pending confirmations and retryable errors precede ordinary age-based
    refreshes; ties are deterministic by due time and numeric ID.
    """

    if limit < 0:
        raise ValueError("limit cannot be negative")
    instant = utc_now() if now is None else parse_utc(now)
    due = [record for record in records if due_at(record, policy) <= instant]
    return sorted(due, key=lambda record: _priority(record, policy))[:limit]


def highest_confirmed_user_id(records: Iterable[UserRecord]) -> int:
    """Return the highest confirmed *existing* (public or hidden) user ID."""

    return max((record.id for record in records if record.is_confirmed_existing_user), default=0)


def frontier_probe_start(records: Iterable[UserRecord]) -> int:
    """Start new-user probing immediately after the highest existing user.

    It deliberately ignores the highest previously attempted missing ID.  A
    later registration can therefore fill a formerly missing frontier ID and is
    never skipped permanently.
    """

    return highest_confirmed_user_id(records) + 1


def trailing_missing_count(
    records: Iterable[UserRecord],
    *,
    after_id: int | None = None,
) -> int:
    """Count consecutive confirmed missing IDs after the current frontier."""

    materialized = {record.id: record for record in records}
    frontier = (
        max(
            (record.id for record in materialized.values() if record.is_confirmed_existing_user),
            default=0,
        )
        if after_id is None
        else after_id
    )
    count = 0
    user_id = frontier + 1
    while True:
        record = materialized.get(user_id)
        if (
            record is None
            or not record.has_confirmed_state
            or record.profile_status is not ProfileStatus.MISSING
        ):
            break
        count += 1
        user_id += 1
    return count


def boundary_reached(
    records: Iterable[UserRecord],
    *,
    threshold: int | None = None,
    policy: SchedulePolicy = DEFAULT_POLICY,
) -> bool:
    needed = policy.boundary_missing_count if threshold is None else threshold
    if needed <= 0:
        raise ValueError("threshold must be positive")
    return trailing_missing_count(records) >= needed


__all__ = [
    "DEFAULT_POLICY",
    "SchedulePolicy",
    "boundary_reached",
    "due_at",
    "frontier_probe_start",
    "highest_confirmed_user_id",
    "is_due",
    "select_due_users",
    "trailing_missing_count",
]
