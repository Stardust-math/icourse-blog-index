from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from icourse_blog_index.cli import (
    CrawlCounters,
    MassChangeCounters,
    ProcessedUser,
    _record_counters,
)
from icourse_blog_index.models import (
    BlogStatus,
    CheckResult,
    CrawlerState,
    CrawlPhase,
    InitializationStatus,
    Manifest,
    Observation,
    ProfileStatus,
    apply_observation,
)
from icourse_blog_index.storage import RepositoryStore


def _processed(user_id: int, change_events: int) -> ProcessedUser:
    return ProcessedUser(
        record=SimpleNamespace(id=user_id),
        confirmed=True,
        changes=tuple(object() for _ in range(change_events)),
        check_results=(CheckResult.OK,),
    )


def test_mass_change_uses_changed_users_not_change_events() -> None:
    audit = CrawlCounters()
    safety = MassChangeCounters()

    # This reproduces the shape of the 2026-09-03 pause: 20 attempted users,
    # 19 newly registered IDs, and two audit events for each registration.
    # Frontier probes are intentionally not fed into the maintenance-only
    # mass-change sample.
    for user_id in range(1, 21):
        processed = _processed(user_id, 2 if user_id < 20 else 0)
        _record_counters(audit, processed)

    assert audit.attempted == 20
    assert audit.changed == 38
    assert safety.attempted == 0
    assert safety.changed_users == 0
    assert not safety.reached(minimum_attempts=20, minimum_changes=10, ratio=0.25)


def test_mass_change_counts_one_vote_per_maintenance_rechecked_user() -> None:
    safety = MassChangeCounters()

    for user_id in range(1, 21):
        # Multiple semantic events from one maintenance-rechecked user still
        # contribute exactly one safety vote.
        safety.record(_processed(user_id, 3))

    assert safety.attempted == 20
    assert safety.changed_users == 20
    assert safety.reached(minimum_attempts=20, minimum_changes=10, ratio=0.25)


def _confirmed_record(
    user_id: int,
    profile_status: ProfileStatus,
    *,
    observed_at: str,
):
    observation = Observation(
        user_id=user_id,
        observed_at=observed_at,
        profile_status=profile_status,
        blog_status=(
            BlogStatus.ABSENT if profile_status is ProfileStatus.PUBLIC else BlogStatus.UNKNOWN
        ),
        check_result=CheckResult.OK,
        parser_version="test",
    )
    return apply_observation(None, observation, confirm_changes=False).record


def _repository(root: Path) -> RepositoryStore:
    store = RepositoryStore(root)
    store.ensure_directories()
    store.save_state(CrawlerState())
    store.write_manifest(Manifest())
    return store


