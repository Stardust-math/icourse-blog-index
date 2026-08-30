from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from icourse_blog_index.models import (
    BlogStatus,
    ChangeKind,
    CheckResult,
    CrawlPhase,
    CrawlerState,
    InitializationStatus,
    Manifest,
    Observation,
    ProfileStatus,
    UserRecord,
    apply_observation,
)
from icourse_blog_index.storage import RepositoryStore


@pytest.mark.parametrize("profile_status", list(ProfileStatus))
@pytest.mark.parametrize("blog_status", list(BlogStatus))
def test_user_record_round_trips_every_status_combination(
    profile_status: ProfileStatus, blog_status: BlogStatus
) -> None:
    record = UserRecord(
        id=5,
        profile_status=profile_status,
        blog_status=blog_status,
        blog_url="https://example.com" if blog_status is BlogStatus.PRESENT else None,
        last_check_result=CheckResult.OK,
    )

    assert UserRecord.from_dict(record.to_dict()).to_dict() == record.to_dict()


def test_first_blog_observation_requires_independent_confirmation(
    observation_factory: Callable[..., Observation], timestamps: tuple[str, ...]
) -> None:
    first = observation_factory(observed_at=timestamps[0])
    pending = apply_observation(None, first)

    assert pending.pending is True
    assert pending.confirmed is False
    assert pending.record.last_confirmed_at is None
    assert pending.record.profile_status is ProfileStatus.UNKNOWN
    assert pending.record.pending_observation is not None
    assert pending.record.pending_observation.confirmations == 1

    duplicate = apply_observation(pending.record, first)
    assert duplicate.pending is True
    assert duplicate.record.pending_observation is not None
    assert duplicate.record.pending_observation.confirmations == 1

    confirmed = apply_observation(
        duplicate.record,
        observation_factory(observed_at=timestamps[1]),
    )
    assert confirmed.confirmed is True
    assert confirmed.pending is False
    assert confirmed.record.profile_status is ProfileStatus.PUBLIC
    assert confirmed.record.blog_status is BlogStatus.PRESENT
    assert confirmed.record.blog_url == "https://stardust-math.pages.dev/"
    assert confirmed.record.pending_observation is None


@pytest.mark.parametrize(
    ("profile_status", "blog_status"),
    [
        (ProfileStatus.PUBLIC, BlogStatus.ABSENT),
        (ProfileStatus.HIDDEN, BlogStatus.UNKNOWN),
        (ProfileStatus.MISSING, BlogStatus.UNKNOWN),
    ],
)
def test_first_non_blog_observations_are_committed_immediately(
    observation_factory: Callable[..., Observation],
    profile_status: ProfileStatus,
    blog_status: BlogStatus,
) -> None:
    result = apply_observation(
        None,
        observation_factory(
            profile_status=profile_status,
            blog_status=blog_status,
            blog_url=None,
            blog_url_raw=None,
        ),
    )

    assert result.confirmed is True
    assert result.pending is False
    assert result.record.profile_status is profile_status
    assert result.record.blog_status is blog_status


@pytest.mark.parametrize(
    "check_result",
    [result for result in CheckResult if result is not CheckResult.OK],
)
def test_unsuccessful_attempt_preserves_last_confirmed_values(
    record_factory: Callable[..., UserRecord],
    observation_factory: Callable[..., Observation],
    check_result: CheckResult,
) -> None:
    previous = record_factory()
    result = apply_observation(
        previous,
        observation_factory(
            observed_at="2026-08-30T01:00:00Z",
            profile_status=ProfileStatus.UNKNOWN,
            blog_status=BlogStatus.UNKNOWN,
            blog_url=None,
            blog_url_raw=None,
            check_result=check_result,
            http_status=None,
            error="temporary failure",
        ),
    )

    assert result.confirmed is False
    assert result.changes == ()
    assert result.record.profile_status is ProfileStatus.PUBLIC
    assert result.record.blog_status is BlogStatus.PRESENT
    assert result.record.blog_url == "https://stardust-math.pages.dev/"
    assert result.record.last_confirmed_at == previous.last_confirmed_at
    assert result.record.last_checked_at == "2026-08-30T01:00:00Z"
    assert result.record.last_check_result is check_result
    assert result.record.consecutive_failures == 1


