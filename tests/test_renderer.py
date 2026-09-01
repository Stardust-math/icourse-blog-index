from __future__ import annotations

import csv
import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from icourse_blog_index.cli import (
    CommandError,
    ProcessedUser,
    _PARSE_FAILURE_RESULTS,
    _advance_safety_streak,
    _frontier_probe_ids,
    command_inspect_user,
    command_resume,
    command_validate,
)
from icourse_blog_index.fetcher import CacheMetadata, FetchOutcome, FetchResult
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
from icourse_blog_index.renderer import (
    INDEX_BEGIN,
    INDEX_END,
    RenderError,
    load_records,
    render_blogs_csv,
    render_readme_text,
    render_repository,
    replace_generated_section,
)
from icourse_blog_index.storage import RepositoryStore


TEMPLATE = f"""# Index

Authored introduction.

{INDEX_BEGIN}
old table
{INDEX_END}

Authored footer.
"""


def _records() -> list[dict[str, object]]:
    return [
        {
            "id": 12,
            "profile_status": "public",
            "blog_status": "present",
            "blog_url": "https://example.com/a_(b)|x",
            "last_confirmed_at": "2026-08-30T12:34:56Z",
        },
        {
            "id": 2,
            "profile_status": "public",
            "blog_status": "present",
            "blog_url": "https://z.example/博客",
            "last_confirmed_at": "2026-08-29T00:00:00Z",
        },
        {
            "id": 3,
            "profile_status": "public",
            "blog_status": "absent",
            "blog_url": None,
        },
        {
            "id": 4,
            "profile_status": "hidden",
            "blog_status": "unknown",
            "blog_url": "https://must-not-be-listed.example",
        },
    ]


def test_replace_generated_section_preserves_authored_text() -> None:
    result = replace_generated_section(TEMPLATE, INDEX_BEGIN, INDEX_END, "new status")
    assert "Authored introduction." in result
    assert "Authored footer." in result
    assert f"{INDEX_BEGIN}\nnew status\n{INDEX_END}" in result
    assert "old table" not in result


def test_replace_generated_section_requires_unique_markers() -> None:
    with pytest.raises(RenderError):
        replace_generated_section("no markers", INDEX_BEGIN, INDEX_END, "content")


def test_readme_lists_only_confirmed_public_blogs_in_id_order() -> None:
    rendered = render_readme_text(
        TEMPLATE,
        reversed(_records()),
        {"phase": "complete", "highest_existing_user_id": 12},
        {
            "https://z.example/博客": {"status": "reachable"},
            "https://example.com/a_(b)|x": {"status": "redirected"},
        },
    )
    row_2 = rendered.index("[2](https://icourse.club/user/2)")
    row_12 = rendered.index("[12](https://icourse.club/user/12)")
    assert row_2 < row_12
    assert "must-not-be-listed" not in rendered
    assert "| 已重定向 | 2026-08-30 |" in rendered
    assert "a_%28b%29%7Cx" in rendered
    assert "%E5%8D%9A%E5%AE%A2" in rendered
    assert "Authored footer." in rendered


def test_csv_is_deterministic_and_contains_only_listed_blogs() -> None:
    first = render_blogs_csv(_records())
    second = render_blogs_csv(list(reversed(_records())))
    assert first == second
    rows = list(csv.DictReader(io.StringIO(first)))
    assert [row["user_id"] for row in rows] == ["2", "12"]
    assert rows[0]["blog_url"] == "https://z.example/博客"
    assert all("must-not-be-listed" not in row["blog_url"] for row in rows)


def test_unconfirmed_link_failure_is_presented_as_pending() -> None:
    record = _records()[1]
    url = str(record["blog_url"])
    pending = {url: {"status": "timeout", "failure_confirmed": False}}
    confirmed = {url: {"status": "timeout", "failure_confirmed": True}}

    pending_readme = render_readme_text(TEMPLATE, [record], {}, pending)
    assert "| 待复核 |" in pending_readme
    pending_csv = list(csv.DictReader(io.StringIO(render_blogs_csv([record], pending))))
    assert pending_csv[0]["health_status"] == "pending_failure"

    confirmed_readme = render_readme_text(TEMPLATE, [record], {}, confirmed)
    assert "| 检查超时 |" in confirmed_readme


