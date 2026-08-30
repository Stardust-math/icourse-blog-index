from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from icourse_blog_index.models import (
    BlogStatus,
    CrawlerState,
    PendingObservation,
    ProfileStatus,
    UserRecord,
)
import icourse_blog_index.cli as cli
from icourse_blog_index.models import Manifest
from icourse_blog_index.storage import DatasetCorruptionError, RepositoryStore
from icourse_blog_index.utils import canonical_json, semantic_profile_fingerprint
from icourse_blog_index.validator import (
    USER_FIELDS,
    validate_dataset,
    validate_record_dict,
    validate_records,
    validate_state,
    validate_user_record,
)


def _confirmed_record(
    user_id: int = 1,
    *,
    profile_status: ProfileStatus | str = ProfileStatus.PUBLIC,
    blog_status: BlogStatus | str = BlogStatus.PRESENT,
    blog_url: str | None = "https://example.com/",
) -> UserRecord:
    profile = ProfileStatus(profile_status)
    blog = BlogStatus(blog_status)
    fingerprint = semantic_profile_fingerprint(profile.value, blog.value, blog_url)
    return UserRecord(
        id=user_id,
        profile_status=profile,
        blog_status=blog,
        blog_url=blog_url,
        blog_url_raw=blog_url,
        first_checked_at="2026-08-30T00:00:00Z",
        last_checked_at="2026-08-30T00:00:00Z",
        last_confirmed_at="2026-08-30T00:00:00Z",
        profile_changed_at="2026-08-30T00:00:00Z",
        blog_changed_at="2026-08-30T00:00:00Z",
        last_check_result="ok",
        consecutive_failures=0,
        parser_version="test",
        source_fingerprint=fingerprint,
        http_status=200,
    )


@pytest.mark.parametrize("profile_status", list(ProfileStatus))
@pytest.mark.parametrize("blog_status", list(BlogStatus))
def test_validator_covers_every_profile_blog_status_combination(
    profile_status: ProfileStatus, blog_status: BlogStatus
) -> None:
    url = "https://example.com/" if blog_status is BlogStatus.PRESENT else None
    record = _confirmed_record(
        profile_status=profile_status,
        blog_status=blog_status,
        blog_url=url,
    )

    report = validate_user_record(record)
    allowed = profile_status is ProfileStatus.PUBLIC or blog_status is BlogStatus.UNKNOWN
    assert report.valid is allowed


def test_absent_public_blog_cannot_retain_raw_or_normalized_url() -> None:
    record = _confirmed_record(blog_status="absent", blog_url=None)
    report = validate_user_record(replace(record, blog_url_raw="https://old.example"))

    assert any(issue.code == "absent_blog_url" for issue in report.errors)


def test_present_blog_requires_url() -> None:
    report = validate_user_record(_confirmed_record(blog_status="present", blog_url=None))
    assert any(issue.code == "missing_blog_url" for issue in report.errors)


def test_private_blog_url_is_rejected() -> None:
    url = "http://127.0.0.1/"
    record = _confirmed_record(blog_url=url)
    report = validate_user_record(record)

    assert any(issue.code == "blog_url" for issue in report.errors)


def test_record_dict_rejects_missing_unknown_and_personal_fields() -> None:
    raw = _confirmed_record().to_dict()
    raw.pop("parser_version")
    raw["email"] = "private@example.com"
    raw["future_field"] = True

    report = validate_record_dict(raw, path="data/users/test.jsonl:1")

    assert {issue.code for issue in report.errors} == {
        "missing_fields",
        "out_of_scope_personal_data",
        "unknown_field",
    }
    assert all(issue.path == "data/users/test.jsonl:1" for issue in report.errors)


def test_canonical_record_dict_contains_exact_scope() -> None:
    raw = _confirmed_record().to_dict()
    assert set(raw) == USER_FIELDS
    assert validate_record_dict(raw).valid


@pytest.mark.parametrize(
    ("record", "code"),
    [
        (
            replace(
                _confirmed_record(),
                profile_url="https://example.com/not-the-profile",
            ),
            "profile_url",
        ),
        (
            replace(_confirmed_record(), consecutive_failures=1),
            "failure_count",
        ),
        (
            replace(
                _confirmed_record(),
                first_checked_at="2026-08-31T00:00:00Z",
            ),
            "timestamp_order",
        ),
        (
            replace(_confirmed_record(), source_fingerprint="wrong"),
            "fingerprint",
        ),
        (
            replace(_confirmed_record(), http_status=999),
            "http_status",
        ),
        (
            replace(_confirmed_record(), parser_version=" "),
            "parser_version",
        ),
    ],
)
def test_semantic_invariants_are_enforced(record: UserRecord, code: str) -> None:
    assert any(issue.code == code for issue in validate_user_record(record).errors)


def test_failed_attempt_requires_positive_failure_count() -> None:
    record = replace(
        _confirmed_record(),
        last_check_result="network_error",
        consecutive_failures=0,
    )
    assert any(issue.code == "failure_count" for issue in validate_user_record(record).errors)