def test_blog_change_is_pending_then_committed_with_change_event(
    record_factory: Callable[..., UserRecord],
    observation_factory: Callable[..., Observation],
    timestamps: tuple[str, ...],
) -> None:
    previous = record_factory()
    changed = {
        "blog_url": "https://new.example/blog",
        "blog_url_raw": "https://new.example/blog",
    }
    first = apply_observation(
        previous,
        observation_factory(observed_at=timestamps[0], **changed),
        run_id="run-1",
    )

    assert first.pending is True
    assert first.record.blog_url == previous.blog_url
    assert first.record.pending_observation is not None
    assert first.record.pending_observation.blog_url == "https://new.example/blog"

    second = apply_observation(
        first.record,
        observation_factory(observed_at=timestamps[1], **changed),
        run_id="run-2",
    )
    assert second.confirmed is True
    assert second.record.blog_url == "https://new.example/blog"
    assert [event.kind for event in second.changes] == [ChangeKind.BLOG_CHANGED]
    assert second.changes[0].run_id == "run-2"


def test_conflicting_candidate_restarts_pending_confirmation(
    record_factory: Callable[..., UserRecord],
    observation_factory: Callable[..., Observation],
    timestamps: tuple[str, ...],
) -> None:
    first = apply_observation(
        record_factory(),
        observation_factory(
            observed_at=timestamps[0],
            blog_url="https://candidate-one.example",
            blog_url_raw="https://candidate-one.example",
        ),
    )
    second = apply_observation(
        first.record,
        observation_factory(
            observed_at=timestamps[1],
            blog_url="https://candidate-two.example",
            blog_url_raw="https://candidate-two.example",
        ),
    )

    assert second.pending is True
    assert second.record.pending_observation is not None
    assert second.record.pending_observation.confirmations == 1
    assert second.record.pending_observation.blog_url == "https://candidate-two.example/"


def test_return_to_confirmed_value_clears_pending_candidate(
    record_factory: Callable[..., UserRecord],
    observation_factory: Callable[..., Observation],
) -> None:
    first = apply_observation(
        record_factory(),
        observation_factory(
            blog_url="https://candidate.example",
            blog_url_raw="https://candidate.example",
        ),
    )
    returned = apply_observation(
        first.record,
        observation_factory(
            observed_at="2026-08-30T00:01:00Z",
            blog_url="https://stardust-math.pages.dev",
            blog_url_raw="https://stardust-math.pages.dev",
        ),
    )

    assert returned.confirmed is True
    assert returned.record.pending_observation is None
    assert returned.record.blog_url == "https://stardust-math.pages.dev/"


def test_storage_shards_boundaries_and_iterates_deterministically(
    tmp_path: Path, record_factory: Callable[..., UserRecord]
) -> None:
    store = RepositoryStore(tmp_path)
    records = [
        record_factory(id=1000),
        record_factory(id=999),
        record_factory(id=1),
        record_factory(id=2000),
    ]
    store.upsert_users(records)

    users_dir = tmp_path / "data" / "users"
    assert {path.name for path in users_dir.glob("*.jsonl")} == {
        "00000-00999.jsonl",
        "01000-01999.jsonl",
        "02000-02999.jsonl",
    }
    assert [record.id for record in store.iter_users()] == [1, 999, 1000, 2000]
    first_bytes = {path.name: path.read_bytes() for path in sorted(users_dir.glob("*.jsonl"))}
    store.upsert_users(reversed(records))
    second_bytes = {path.name: path.read_bytes() for path in sorted(users_dir.glob("*.jsonl"))}
    assert second_bytes == first_bytes
    assert store.load_user(999) is not None
    assert store.load_user(999).id == 999  # type: ignore[union-attr]
    assert store.load_user(12345) is None


def test_upsert_preserves_other_records_in_same_shard(
    tmp_path: Path, record_factory: Callable[..., UserRecord]
) -> None:
    store = RepositoryStore(tmp_path)
    store.upsert_users([record_factory(id=1), record_factory(id=2)])
    store.upsert_users([record_factory(id=2, profile_status="missing", blog_status="unknown")])

    assert [record.id for record in store.iter_users()] == [1, 2]
    assert store.load_user(1) is not None
    assert store.load_user(2).profile_status is ProfileStatus.MISSING  # type: ignore[union-attr]


def test_state_and_manifest_round_trip_and_leave_no_temp_files(tmp_path: Path) -> None:
    store = RepositoryStore(tmp_path)
    state = CrawlerState(
        phase=CrawlPhase.BOUNDARY_CONFIRMATION,
        next_id=123,
        highest_attempted_id=122,
        highest_confirmed_user_id=100,
        updated_at="2026-08-30T00:00:00Z",
    )
    manifest = Manifest(
        generated_at="2026-08-30T00:00:00Z",
        phase="bootstrap",
        initialization_status=InitializationStatus.IN_PROGRESS,
        record_count=122,
    )

    store.save_state(state)
    store.write_manifest(manifest)

    assert store.load_state() == state
    assert store.load_manifest() == manifest
    assert not list(tmp_path.rglob("*.tmp"))
    assert (
        json.loads((tmp_path / "state" / "crawler.json").read_text(encoding="utf-8"))["next_id"]
        == 123
    )