def test_repository_render_and_staleness_check(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(TEMPLATE, encoding="utf-8")
    users_dir = tmp_path / "data" / "users"
    users_dir.mkdir(parents=True)
    with (users_dir / "00000-00999.jsonl").open("w", encoding="utf-8") as handle:
        for record in _records():
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    (tmp_path / "data" / "manifest.json").write_text(
        json.dumps({"phase": "initializing"}), encoding="utf-8"
    )

    result = render_repository(tmp_path)
    assert result == {"records": 4, "blogs": 2}
    render_repository(tmp_path, check=True)

    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace("initializing", "stale"), encoding="utf-8"
    )
    with pytest.raises(RenderError, match="stale"):
        render_repository(tmp_path, check=True)


def test_load_records_rejects_duplicate_user_ids(tmp_path: Path) -> None:
    users = tmp_path / "data" / "users"
    users.mkdir(parents=True)
    (users / "a.jsonl").write_text('{"id": 1}\n', encoding="utf-8")
    (users / "b.jsonl").write_text('{"id": 1}\n', encoding="utf-8")
    with pytest.raises(RenderError, match="duplicate user ID 1"):
        load_records(tmp_path)


class _FakeFetcher:
    def __init__(self, *, allowed: bool, probe_result: CheckResult = CheckResult.OK) -> None:
        self.allowed = allowed
        self.probe_result = probe_result

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def check_robots(self, *, force: bool = False):
        assert force
        return SimpleNamespace(allowed=self.allowed, reason="test robots policy")

    def fetch_and_parse(self, user_id: int, *, cache_bust: bool = False) -> Observation:
        assert cache_bust
        return Observation(
            user_id=user_id,
            observed_at="2026-08-30T00:00:00Z",
            profile_status=("missing" if self.probe_result is CheckResult.OK else "unknown"),
            blog_status="unknown",
            check_result=self.probe_result,
            parser_version="test",
            error=(None if self.probe_result is CheckResult.OK else "test safety refusal"),
        )


def _paused_repository(root: Path) -> RepositoryStore:
    store = RepositoryStore(root)
    store.ensure_directories()
    store.save_state(
        CrawlerState(
            phase=CrawlPhase.PAUSED,
            paused_from_phase=CrawlPhase.MAINTENANCE,
            updated_at="2026-08-30T00:00:00Z",
        )
    )
    store.write_manifest(
        Manifest(
            initialization_status=InitializationStatus.PAUSED,
            phase=CrawlPhase.PAUSED.value,
        )
    )
    return store


def _repository_skeleton(root: Path) -> RepositoryStore:
    store = RepositoryStore(root)
    store.ensure_directories()
    store.save_state(CrawlerState())
    store.write_manifest(Manifest())
    (root / "README.md").write_text(TEMPLATE, encoding="utf-8")
    return store


def test_resume_requires_acknowledgement_and_records_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _paused_repository(tmp_path)
    monkeypatch.setattr(
        "icourse_blog_index.cli._fetcher_from_args", lambda _args: _FakeFetcher(allowed=True)
    )
    args = SimpleNamespace(root=tmp_path, acknowledge="reviewed robots and site response")
    assert command_resume(args) == 0

    state = store.load_state()
    assert state.phase is CrawlPhase.MAINTENANCE
    assert state.paused_from_phase is None
    assert state.last_run_id is not None
    run_log = (tmp_path / "data" / "runs" / "2026.jsonl").read_text(encoding="utf-8")
    assert "operator acknowledgement: reviewed robots and site response" in run_log


def test_resume_robots_refusal_does_not_change_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _paused_repository(tmp_path)
    before = store.load_state().to_dict()
    monkeypatch.setattr(
        "icourse_blog_index.cli._fetcher_from_args", lambda _args: _FakeFetcher(allowed=False)
    )
    args = SimpleNamespace(root=tmp_path, acknowledge="review complete")

    with pytest.raises(CommandError, match="safety stop"):
        command_resume(args)
    assert store.load_state().to_dict() == before
    assert not any((tmp_path / "data" / "runs").glob("*.jsonl"))