def test_pending_candidate_timestamp_and_fingerprint_are_validated() -> None:
    pending = PendingObservation(
        profile_status="public",
        blog_status="present",
        blog_url="https://candidate.example/",
        blog_url_raw="https://candidate.example/",
        first_observed_at="2026-08-31T00:00:00Z",
        last_observed_at="2026-08-31T00:00:00Z",
        confirmations=1,
        parser_version="test",
        source_fingerprint="wrong",
    )
    record = replace(_confirmed_record(), pending_observation=pending)

    codes = {issue.code for issue in validate_user_record(record).errors}
    assert "pending_timestamp" in codes
    assert "pending_fingerprint" in codes


def test_validate_records_rejects_duplicate_and_non_sorted_ids() -> None:
    report = validate_records([_confirmed_record(2), _confirmed_record(1), _confirmed_record(1)])
    codes = [issue.code for issue in report.errors]

    assert "duplicate_id" in codes
    assert "record_order" in codes


def test_state_frontier_invariants_and_warning() -> None:
    state = CrawlerState(
        next_id=4,
        highest_attempted_id=4,
        highest_confirmed_user_id=5,
        boundary_confirmation_pending=True,
        boundary_candidate_start=None,
    )
    report = validate_state(state)

    assert {issue.code for issue in report.errors} == {"state_frontier", "state_boundary"}
    assert {issue.code for issue in report.warnings} == {"state_next_id"}


def test_dataset_validation_accepts_consistent_store_and_detects_stale_manifest(
    tmp_path: Path,
) -> None:
    store = RepositoryStore(tmp_path)
    store.upsert_users(
        [
            _confirmed_record(1),
            _confirmed_record(
                2,
                profile_status="public",
                blog_status="absent",
                blog_url=None,
            ),
            _confirmed_record(
                3,
                profile_status="missing",
                blog_status="unknown",
                blog_url=None,
            ),
        ]
    )
    store.rebuild_manifest(
        generated_at="2026-08-30T00:00:00Z",
        initialization_status="in_progress",
        phase="bootstrap",
    )
    store.save_state(
        CrawlerState(
            next_id=4,
            highest_attempted_id=3,
            highest_confirmed_user_id=2,
        )
    )

    assert validate_dataset(store).valid

    manifest = store.load_manifest()
    store.write_manifest(replace(manifest, blog_count=99))
    report = validate_dataset(tmp_path)
    assert any(issue.code == "manifest_mismatch" for issue in report.errors)


def test_dataset_read_corruption_is_reported_instead_of_raised(tmp_path: Path) -> None:
    users = tmp_path / "data" / "users"
    users.mkdir(parents=True)
    (users / "00000-00999.jsonl").write_text("{not json}\n", encoding="utf-8")

    report = validate_dataset(tmp_path)

    assert not report.valid
    assert [issue.code for issue in report.errors] == ["dataset_read"]
    with pytest.raises(ValueError, match="validation failed"):
        report.raise_for_errors()


def test_storage_rejects_unknown_personal_field_in_raw_user_shard(tmp_path: Path) -> None:
    store = RepositoryStore(tmp_path)
    store.upsert_users([_confirmed_record()])
    shard = store.shard_path(1)
    raw = json.loads(shard.read_text(encoding="utf-8"))
    raw["email"] = "private@example.com"
    shard.write_text(canonical_json(raw) + "\n", encoding="utf-8")

    with pytest.raises(DatasetCorruptionError, match=r"unknown field\(s\): email"):
        list(store.iter_users())


def test_storage_rejects_noncanonical_user_data_filename(tmp_path: Path) -> None:
    store = RepositoryStore(tmp_path)
    store.ensure_directories()
    (store.users_dir / "private-export.jsonl").write_text(
        '{"email":"private@example.com"}\n', encoding="utf-8"
    )

    with pytest.raises(DatasetCorruptionError, match="invalid user shard filename"):
        list(store.iter_users())


def test_persisted_json_types_are_not_silently_coerced() -> None:
    raw_user = _confirmed_record().to_dict()
    raw_user["id"] = "1"
    with pytest.raises(ValueError, match="field 'id' must be int"):
        UserRecord.from_dict(raw_user)

    raw_manifest = Manifest().to_dict()
    raw_manifest["schema_version"] = True
    with pytest.raises(ValueError, match="field 'schema_version' must be int"):
        Manifest.from_dict(raw_manifest)

    raw_state = CrawlerState().to_dict()
    raw_state["next_id"] = "1"
    with pytest.raises(ValueError, match="field 'next_id' must be int"):
        CrawlerState.from_dict(raw_state)


def test_persisted_user_metadata_limits_are_strict() -> None:
    raw = _confirmed_record().to_dict()
    raw["source_fingerprint"] = "not-a-sha256"
    with pytest.raises(ValueError, match="64 lowercase hex"):
        UserRecord.from_dict(raw)

    raw = _confirmed_record().to_dict()
    raw["parser_version"] = "unsafe version"
    with pytest.raises(ValueError, match="safe ASCII"):
        UserRecord.from_dict(raw)

    raw = _confirmed_record().to_dict()
    raw["blog_url_raw"] = "https://example.com/" + "x" * 2048
    with pytest.raises(ValueError, match="exceeds 2048"):
        UserRecord.from_dict(raw)