class _RegistrationBurstFetcher:
    def __init__(self) -> None:
        self.calls: list[tuple[int, bool]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def check_robots(self, *, force: bool = False):
        assert force
        return SimpleNamespace(allowed=True, reason="test robots policy")

    def fetch_and_parse(self, user_id: int, *, cache_bust: bool = False) -> Observation:
        self.calls.append((user_id, cache_bust))
        suffix = len(self.calls)
        if user_id < 120:
            return Observation(
                user_id=user_id,
                observed_at=f"2026-09-03T22:{suffix // 60:02d}:{suffix % 60:02d}Z",
                profile_status=ProfileStatus.PUBLIC,
                blog_status=BlogStatus.ABSENT,
                check_result=CheckResult.OK,
                parser_version="test",
            )
        return Observation(
            user_id=user_id,
            observed_at=f"2026-09-03T22:{suffix // 60:02d}:{suffix % 60:02d}Z",
            profile_status=ProfileStatus.MISSING,
            blog_status=BlogStatus.UNKNOWN,
            check_result=CheckResult.OK,
            parser_version="test",
        )


def test_update_allows_confirmed_registration_burst_without_mass_change_pause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _repository(tmp_path)
    records = [
        _confirmed_record(
            100,
            ProfileStatus.PUBLIC,
            observed_at="2026-09-01T00:00:00Z",
        )
    ]
    records.extend(
        _confirmed_record(
            user_id,
            ProfileStatus.MISSING,
            observed_at="2025-01-01T00:00:00Z",
        )
        for user_id in range(101, 121)
    )
    store.upsert_users(records)
    store.save_state(
        CrawlerState(
            phase=CrawlPhase.MAINTENANCE,
            next_id=121,
            highest_attempted_id=120,
            highest_confirmed_user_id=100,
            maintenance_cursor=101,
            updated_at="2026-09-03T00:00:00Z",
        )
    )
    store.rebuild_manifest(
        generated_at="2026-09-03T00:00:00Z",
        initialization_status=InitializationStatus.COMPLETE,
        phase=CrawlPhase.MAINTENANCE,
    )

    fetcher = _RegistrationBurstFetcher()
    monkeypatch.setattr("icourse_blog_index.cli._fetcher_from_args", lambda _args: fetcher)

    from icourse_blog_index.cli import main

    code = main(
        [
            "--root",
            str(tmp_path),
            "update",
            "--max-existing",
            "1",
            "--max-new",
            "20",
            "--frontier-sweep-size",
            "20",
            "--time-budget-seconds",
            "60",
            "--checkpoint-every",
            "1",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["attempted"] == 20
    assert output["changed"] == 38
    assert output["mass_change_attempted"] == 0
    assert output["mass_change_changed_users"] == 0
    assert output["stopped_reason"] == "maintenance batch completed"
    assert store.load_state().phase is CrawlPhase.MAINTENANCE
    assert store.load_state().highest_confirmed_user_id == 119


class _ExistingMassChangeFetcher:
    def __init__(self) -> None:
        self.calls: list[tuple[int, bool]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def check_robots(self, *, force: bool = False):
        assert force
        return SimpleNamespace(allowed=True, reason="test robots policy")

    def fetch_and_parse(self, user_id: int, *, cache_bust: bool = False) -> Observation:
        self.calls.append((user_id, cache_bust))
        suffix = len(self.calls)
        if user_id == 21:
            return Observation(
                user_id=user_id,
                observed_at=f"2026-09-03T23:{suffix // 60:02d}:{suffix % 60:02d}Z",
                profile_status=ProfileStatus.MISSING,
                blog_status=BlogStatus.UNKNOWN,
                check_result=CheckResult.OK,
                parser_version="test",
            )
        return Observation(
            user_id=user_id,
            observed_at=f"2026-09-03T23:{suffix // 60:02d}:{suffix % 60:02d}Z",
            profile_status=ProfileStatus.HIDDEN,
            blog_status=BlogStatus.UNKNOWN,
            check_result=CheckResult.OK,
            parser_version="test",
        )


def test_update_still_pauses_when_existing_users_change_in_mass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _repository(tmp_path)
    store.upsert_users(
        _confirmed_record(
            user_id,
            ProfileStatus.PUBLIC,
            observed_at="2025-01-01T00:00:00Z",
        )
        for user_id in range(1, 21)
    )
    store.save_state(
        CrawlerState(
            phase=CrawlPhase.MAINTENANCE,
            next_id=21,
            highest_attempted_id=20,
            highest_confirmed_user_id=20,
            maintenance_cursor=21,
            updated_at="2026-09-03T00:00:00Z",
        )
    )
    store.rebuild_manifest(
        generated_at="2026-09-03T00:00:00Z",
        initialization_status=InitializationStatus.COMPLETE,
        phase=CrawlPhase.MAINTENANCE,
    )

    fetcher = _ExistingMassChangeFetcher()
    monkeypatch.setattr("icourse_blog_index.cli._fetcher_from_args", lambda _args: fetcher)

    from icourse_blog_index.cli import main

    code = main(
        [
            "--root",
            str(tmp_path),
            "update",
            "--max-existing",
            "20",
            "--max-new",
            "1",
            "--frontier-sweep-size",
            "256",
            "--time-budget-seconds",
            "60",
            "--checkpoint-every",
            "1",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert code == 3
    assert output["attempted"] == 21
    assert output["mass_change_attempted"] == 20
    assert output["mass_change_changed_users"] == 20
    assert output["stopped_reason"] == "mass-change safety threshold reached"
    state = store.load_state()
    assert state.phase is CrawlPhase.PAUSED
    assert state.paused_from_phase is CrawlPhase.MAINTENANCE