def test_resume_active_block_refusal_does_not_change_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _paused_repository(tmp_path)
    before = store.load_state().to_dict()
    monkeypatch.setattr(
        "icourse_blog_index.cli._fetcher_from_args",
        lambda _args: _FakeFetcher(allowed=True, probe_result=CheckResult.BLOCKED_OR_CHALLENGE),
    )
    args = SimpleNamespace(root=tmp_path, acknowledge="review complete")

    with pytest.raises(CommandError, match="still reports blocked_or_challenge"):
        command_resume(args)
    assert store.load_state().to_dict() == before
    assert not any((tmp_path / "data" / "runs").glob("*.jsonl"))


def _confirmed_record(user_id: int, profile_status: ProfileStatus):
    observation = Observation(
        user_id=user_id,
        observed_at="2026-08-30T00:00:00Z",
        profile_status=profile_status,
        blog_status=(
            BlogStatus.ABSENT if profile_status is ProfileStatus.PUBLIC else BlogStatus.UNKNOWN
        ),
        check_result=CheckResult.OK,
        parser_version="test",
    )
    return apply_observation(None, observation, confirm_changes=False).record


def _failed_record(
    user_id: int,
    *,
    check_result: CheckResult = CheckResult.PARSE_ERROR,
    error: str = "unsafe or malformed blog URL",
    http_status: int | None = 200,
    parser_version: str = "test",
):
    return apply_observation(
        None,
        Observation(
            user_id=user_id,
            observed_at="2026-08-29T00:00:00Z",
            check_result=check_result,
            http_status=http_status,
            parser_version=parser_version,
            error=error,
        ),
    ).record


def test_frontier_probe_progresses_across_batches_and_periodically_wraps() -> None:
    records = [_confirmed_record(10, ProfileStatus.PUBLIC)]
    first = CrawlerState(phase=CrawlPhase.MAINTENANCE, maintenance_cursor=1)
    second = replace(first, maintenance_cursor=13)
    wrapped = replace(first, maintenance_cursor=267)

    assert _frontier_probe_ids(records, first, max_new=2, sweep_size=256) == [11, 12]
    assert _frontier_probe_ids(records, second, max_new=2, sweep_size=256) == [13, 14]
    assert _frontier_probe_ids(records, wrapped, max_new=2, sweep_size=256) == [11, 12]


class _BoundaryFetcher(_FakeFetcher):
    def __init__(self) -> None:
        super().__init__(allowed=True)
        self.calls: list[int] = []

    def fetch_and_parse(self, user_id: int, *, cache_bust: bool = False) -> Observation:
        assert not cache_bust
        self.calls.append(user_id)
        return Observation(
            user_id=user_id,
            observed_at=f"2026-08-30T00:{len(self.calls):02d}:00Z",
            profile_status=("public" if user_id == 1 else "missing"),
            blog_status=("absent" if user_id == 1 else "unknown"),
            check_result="ok",
            parser_version="test",
        )


def test_bootstrap_boundary_confirmation_clears_candidate_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _repository_skeleton(tmp_path)
    fetcher = _BoundaryFetcher()
    monkeypatch.setattr("icourse_blog_index.cli._fetcher_from_args", lambda _args: fetcher)
    common = [
        "--root",
        str(tmp_path),
        "bootstrap-chunk",
        "--time-budget-seconds",
        "60",
        "--checkpoint-every",
        "1",
        "--boundary-missing-count",
        "2",
    ]

    from icourse_blog_index.cli import main

    assert main([*common, "--max-ids", "3"]) == 0
    capsys.readouterr()
    candidate = store.load_state()
    assert candidate.phase is CrawlPhase.BOUNDARY_CONFIRMATION
    assert candidate.boundary_candidate_start == 2
    assert candidate.next_id == 2

    assert main([*common, "--max-ids", "2"]) == 0
    capsys.readouterr()
    completed = store.load_state()
    assert completed.phase is CrawlPhase.MAINTENANCE
    assert completed.boundary_candidate_start is None
    assert completed.boundary_confirmation_pending is False
    assert completed.consecutive_missing_after_frontier == 0