def test_unconfirmed_unknown_record_cannot_hide_a_fingerprint() -> None:
    record = UserRecord(
        id=1,
        first_checked_at="2026-08-30T00:00:00Z",
        last_checked_at="2026-08-30T00:00:00Z",
        last_check_result="network_error",
        consecutive_failures=1,
        parser_version="test",
        source_fingerprint="a" * 64,
    )

    assert any(issue.code == "fingerprint" for issue in validate_user_record(record).errors)
    with pytest.raises(ValueError, match="cannot retain source_fingerprint"):
        UserRecord.from_dict(record.to_dict())


def test_cli_rejects_unknown_field_nested_in_pending_observation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = RepositoryStore(tmp_path)
    store.upsert_users([_confirmed_record()])
    store.rebuild_manifest(
        generated_at="2026-08-30T00:00:00Z",
        initialization_status="in_progress",
        phase="bootstrap",
    )
    store.save_state(CrawlerState(next_id=2, highest_attempted_id=1, highest_confirmed_user_id=1))
    (tmp_path / "README.md").write_text("# Test\n", encoding="utf-8")

    shard = store.shard_path(1)
    raw = json.loads(shard.read_text(encoding="utf-8"))
    raw["pending_observation"] = PendingObservation(
        profile_status="public",
        blog_status="present",
        blog_url="https://candidate.example/",
        blog_url_raw="https://candidate.example/",
        first_observed_at="2026-08-31T00:00:00Z",
        last_observed_at="2026-08-31T00:00:00Z",
        confirmations=1,
        parser_version="test",
        source_fingerprint=semantic_profile_fingerprint(
            "public", "present", "https://candidate.example/"
        ),
    ).to_dict()
    raw["pending_observation"]["raw_html"] = "<secret>"  # type: ignore[index]
    shard.write_text(canonical_json(raw) + "\n", encoding="utf-8")

    assert cli.main(["--root", str(tmp_path), "validate"]) == cli.EXIT_INVALID_DATA
    assert "unknown field(s): raw_html" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("relative_path", "extra_field"),
    [
        ("data/manifest.json", "email"),
        ("state/crawler.json", "student_id"),
    ],
)
def test_store_rejects_unknown_manifest_and_state_fields(
    tmp_path: Path,
    relative_path: str,
    extra_field: str,
) -> None:
    store = RepositoryStore(tmp_path)
    store.write_manifest(Manifest())
    store.save_state(CrawlerState())
    path = tmp_path / relative_path
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw[extra_field] = "must not be silently discarded"
    path.write_text(json.dumps(raw), encoding="utf-8")

    loader = store.load_manifest if "manifest" in relative_path else store.load_state
    with pytest.raises(DatasetCorruptionError, match=extra_field):
        loader()


def test_dataset_rejects_state_frontier_without_canonical_rows(tmp_path: Path) -> None:
    store = RepositoryStore(tmp_path)
    store.write_manifest(Manifest())
    store.save_state(CrawlerState(next_id=1, highest_attempted_id=999))

    codes = {issue.code for issue in validate_dataset(store).errors}
    assert "state_dataset_mismatch" in codes
    assert "state_manifest_mismatch" in codes


def test_dataset_rejects_a_missing_row_even_if_summaries_are_adjusted(tmp_path: Path) -> None:
    store = RepositoryStore(tmp_path)
    store.upsert_users([_confirmed_record(1), _confirmed_record(2), _confirmed_record(3)])
    shard = store.shard_path(2)
    retained = [
        line
        for line in shard.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["id"] != 2
    ]
    shard.write_text("\n".join(retained) + "\n", encoding="utf-8")
    store.write_manifest(
        Manifest(
            generated_at="2026-08-30T00:00:00Z",
            updated_at="2026-08-30T00:00:00Z",
            initialization_status="in_progress",
            record_count=2,
            highest_attempted_id=3,
            highest_confirmed_user_id=3,
            highest_existing_user_id=3,
            blog_count=2,
            profile_status_counts={"public": 2, "hidden": 0, "missing": 0, "unknown": 0},
        )
    )
    store.save_state(CrawlerState(next_id=4, highest_attempted_id=3, highest_confirmed_user_id=3))

    report = validate_dataset(store)
    assert any(issue.code == "record_coverage" for issue in report.errors)


@pytest.mark.parametrize(
    "state",
    [
        CrawlerState(
            phase="boundary_confirmation",
            next_id=1,
            highest_attempted_id=5,
            boundary_candidate_start=None,
            boundary_confirmation_pending=False,
        ),
        CrawlerState(
            phase="maintenance",
            next_id=1,
            highest_attempted_id=5,
            boundary_candidate_start=1,
            boundary_confirmation_pending=True,
        ),
    ],
)
def test_state_phase_rejects_missing_or_stale_boundary_fields(state: CrawlerState) -> None:
    assert any(issue.code == "state_boundary" for issue in validate_state(state).errors)
