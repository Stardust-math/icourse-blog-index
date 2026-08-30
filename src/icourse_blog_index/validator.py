"""Semantic validation for canonical data, crawler state, and summaries."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .models import (
    BlogStatus,
    CheckResult,
    CrawlPhase,
    CrawlerState,
    Manifest,
    ProfileStatus,
    USER_RECORD_FIELDS,
    UserRecord,
)
from .utils import is_public_http_url, parse_utc, semantic_profile_fingerprint


# Backwards-compatible public name used by tests and downstream checks.  The
# authoritative set lives beside ``UserRecord.from_dict`` so readers cannot
# bypass it by omitting the validator layer.
USER_FIELDS = USER_RECORD_FIELDS
SENSITIVE_OR_OUT_OF_SCOPE_FIELDS = frozenset(
    {
        "avatar",
        "bio",
        "cookie",
        "description",
        "display_name",
        "email",
        "followers",
        "following",
        "name",
        "nickname",
        "raw_html",
        "reviews",
        "student_id",
        "username",
    }
)
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_PARSER_VERSION_RE = re.compile(r"^[A-Za-z0-9._+\-]{1,100}$")


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    severity: str
    code: str
    message: str
    path: str = ""
    user_id: int | None = None

    def __str__(self) -> str:
        location = self.path or (f"user {self.user_id}" if self.user_id is not None else "dataset")
        return f"{self.severity}: {location}: {self.message} [{self.code}]"


@dataclass(frozen=True, slots=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")

    @property
    def valid(self) -> bool:
        return not self.errors

    def extend(self, other: "ValidationReport") -> "ValidationReport":
        return ValidationReport(self.issues + other.issues)

    def raise_for_errors(self) -> None:
        if self.errors:
            rendered = "\n".join(str(issue) for issue in self.errors)
            raise ValueError(f"validation failed:\n{rendered}")


def _error(
    code: str, message: str, *, path: str = "", user_id: int | None = None
) -> ValidationIssue:
    return ValidationIssue("error", code, message, path, user_id)


def _warning(
    code: str,
    message: str,
    *,
    path: str = "",
    user_id: int | None = None,
) -> ValidationIssue:
    return ValidationIssue("warning", code, message, path, user_id)


def validate_record_dict(value: Mapping[str, Any], *, path: str = "") -> ValidationReport:
    """Validate field scope before parsing a raw JSON object."""

    issues: list[ValidationIssue] = []
    missing = USER_FIELDS.difference(value)
    if missing:
        issues.append(
            _error(
                "missing_fields",
                "missing canonical fields: " + ", ".join(sorted(missing)),
                path=path,
            )
        )
    unknown = set(value).difference(USER_FIELDS)
    for name in sorted(unknown):
        code = (
            "out_of_scope_personal_data"
            if name.lower() in SENSITIVE_OR_OUT_OF_SCOPE_FIELDS
            else "unknown_field"
        )
        issues.append(
            _error(code, f"field {name!r} is not part of the canonical schema", path=path)
        )
    if issues:
        return ValidationReport(tuple(issues))
    try:
        record = UserRecord.from_dict(value)
    except (KeyError, TypeError, ValueError) as exc:
        return ValidationReport((_error("invalid_record", str(exc), path=path),))
    return validate_user_record(record, path=path)


def validate_user_record(record: UserRecord, *, path: str = "") -> ValidationReport:
    issues: list[ValidationIssue] = []
    uid = record.id

    expected_profile_url = f"https://icourse.club/user/{uid}"
    if record.profile_url != expected_profile_url:
        issues.append(
            _error(
                "profile_url",
                f"profile_url must be {expected_profile_url!r}",
                path=path,
                user_id=uid,
            )
        )

    if record.profile_status is ProfileStatus.PUBLIC:
        if record.blog_status is BlogStatus.PRESENT and record.blog_url is None:
            issues.append(
                _error("missing_blog_url", "present blog requires blog_url", path=path, user_id=uid)
            )
        if record.blog_status is BlogStatus.ABSENT and (
            record.blog_url is not None or record.blog_url_raw is not None
        ):
            issues.append(
                _error("absent_blog_url", "absent blog cannot retain a URL", path=path, user_id=uid)
            )
    elif record.profile_status in {
        ProfileStatus.HIDDEN,
        ProfileStatus.MISSING,
        ProfileStatus.UNKNOWN,
    }:
        if record.blog_status is not BlogStatus.UNKNOWN:
            issues.append(
                _error(
                    "profile_blog_status",
                    f"{record.profile_status.value} profile must have unknown blog status",
                    path=path,
                    user_id=uid,
                )
            )
        if record.blog_url is not None or record.blog_url_raw is not None:
            issues.append(
                _error(
                    "profile_blog_url",
                    f"{record.profile_status.value} profile cannot expose a confirmed blog URL",
                    path=path,
                    user_id=uid,
                )
            )

    if record.blog_url is not None and not is_public_http_url(record.blog_url):
        issues.append(
            _error("blog_url", "blog_url must be a public HTTP(S) URL", path=path, user_id=uid)
        )
    for field_name in ("blog_url", "blog_url_raw"):
        value = getattr(record, field_name)
        if value is not None and len(value) > 2048:
            issues.append(
                _error(
                    "blog_url_length",
                    f"{field_name} exceeds 2048 characters",
                    path=path,
                    user_id=uid,
                )
            )

    if record.last_check_result is CheckResult.OK and record.consecutive_failures != 0:
        issues.append(
            _error(
                "failure_count",
                "successful check must reset failures to zero",
                path=path,
                user_id=uid,
            )
        )
    if record.last_check_result is not CheckResult.OK and record.consecutive_failures < 1:
        issues.append(
            _error(
                "failure_count",
                "failed check must have a positive failure count",
                path=path,
                user_id=uid,
            )
        )
    if record.first_checked_at is None or record.last_checked_at is None:
        issues.append(
            _error(
                "attempt_timestamps",
                "every attempted ID needs first/last check times",
                path=path,
                user_id=uid,
            )
        )
    else:
        if parse_utc(record.first_checked_at) > parse_utc(record.last_checked_at):
            issues.append(
                _error(
                    "timestamp_order",
                    "first_checked_at is after last_checked_at",
                    path=path,
                    user_id=uid,
                )
            )

    if record.last_confirmed_at is not None:
        if record.last_checked_at is not None and parse_utc(record.last_confirmed_at) > parse_utc(
            record.last_checked_at
        ):
            issues.append(
                _error(
                    "timestamp_order",
                    "last_confirmed_at is after last_checked_at",
                    path=path,
                    user_id=uid,
                )
            )
        if record.source_fingerprint is None:
            issues.append(
                _error(
                    "fingerprint",
                    "confirmed state requires source_fingerprint",
                    path=path,
                    user_id=uid,
                )
            )
        else:
            expected = semantic_profile_fingerprint(
                record.profile_status.value,
                record.blog_status.value,
                record.blog_url,
            )
            if record.source_fingerprint != expected:
                issues.append(
                    _error(
                        "fingerprint",
                        "source_fingerprint does not match confirmed fields",
                        path=path,
                        user_id=uid,
                    )
                )
    elif record.profile_status is not ProfileStatus.UNKNOWN:
        issues.append(
            _error(
                "unconfirmed_state",
                "non-unknown state requires last_confirmed_at",
                path=path,
                user_id=uid,
            )
        )
    elif record.pending_observation is None and record.source_fingerprint is not None:
        issues.append(
            _error(
                "fingerprint",
                "unconfirmed record without a pending observation must have null fingerprint",
                path=path,
                user_id=uid,
            )
        )
    if record.source_fingerprint is not None and not _FINGERPRINT_RE.fullmatch(
        record.source_fingerprint
    ):
        issues.append(
            _error(
                "fingerprint",
                "source_fingerprint must be 64 lowercase hexadecimal characters",
                path=path,
                user_id=uid,
            )
        )

    for name in ("profile_changed_at", "blog_changed_at"):
        value = getattr(record, name)
        if value is not None and record.last_confirmed_at is not None:
            if parse_utc(value) > parse_utc(record.last_confirmed_at):
                issues.append(
                    _error(
                        "timestamp_order",
                        f"{name} is after last_confirmed_at",
                        path=path,
                        user_id=uid,
                    )
                )

    pending = record.pending_observation
    if pending is not None:
        if record.last_checked_at is not None and parse_utc(pending.last_observed_at) > parse_utc(
            record.last_checked_at
        ):
            issues.append(
                _error(
                    "pending_timestamp",
                    "pending observation is newer than last check",
                    path=path,
                    user_id=uid,
                )
            )
        if parse_utc(pending.first_observed_at) > parse_utc(pending.last_observed_at):
            issues.append(
                _error(
                    "pending_timestamp",
                    "pending first observation is after last observation",
                    path=path,
                    user_id=uid,
                )
            )
        expected = semantic_profile_fingerprint(
            pending.profile_status.value,
            pending.blog_status.value,
            pending.blog_url,
        )
        if pending.source_fingerprint != expected:
            issues.append(
                _error(
                    "pending_fingerprint",
                    "pending fingerprint does not match candidate",
                    path=path,
                    user_id=uid,
                )
            )
        if not _FINGERPRINT_RE.fullmatch(pending.source_fingerprint):
            issues.append(
                _error(
                    "pending_fingerprint",
                    "pending fingerprint must be 64 lowercase hexadecimal characters",
                    path=path,
                    user_id=uid,
                )
            )
        if not _PARSER_VERSION_RE.fullmatch(pending.parser_version):
            issues.append(
                _error(
                    "parser_version",
                    "pending parser_version contains unsafe characters or is too long",
                    path=path,
                    user_id=uid,
                )
            )
        for field_name in ("blog_url", "blog_url_raw"):
            value = getattr(pending, field_name)
            if value is not None and len(value) > 2048:
                issues.append(
                    _error(
                        "blog_url_length",
                        f"pending {field_name} exceeds 2048 characters",
                        path=path,
                        user_id=uid,
                    )
                )
        if pending.blog_status is BlogStatus.PRESENT and pending.blog_url is None:
            issues.append(
                _error(
                    "pending_blog", "pending present blog requires a URL", path=path, user_id=uid
                )
            )

    if record.http_status is not None and not 100 <= record.http_status <= 599:
        issues.append(
            _error("http_status", "HTTP status must be between 100 and 599", path=path, user_id=uid)
        )
    if not _PARSER_VERSION_RE.fullmatch(record.parser_version):
        issues.append(
            _error(
                "parser_version",
                "parser_version contains unsafe characters or is too long",
                path=path,
                user_id=uid,
            )
        )
    return ValidationReport(tuple(issues))


def validate_records(records: Iterable[UserRecord]) -> ValidationReport:
    issues: list[ValidationIssue] = []
    previous_id = 0
    seen: set[int] = set()
    for record in records:
        if record.id in seen:
            issues.append(
                _error("duplicate_id", f"duplicate user ID {record.id}", user_id=record.id)
            )
        if record.id <= previous_id:
            issues.append(
                _error("record_order", "records must be strictly ID-sorted", user_id=record.id)
            )
        seen.add(record.id)
        previous_id = record.id
        issues.extend(validate_user_record(record).issues)
    return ValidationReport(tuple(issues))


def validate_record_coverage(records: Iterable[UserRecord]) -> ValidationReport:
    """Require one canonical row for every attempted numeric ID from 1 onward."""

    issues: list[ValidationIssue] = []
    expected = 1
    for record in sorted(records, key=lambda item: item.id):
        if record.id < expected:
            continue  # duplicate/order errors are reported by validate_records
        if record.id != expected:
            end = record.id - 1
            missing = str(expected) if expected == end else f"{expected}-{end}"
            issues.append(
                _error(
                    "record_coverage",
                    f"canonical records skip attempted ID range {missing}",
                    path="data/users",
                )
            )
        expected = record.id + 1
    return ValidationReport(tuple(issues))


def validate_manifest(manifest: Manifest, records: Iterable[UserRecord]) -> ValidationReport:
    materialized = list(records)
    issues: list[ValidationIssue] = []
    counts = {status.value: 0 for status in ProfileStatus}
    for record in materialized:
        counts[record.profile_status.value] += 1
    highest_attempted = max((record.id for record in materialized), default=0)
    highest_existing = max(
        (record.id for record in materialized if record.is_confirmed_existing_user),
        default=0,
    )
    blog_count = sum(
        record.profile_status is ProfileStatus.PUBLIC
        and record.blog_status is BlogStatus.PRESENT
        and record.blog_url is not None
        for record in materialized
    )
    expected: dict[str, Any] = {
        "record_count": len(materialized),
        "highest_attempted_id": highest_attempted,
        "highest_confirmed_user_id": highest_existing,
        "highest_existing_user_id": highest_existing,
        "blog_count": blog_count,
    }
    for field, value in expected.items():
        if getattr(manifest, field) != value:
            issues.append(
                _error(
                    "manifest_mismatch",
                    f"{field}={getattr(manifest, field)!r}, expected {value!r}",
                    path="data/manifest.json",
                )
            )
    if dict(manifest.profile_status_counts) != counts:
        issues.append(
            _error(
                "manifest_mismatch",
                f"profile_status_counts={dict(manifest.profile_status_counts)!r}, expected {counts!r}",
                path="data/manifest.json",
            )
        )
    return ValidationReport(tuple(issues))


def validate_state(
    state: CrawlerState,
    records: Iterable[UserRecord] | None = None,
    manifest: Manifest | None = None,
) -> ValidationReport:
    issues: list[ValidationIssue] = []
    if state.highest_confirmed_user_id > state.highest_attempted_id:
        issues.append(
            _error(
                "state_frontier",
                "highest_confirmed_user_id cannot exceed highest_attempted_id",
                path="state/crawler.json",
            )
        )
    if state.boundary_confirmation_pending and state.boundary_candidate_start is None:
        issues.append(
            _error(
                "state_boundary",
                "pending boundary confirmation requires boundary_candidate_start",
                path="state/crawler.json",
            )
        )
    if state.phase.value == "paused" and state.paused_from_phase is None:
        issues.append(
            _error(
                "state_pause",
                "paused state requires paused_from_phase for an explicit resume",
                path="state/crawler.json",
            )
        )
    if state.phase.value != "paused" and state.paused_from_phase is not None:
        issues.append(
            _error(
                "state_pause",
                "paused_from_phase must be null while the crawler is running",
                path="state/crawler.json",
            )
        )
    if state.next_id <= state.highest_confirmed_user_id:
        issues.append(
            _warning(
                "state_next_id",
                "next_id is not beyond the confirmed frontier; this is valid only for a deliberate recheck",
                path="state/crawler.json",
            )
        )
    boundary_active = state.phase is CrawlPhase.BOUNDARY_CONFIRMATION or (
        state.phase is CrawlPhase.PAUSED
        and state.paused_from_phase is CrawlPhase.BOUNDARY_CONFIRMATION
    )
    if boundary_active:
        if state.boundary_candidate_start is None or not state.boundary_confirmation_pending:
            issues.append(
                _error(
                    "state_boundary",
                    "boundary confirmation requires a candidate and pending=true",
                    path="state/crawler.json",
                )
            )
    elif state.boundary_candidate_start is not None or state.boundary_confirmation_pending:
        issues.append(
            _error(
                "state_boundary",
                "non-boundary state cannot retain boundary candidate fields",
                path="state/crawler.json",
            )
        )
    if state.boundary_candidate_start is not None:
        if state.boundary_candidate_start < 1:
            issues.append(
                _error(
                    "state_boundary_range",
                    "boundary_candidate_start must be positive",
                    path="state/crawler.json",
                )
            )
        if state.boundary_candidate_start > state.highest_attempted_id:
            issues.append(
                _error(
                    "state_boundary_range",
                    "boundary candidate cannot exceed the highest attempted ID",
                    path="state/crawler.json",
                )
            )

    materialized = list(records) if records is not None else None
    if materialized is not None:
        canonical_max = max((record.id for record in materialized), default=0)
        canonical_existing = max(
            (record.id for record in materialized if record.is_confirmed_existing_user),
            default=0,
        )
        if state.highest_attempted_id != canonical_max:
            issues.append(
                _error(
                    "state_dataset_mismatch",
                    f"highest_attempted_id={state.highest_attempted_id}, expected {canonical_max}",
                    path="state/crawler.json",
                )
            )
        if state.highest_confirmed_user_id != canonical_existing:
            issues.append(
                _error(
                    "state_dataset_mismatch",
                    "highest_confirmed_user_id="
                    f"{state.highest_confirmed_user_id}, expected {canonical_existing}",
                    path="state/crawler.json",
                )
            )
        if state.next_id > canonical_max + 1:
            issues.append(
                _error(
                    "state_next_id_gap",
                    f"next_id={state.next_id} skips unrecorded IDs beyond {canonical_max}",
                    path="state/crawler.json",
                )
            )
    if manifest is not None:
        if state.highest_attempted_id != manifest.highest_attempted_id:
            issues.append(
                _error(
                    "state_manifest_mismatch",
                    "state and manifest highest_attempted_id differ",
                    path="state/crawler.json",
                )
            )
        if state.highest_confirmed_user_id != manifest.highest_confirmed_user_id:
            issues.append(
                _error(
                    "state_manifest_mismatch",
                    "state and manifest highest_confirmed_user_id differ",
                    path="state/crawler.json",
                )
            )
        if manifest.phase != state.phase.value:
            issues.append(
                _error(
                    "state_manifest_mismatch",
                    f"manifest phase {manifest.phase!r} differs from state phase {state.phase.value!r}",
                    path="state/crawler.json",
                )
            )
        initialization = manifest.initialization_status.value
        allowed_statuses = {
            CrawlPhase.BOOTSTRAP: {"not_started", "in_progress"},
            CrawlPhase.BOUNDARY_CONFIRMATION: {"in_progress"},
            CrawlPhase.MAINTENANCE: {"complete"},
            CrawlPhase.PAUSED: {"paused"},
        }[state.phase]
        if initialization not in allowed_statuses:
            issues.append(
                _error(
                    "state_manifest_status",
                    f"initialization_status {initialization!r} is invalid for phase "
                    f"{state.phase.value!r}",
                    path="data/manifest.json",
                )
            )
        if initialization == "not_started" and (
            state.highest_attempted_id != 0 or (materialized is not None and materialized)
        ):
            issues.append(
                _error(
                    "state_manifest_status",
                    "not_started status requires an empty, unattempted dataset",
                    path="data/manifest.json",
                )
            )
    return ValidationReport(tuple(issues))


def validate_dataset(root_or_store: str | Path | Any) -> ValidationReport:
    """Validate shards plus their derived manifest and crawler state."""

    from .storage import DatasetCorruptionError, RepositoryStore

    store = (
        root_or_store
        if isinstance(root_or_store, RepositoryStore)
        else RepositoryStore(root_or_store)
    )
    try:
        store.validate_layout()
        records = list(store.iter_users())
        manifest = store.load_manifest()
        state = store.load_state()
        changes = list(store.iter_changes())
        list(store.iter_runs())
        store.load_link_health()
    except (DatasetCorruptionError, OSError, ValueError) as exc:
        return ValidationReport((_error("dataset_read", str(exc)),))
    report = validate_records(records)
    report = report.extend(validate_record_coverage(records))
    report = report.extend(validate_manifest(manifest, records))
    report = report.extend(validate_state(state, records, manifest))
    user_ids = {record.id for record in records}
    orphaned_events = sorted({event.user_id for event in changes}.difference(user_ids))
    if orphaned_events:
        report = report.extend(
            ValidationReport(
                (
                    _error(
                        "change_user_missing",
                        "change events reference absent user IDs: "
                        + ", ".join(map(str, orphaned_events[:20])),
                        path="data/changes",
                    ),
                )
            )
        )
    return report


__all__ = [
    "USER_FIELDS",
    "ValidationIssue",
    "ValidationReport",
    "validate_dataset",
    "validate_manifest",
    "validate_record_dict",
    "validate_record_coverage",
    "validate_records",
    "validate_state",
    "validate_user_record",
]