class _BoundaryFailureFetcher(_FakeFetcher):
    def fetch_and_parse(self, user_id: int, *, cache_bust: bool = False) -> Observation:
        assert not cache_bust
        return Observation(
            user_id=user_id,
            observed_at="2026-08-30T01:00:00Z",
            check_result=CheckResult.NETWORK_ERROR,
            parser_version="test",
            error="temporary network failure",
        )


def test_boundary_confirmation_cannot_reuse_old_missing_after_network_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _repository_skeleton(tmp_path)
    successful = _BoundaryFetcher()
    monkeypatch.setattr("icourse_blog_index.cli._fetcher_from_args", lambda _args: successful)
    command = [
        "--root",
        str(tmp_path),
        "bootstrap-chunk",
        "--time-budget-seconds",
        "60",
        "--checkpoint-every",
        "1",
        "--boundary-missing-count",
        "2",
    ]

    from icourse_blog_index.cli import main

    assert main([*command, "--max-ids", "3"]) == 0
    capsys.readouterr()
    assert store.load_state().phase is CrawlPhase.BOUNDARY_CONFIRMATION

    failing = _BoundaryFailureFetcher(allowed=True)
    monkeypatch.setattr("icourse_blog_index.cli._fetcher_from_args", lambda _args: failing)
    assert main([*command, "--max-ids", "2"]) == 0
    output = json.loads(capsys.readouterr().out)

    state = store.load_state()
    assert state.phase is CrawlPhase.BOUNDARY_CONFIRMATION
    assert state.next_id == state.boundary_candidate_start == 2
    assert output["stopped_reason"] == "boundary confirmation was inconclusive"
    assert store.load_user(2).last_check_result is CheckResult.NETWORK_ERROR


class _MaintenanceFetcher(_FakeFetcher):
    def __init__(self) -> None:
        super().__init__(allowed=True)
        self.calls: list[int] = []

    def fetch_and_parse(self, user_id: int, *, cache_bust: bool = False) -> Observation:
        assert not cache_bust
        self.calls.append(user_id)
        return Observation(
            user_id=user_id,
            observed_at=f"2026-08-30T01:{len(self.calls):02d}:00Z",
            profile_status="missing",
            blog_status="unknown",
            check_result="ok",
            parser_version="test",
        )


class _FailingMaintenanceFetcher(_FakeFetcher):
    def __init__(self) -> None:
        super().__init__(allowed=True)
        self.calls: list[int] = []

    def fetch_and_parse(self, user_id: int, *, cache_bust: bool = False) -> Observation:
        self.calls.append(user_id)
        return Observation(
            user_id=user_id,
            observed_at=f"2026-08-30T02:{len(self.calls):02d}:00Z",
            check_result=CheckResult.SERVER_ERROR,
            parser_version="test",
            error="temporary upstream failure",
        )


class _KnownFailureRetryFetcher(_FakeFetcher):
    def __init__(
        self,
        *,
        check_result: CheckResult,
        error: str,
        http_status: int | None,
    ) -> None:
        super().__init__(allowed=True)
        self.check_result = check_result
        self.error = error
        self.http_status = http_status
        self.calls: list[int] = []

    def fetch_and_parse(self, user_id: int, *, cache_bust: bool = False) -> Observation:
        assert not cache_bust
        self.calls.append(user_id)
        if user_id == 11:
            return Observation(
                user_id=user_id,
                observed_at="2026-08-30T03:00:00Z",
                profile_status=ProfileStatus.MISSING,
                blog_status=BlogStatus.UNKNOWN,
                check_result=CheckResult.OK,
                parser_version="test",
            )
        return Observation(
            user_id=user_id,
            observed_at=f"2026-08-30T03:{len(self.calls):02d}:00Z",
            check_result=self.check_result,
            http_status=self.http_status,
            parser_version="test",
            error=self.error,
        )


