from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest

from icourse_blog_index.models import PendingObservation, UserRecord
from icourse_blog_index.scheduler import (
    SchedulePolicy,
    boundary_reached,
    due_at,
    frontier_probe_start,
    is_due,
    select_due_users,
    trailing_missing_count,
)


UTC = timezone.utc


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"profile_status": "public", "blog_status": "present"}, "2026-08-31T00:00:00Z"),
        ({"profile_status": "public", "blog_status": "absent"}, "2026-09-02T00:00:00Z"),
        ({"profile_status": "hidden", "blog_status": "unknown"}, "2026-09-05T00:00:00Z"),
        ({"profile_status": "missing", "blog_status": "unknown"}, "2026-09-05T00:00:00Z"),
        ({"profile_status": "unknown", "blog_status": "unknown"}, "2026-08-31T00:00:00Z"),
    ],
)
def test_due_at_uses_status_specific_interval(
    record_factory: Callable[..., UserRecord],
    overrides: dict[str, object],
    expected: str,
) -> None:
    policy = SchedulePolicy(
        blog_present_days=1,
        blog_absent_days=3,
        hidden_days=6,
        missing_days=6,
        unknown_days=1,
    )
    record = record_factory(
        last_confirmed_at="2026-08-30T00:00:00Z",
        last_checked_at="2026-08-30T00:00:00Z",
        profile_changed_at=None,
        blog_changed_at=None,
        blog_url=None if overrides["blog_status"] != "present" else "https://example.com",
        **overrides,
    )

    assert due_at(record, policy).isoformat() == expected.replace("Z", "+00:00")


@pytest.mark.parametrize(
    ("failures", "hours"),
    [(1, 24), (2, 48), (3, 72), (9, 72)],
)
def test_failed_attempts_use_capped_exponential_retry(
    record_factory: Callable[..., UserRecord], failures: int, hours: int
) -> None:
    record = record_factory(
        last_checked_at="2026-08-30T00:00:00Z",
        last_check_result="timeout",
        consecutive_failures=failures,
    )

    assert due_at(record) == datetime(2026, 8, 30, tzinfo=UTC) + timedelta(hours=hours)


def test_pending_confirmation_is_due_after_24_hours(
    record_factory: Callable[..., UserRecord],
) -> None:
    pending = PendingObservation(
        profile_status="public",
        blog_status="present",
        blog_url="https://candidate.example",
        blog_url_raw="https://candidate.example",
        first_observed_at="2026-08-30T00:00:00Z",
        last_observed_at="2026-08-30T00:00:00Z",
        confirmations=1,
        parser_version="test",
        source_fingerprint="candidate",
    )
    record = record_factory(
        last_checked_at="2026-08-30T00:00:00Z",
        pending_observation=pending,
    )

    assert due_at(record) == datetime(2026, 8, 31, tzinfo=UTC)
    assert not is_due(record, "2026-08-30T23:59:59Z")
    assert is_due(record, "2026-08-31T00:00:00Z")


def test_recent_unreconfirmed_change_accelerates_revisit(
    record_factory: Callable[..., UserRecord],
) -> None:
    changed = record_factory(
        last_confirmed_at="2026-08-30T00:00:00Z",
        blog_changed_at="2026-08-30T00:00:00Z",
        profile_changed_at=None,
    )
    already_reconfirmed = record_factory(
        last_confirmed_at="2026-08-31T00:00:00Z",
        blog_changed_at="2026-08-30T00:00:00Z",
        profile_changed_at=None,
    )

    assert due_at(changed) == datetime(2026, 9, 6, tzinfo=UTC)
    assert due_at(already_reconfirmed) == datetime(2026, 9, 30, tzinfo=UTC)


def test_select_due_users_prioritizes_pending_then_errors_then_unknown(
    record_factory: Callable[..., UserRecord],
) -> None:
    pending = PendingObservation(
        profile_status="public",
        blog_status="absent",
        blog_url=None,
        blog_url_raw=None,
        first_observed_at="2026-01-01T00:00:00Z",
        last_observed_at="2026-01-01T00:00:00Z",
        confirmations=1,
        parser_version="test",
        source_fingerprint="pending",
    )
    records = [
        record_factory(id=6, profile_status="missing", blog_status="unknown", blog_url=None),
        record_factory(id=5, profile_status="public", blog_status="absent", blog_url=None),
        record_factory(id=4),
        record_factory(id=3, profile_status="unknown", blog_status="unknown", blog_url=None),
        record_factory(id=2, last_check_result="network_error", consecutive_failures=1),
        record_factory(id=1, pending_observation=pending),
    ]

    selected = select_due_users(records, now="2027-08-30T00:00:00Z", limit=4)
    assert [record.id for record in selected] == [1, 2, 3, 4]


def _frontier_record(
    record_factory: Callable[..., UserRecord], user_id: int, status: str
) -> UserRecord:
    return record_factory(
        id=user_id,
        profile_status=status,
        blog_status="absent" if status == "public" else "unknown",
        blog_url=None,
        last_confirmed_at="2026-08-30T00:00:00Z",
    )


def test_frontier_ignores_missing_ids_and_honors_hidden_existing_user(
    record_factory: Callable[..., UserRecord],
) -> None:
    records = [
        _frontier_record(record_factory, 10, "public"),
        _frontier_record(record_factory, 11, "missing"),
        _frontier_record(record_factory, 12, "missing"),
        _frontier_record(record_factory, 13, "hidden"),
        _frontier_record(record_factory, 14, "missing"),
    ]

    assert frontier_probe_start(records) == 14
    assert trailing_missing_count(records) == 1


def test_boundary_requires_consecutive_confirmed_missing_ids(
    record_factory: Callable[..., UserRecord],
) -> None:
    records = [_frontier_record(record_factory, 100, "public")]
    records.extend(
        _frontier_record(record_factory, user_id, "missing") for user_id in range(101, 105)
    )

    assert trailing_missing_count(records) == 4
    assert boundary_reached(records, threshold=4)
    assert not boundary_reached(records, threshold=5)

    gap = [record for record in records if record.id != 103]
    assert trailing_missing_count(gap) == 2
    assert not boundary_reached(gap, threshold=4)


def test_boundary_unknown_or_unconfirmed_record_breaks_run(
    record_factory: Callable[..., UserRecord],
) -> None:
    records = [
        _frontier_record(record_factory, 20, "public"),
        _frontier_record(record_factory, 21, "missing"),
        record_factory(
            id=22,
            profile_status="unknown",
            blog_status="unknown",
            blog_url=None,
            last_confirmed_at=None,
        ),
        _frontier_record(record_factory, 23, "missing"),
    ]

    assert trailing_missing_count(records) == 1


def test_invalid_boundary_and_limit_are_rejected(
    record_factory: Callable[..., UserRecord],
) -> None:
    with pytest.raises(ValueError):
        boundary_reached([], threshold=0)
    with pytest.raises(ValueError):
        select_due_users([record_factory()], now="2027-01-01T00:00:00Z", limit=-1)