@pytest.mark.parametrize(
    ("check_result", "error", "http_status"),
    [
        (CheckResult.PARSE_ERROR, "unsafe or malformed blog URL", 200),
        (CheckResult.SERVER_ERROR, "profile server returned HTTP 525", 525),
    ],
)
def test_update_does_not_treat_stable_known_failures_as_site_wide_outage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    check_result: CheckResult,
    error: str,
    http_status: int | None,
) -> None:
    store = _repository_skeleton(tmp_path)
    known_failures = [
        _failed_record(
            user_id,
            check_result=check_result,
            error=error,
            http_status=http_status,
        )
        for user_id in range(1, 6)
    ]
    store.upsert_users([*known_failures, _confirmed_record(10, ProfileStatus.PUBLIC)])
    store.save_state(
        CrawlerState(
            phase=CrawlPhase.MAINTENANCE,
            next_id=11,
            highest_attempted_id=10,
            highest_confirmed_user_id=10,
            maintenance_cursor=11,
            updated_at="2026-08-30T00:00:00Z",
        )
    )
    store.rebuild_manifest(
        generated_at="2026-08-30T00:00:00Z",
        initialization_status=InitializationStatus.COMPLETE,
        phase=CrawlPhase.MAINTENANCE,
    )
    fetcher = _KnownFailureRetryFetcher(
        check_result=check_result,
        error=error,
        http_status=http_status,
    )
    monkeypatch.setattr("icourse_blog_index.cli._fetcher_from_args", lambda _args: fetcher)

    from icourse_blog_index.cli import main

    code = main(
        [
            "--root",
            str(tmp_path),
            "update",
            "--max-existing",
            "5",
            "--max-new",
            "1",
            "--frontier-sweep-size",
            "256",
            "--time-budget-seconds",
            "60",
            "--checkpoint-every",
            "1",
            "--max-consecutive-parse-failures",
            "5",
            "--max-consecutive-transient-failures",
            "5",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert fetcher.calls == [11, 1, 2, 3, 4, 5]
    assert output["attempted"] == 6
    assert output["errors"] == 5
    assert output["stopped_reason"] == "maintenance batch completed"
    assert store.load_state().phase is CrawlPhase.MAINTENANCE
    assert store.load_manifest().initialization_status is InitializationStatus.COMPLETE
    assert all(store.load_user(user_id).consecutive_failures == 2 for user_id in range(1, 6))


class _NewParseFailureFetcher(_FakeFetcher):
    def __init__(self) -> None:
        super().__init__(allowed=True)
        self.calls: list[int] = []

    def fetch_and_parse(self, user_id: int, *, cache_bust: bool = False) -> Observation:
        assert not cache_bust
        self.calls.append(user_id)
        return Observation(
            user_id=user_id,
            observed_at=f"2026-08-30T04:{len(self.calls):02d}:00Z",
            check_result=CheckResult.PARSE_ERROR,
            http_status=200,
            parser_version="test",
            error="known profile-state markers and the labelled blog field were not found",
        )


def test_update_still_pauses_after_new_consecutive_parse_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _repository_skeleton(tmp_path)
    store.upsert_users([_confirmed_record(1, ProfileStatus.PUBLIC)])
    store.save_state(
        CrawlerState(
            phase=CrawlPhase.MAINTENANCE,
            next_id=2,
            highest_attempted_id=1,
            highest_confirmed_user_id=1,
            maintenance_cursor=2,
            updated_at="2026-08-30T00:00:00Z",
        )
    )
    store.rebuild_manifest(
        generated_at="2026-08-30T00:00:00Z",
        initialization_status=InitializationStatus.COMPLETE,
        phase=CrawlPhase.MAINTENANCE,
    )
    fetcher = _NewParseFailureFetcher()
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
            "256",
            "--time-budget-seconds",
            "60",
            "--checkpoint-every",
            "1",
            "--max-consecutive-parse-failures",
            "3",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert code == 3
    assert fetcher.calls == [2, 3, 4]
    assert output["stopped_reason"] == "consecutive parse/staleness safety threshold reached"
    state = store.load_state()
    assert state.phase is CrawlPhase.PAUSED
    assert state.paused_from_phase is CrawlPhase.MAINTENANCE


def test_stable_failure_is_neutral_but_changed_signature_advances_safety_streak() -> None:
    previous = _failed_record(1)
    same = apply_observation(
        previous,
        Observation(
            user_id=1,
            observed_at="2026-08-30T00:00:00Z",
            check_result=CheckResult.PARSE_ERROR,
            http_status=200,
            parser_version="test",
            error="unsafe or malformed blog URL",
        ),
    )
    repeated = ProcessedUser(
        record=same.record,
        confirmed=False,
        changes=(),
        check_results=(CheckResult.PARSE_ERROR,),
    )
    assert _advance_safety_streak(2, previous, repeated, _PARSE_FAILURE_RESULTS) == 2

    changed_error = apply_observation(
        previous,
        Observation(
            user_id=1,
            observed_at="2026-08-30T00:00:00Z",
            check_result=CheckResult.PARSE_ERROR,
            http_status=200,
            parser_version="test",
            error="known profile-state markers were not found",
        ),
    )
    changed_error_retry = ProcessedUser(
        record=changed_error.record,
        confirmed=False,
        changes=(),
        check_results=(CheckResult.PARSE_ERROR,),
    )
    assert _advance_safety_streak(2, previous, changed_error_retry, _PARSE_FAILURE_RESULTS) == 3

    changed_parser = apply_observation(
        previous,
        Observation(
            user_id=1,
            observed_at="2026-08-30T00:00:00Z",
            check_result=CheckResult.PARSE_ERROR,
            http_status=200,
            parser_version="new-parser",
            error="unsafe or malformed blog URL",
        ),
    )
    changed_parser_retry = ProcessedUser(
        record=changed_parser.record,
        confirmed=False,
        changes=(),
        check_results=(CheckResult.PARSE_ERROR,),
    )
    assert _advance_safety_streak(2, previous, changed_parser_retry, _PARSE_FAILURE_RESULTS) == 3


def test_repeated_suspected_stale_response_still_advances_safety_streak() -> None:
    previous = _failed_record(
        1,
        check_result=CheckResult.SUSPECTED_STALE,
        error="cached response metadata",
    )
    repeated = apply_observation(
        previous,
        Observation(
            user_id=1,
            observed_at="2026-08-30T00:00:00Z",
            check_result=CheckResult.SUSPECTED_STALE,
            http_status=200,
            parser_version="test",
            error="cached response metadata",
        ),
    )
    processed = ProcessedUser(
        record=repeated.record,
        confirmed=False,
        changes=(),
        check_results=(CheckResult.SUSPECTED_STALE,),
    )
    assert _advance_safety_streak(2, previous, processed, _PARSE_FAILURE_RESULTS) == 3


def test_update_pauses_after_consecutive_transport_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _repository_skeleton(tmp_path)
    store.upsert_users([_confirmed_record(1, ProfileStatus.PUBLIC)])
    store.save_state(
        CrawlerState(
            phase=CrawlPhase.MAINTENANCE,
            next_id=2,
            highest_attempted_id=1,
            highest_confirmed_user_id=1,
            maintenance_cursor=2,
            updated_at="2026-08-30T00:00:00Z",
        )
    )
    store.rebuild_manifest(
        generated_at="2026-08-30T00:00:00Z",
        initialization_status=InitializationStatus.COMPLETE,
        phase=CrawlPhase.MAINTENANCE,
    )
    fetcher = _FailingMaintenanceFetcher()
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
            "256",
            "--time-budget-seconds",
            "60",
            "--checkpoint-every",
            "1",
            "--max-consecutive-transient-failures",
            "2",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert code == 3
    assert fetcher.calls == [2, 3]
    assert output["stopped_reason"] == "consecutive transport/HTTP safety threshold reached"
    assert store.load_state().phase is CrawlPhase.PAUSED


def test_update_command_advances_and_wraps_maintenance_frontier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _repository_skeleton(tmp_path)
    store.upsert_users([_confirmed_record(1, ProfileStatus.PUBLIC)])
    store.save_state(
        CrawlerState(
            phase=CrawlPhase.MAINTENANCE,
            next_id=2,
            highest_attempted_id=1,
            highest_confirmed_user_id=1,
            maintenance_cursor=2,
            updated_at="2026-08-30T00:00:00Z",
        )
    )
    store.rebuild_manifest(
        generated_at="2026-08-30T00:00:00Z",
        initialization_status=InitializationStatus.COMPLETE,
        phase=CrawlPhase.MAINTENANCE,
    )
    fetcher = _MaintenanceFetcher()
    monkeypatch.setattr("icourse_blog_index.cli._fetcher_from_args", lambda _args: fetcher)
    command = [
        "--root",
        str(tmp_path),
        "update",
        "--max-existing",
        "1",
        "--max-new",
        "2",
        "--frontier-sweep-size",
        "4",
        "--time-budget-seconds",
        "60",
        "--checkpoint-every",
        "1",
    ]

    from icourse_blog_index.cli import main

    assert main(command) == 0
    capsys.readouterr()
    assert store.load_state().maintenance_cursor == 4
    assert main(command) == 0
    capsys.readouterr()
    assert store.load_state().maintenance_cursor == 2
    assert fetcher.calls == [2, 3, 4, 5]


def test_validate_accepts_confirmed_user_followed_by_missing_tail(tmp_path: Path) -> None:
    store = RepositoryStore(tmp_path)
    store.ensure_directories()
    records = [
        _confirmed_record(1, ProfileStatus.PUBLIC),
        _confirmed_record(2, ProfileStatus.MISSING),
    ]
    store.upsert_users(records)
    store.save_state(
        CrawlerState(
            phase=CrawlPhase.BOOTSTRAP,
            next_id=3,
            highest_attempted_id=2,
            highest_confirmed_user_id=1,
            updated_at="2026-08-30T00:00:00Z",
        )
    )
    store.rebuild_manifest(
        generated_at="2026-08-30T00:00:00Z",
        initialization_status=InitializationStatus.IN_PROGRESS,
        phase=CrawlPhase.BOOTSTRAP,
    )
    (tmp_path / "README.md").write_text(TEMPLATE, encoding="utf-8")
    render_repository(tmp_path)

    assert command_validate(SimpleNamespace(root=tmp_path)) == 0


class _StaleThenFreshFetcher(_FakeFetcher):
    def __init__(self) -> None:
        super().__init__(allowed=True)
        self.cache_bust_calls: list[bool] = []

    def fetch_user(self, user_id: int, *, cache_bust: bool = False) -> FetchResult:
        self.cache_bust_calls.append(cache_bust)
        old = not cache_bust
        blog = "https://stardust-math.github.io/" if old else "https://stardust-math.pages.dev/"
        body = f'<li>博客：<a href="{blog}"><bdi>{blog}</bdi></a></li>'
        return FetchResult(
            user_id=user_id,
            requested_url=f"https://icourse.club/user/{user_id}",
            final_url=f"https://icourse.club/user/{user_id}",
            fetched_at=("2026-08-30T00:00:00Z" if old else "2026-08-30T00:01:00Z"),
            outcome=FetchOutcome.OK,
            http_status=200,
            body=body,
            attempts=1,
            elapsed_seconds=0.1,
            cache_metadata=CacheMetadata(
                cf_cache_status=("HIT" if old else "DYNAMIC"),
                age_seconds=(3_600 if old else None),
            ),
            suspected_stale=old,
            stale_reasons=(("Age header is positive",) if old else ()),
        )


def test_inspect_user_retries_stale_response_and_remains_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fetcher = _StaleThenFreshFetcher()
    monkeypatch.setattr("icourse_blog_index.cli._fetcher_from_args", lambda _args: fetcher)
    args = SimpleNamespace(root=tmp_path, user_id=11706, cache_bust=False)

    assert command_inspect_user(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert fetcher.cache_bust_calls == [False, True]
    assert output["cache"]["retried_after_stale"] is True
    assert output["cache"]["cf_cache_status"] == "DYNAMIC"
    assert output["observation"]["blog_url"] == "https://stardust-math.pages.dev/"
    assert list(tmp_path.iterdir()) == []
