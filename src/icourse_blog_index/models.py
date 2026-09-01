"""Canonical data models and observation merge semantics.

The important distinction in this module is between an *attempt* and a
*confirmed observation*.  Network failures and unrecognized HTML are recorded
as attempts, but they never erase a previously confirmed profile or blog.
Potential profile/blog changes are held in ``pending_observation`` until a
second independent successful observation agrees.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
import re
from typing import Any

from .utils import (
    format_utc,
    json_fingerprint,
    normalize_http_url,
    parse_utc,
    sanitize_error,
    semantic_profile_fingerprint,
)


PENDING_OBSERVATION_FIELDS = frozenset(
    {
        "blog_status",
        "blog_url",
        "blog_url_raw",
        "confirmations",
        "first_observed_at",
        "http_status",
        "last_observed_at",
        "parser_version",
        "profile_status",
        "source_fingerprint",
    }
)
USER_RECORD_FIELDS = frozenset(
    {
        "blog_changed_at",
        "blog_status",
        "blog_url",
        "blog_url_raw",
        "consecutive_failures",
        "first_checked_at",
        "http_status",
        "id",
        "last_check_result",
        "last_checked_at",
        "last_confirmed_at",
        "last_error",
        "parser_version",
        "pending_observation",
        "profile_changed_at",
        "profile_status",
        "profile_url",
        "source_fingerprint",
    }
)
CHANGE_EVENT_FIELDS = frozenset(
    {"changed_at", "event_id", "kind", "new_value", "old_value", "run_id", "user_id"}
)
MANIFEST_FIELDS = frozenset(
    {
        "blog_count",
        "generated_at",
        "highest_attempted_id",
        "highest_confirmed_user_id",
        "highest_existing_user_id",
        "initialization_status",
        "last_successful_run_at",
        "phase",
        "profile_status_counts",
        "record_count",
        "schema_version",
        "updated_at",
    }
)
CRAWLER_STATE_FIELDS = frozenset(
    {
        "boundary_candidate_start",
        "boundary_confirmation_pending",
        "consecutive_missing_after_frontier",
        "highest_attempted_id",
        "highest_confirmed_user_id",
        "last_run_id",
        "maintenance_cursor",
        "next_id",
        "paused_from_phase",
        "phase",
        "schema_version",
        "updated_at",
    }
)
RUN_RECORD_FIELDS = frozenset(
    {
        "attempted",
        "changed",
        "confirmed",
        "crawler_version",
        "end_id",
        "errors",
        "finished_at",
        "mode",
        "rate_limited",
        "run_id",
        "start_id",
        "started_at",
        "stopped_reason",
    }
)
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_PARSER_VERSION_RE = re.compile(r"^[A-Za-z0-9._+\-]{1,100}$")


def _require_exact_fields(
    value: Mapping[str, Any],
    allowed: frozenset[str],
    context: str,
) -> None:
    """Reject schema drift before values can be silently discarded.

    This belongs in the model layer rather than the validator so every reader,
    including the CLI and storage layer, gets the same privacy boundary without
    introducing an import cycle.
    """

    unknown = set(value).difference(allowed)
    missing = allowed.difference(value)
    details: list[str] = []
    if unknown:
        details.append("unknown field(s): " + ", ".join(sorted(map(str, unknown))))
    if missing:
        details.append("missing field(s): " + ", ".join(sorted(missing)))
    if details:
        raise ValueError(f"{context} has " + "; ".join(details))


def _require_json_types(
    value: Mapping[str, Any],
    expected: Mapping[str, type[Any] | tuple[type[Any], ...]],
    context: str,
) -> None:
    """Validate raw JSON types before convenience coercions can repair them."""

    for field_name, accepted in expected.items():
        choices = accepted if isinstance(accepted, tuple) else (accepted,)
        actual = value[field_name]
        valid = any(
            type(actual) is choice if choice in {bool, int} else isinstance(actual, choice)
            for choice in choices
        )
        if not valid:
            labels = [
                "object"
                if choice is Mapping
                else "null"
                if choice is type(None)
                else choice.__name__
                for choice in choices
            ]
            raise ValueError(
                f"{context} field {field_name!r} must be "
                f"{' or '.join(labels)}, got {type(actual).__name__}"
            )


def _require_persisted_parser_version(value: str, context: str) -> None:
    if not _PARSER_VERSION_RE.fullmatch(value):
        raise ValueError(f"{context} parser_version must use 1-100 safe ASCII version characters")


def _require_persisted_fingerprint(value: str | None, context: str) -> None:
    if value is not None and not _FINGERPRINT_RE.fullmatch(value):
        raise ValueError(f"{context} source_fingerprint must be 64 lowercase hex characters")


def _require_url_lengths(value: Mapping[str, Any], context: str) -> None:
    for field_name in ("blog_url", "blog_url_raw"):
        item = value[field_name]
        if item is not None and len(item) > 2048:
            raise ValueError(f"{context} {field_name} exceeds 2048 characters")


class StringEnum(str, Enum):
    """A string enum that renders naturally in logs and JSON."""

    def __str__(self) -> str:
        return self.value


class ProfileStatus(StringEnum):
    PUBLIC = "public"
    HIDDEN = "hidden"
    MISSING = "missing"
    UNKNOWN = "unknown"


class BlogStatus(StringEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class CheckResult(StringEnum):
    OK = "ok"
    HTTP_ERROR = "http_error"
    RATE_LIMITED = "rate_limited"
    BLOCKED_OR_CHALLENGE = "blocked_or_challenge"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    PARSE_ERROR = "parse_error"
    SUSPECTED_STALE = "suspected_stale"


class ChangeKind(StringEnum):
    PROFILE_STATUS = "profile_status"
    BLOG_ADDED = "blog_added"
    BLOG_REMOVED = "blog_removed"
    BLOG_CHANGED = "blog_changed"


class InitializationStatus(StringEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    PAUSED = "paused"


class CrawlPhase(StringEnum):
    BOOTSTRAP = "bootstrap"
    BOUNDARY_CONFIRMATION = "boundary_confirmation"
    MAINTENANCE = "maintenance"
    PAUSED = "paused"


def _enum(enum_type: type[StringEnum], value: StringEnum | str) -> StringEnum:
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as exc:
        choices = ", ".join(item.value for item in enum_type)
        raise ValueError(f"expected one of {{{choices}}}, got {value!r}") from exc


def _timestamp(value: str | None) -> str | None:
    return None if value is None else format_utc(value)


@dataclass(frozen=True, slots=True)
class PendingObservation:
    """A successful semantic observation awaiting independent confirmation."""

    profile_status: ProfileStatus | str
    blog_status: BlogStatus | str
    blog_url: str | None
    blog_url_raw: str | None
    first_observed_at: str
    last_observed_at: str
    confirmations: int
    parser_version: str
    source_fingerprint: str
    http_status: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_status", _enum(ProfileStatus, self.profile_status))
        object.__setattr__(self, "blog_status", _enum(BlogStatus, self.blog_status))
        object.__setattr__(self, "first_observed_at", format_utc(self.first_observed_at))
        object.__setattr__(self, "last_observed_at", format_utc(self.last_observed_at))
        if self.blog_url is not None:
            object.__setattr__(self, "blog_url", normalize_http_url(self.blog_url))
        if self.blog_url_raw is not None:
            object.__setattr__(self, "blog_url_raw", self.blog_url_raw.strip() or None)
        if self.confirmations < 1:
            raise ValueError("pending confirmations must be positive")
        if not self.source_fingerprint:
            raise ValueError("pending observation requires a source fingerprint")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PendingObservation":
        _require_exact_fields(value, PENDING_OBSERVATION_FIELDS, "pending observation")
        _require_json_types(
            value,
            {
                "profile_status": str,
                "blog_status": str,
                "blog_url": (str, type(None)),
                "blog_url_raw": (str, type(None)),
                "first_observed_at": str,
                "last_observed_at": str,
                "confirmations": int,
                "parser_version": str,
                "source_fingerprint": str,
                "http_status": (int, type(None)),
            },
            "pending observation",
        )
        _require_persisted_parser_version(value["parser_version"], "pending observation")
        _require_persisted_fingerprint(value["source_fingerprint"], "pending observation")
        _require_url_lengths(value, "pending observation")
        return cls(
            profile_status=value["profile_status"],
            blog_status=value["blog_status"],
            blog_url=value.get("blog_url"),
            blog_url_raw=value.get("blog_url_raw"),
            first_observed_at=value["first_observed_at"],
            last_observed_at=value["last_observed_at"],
            confirmations=int(value["confirmations"]),
            parser_version=str(value.get("parser_version", "unknown")),
            source_fingerprint=str(value["source_fingerprint"]),
            http_status=value.get("http_status"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "blog_status": self.blog_status.value,
            "blog_url": self.blog_url,
            "blog_url_raw": self.blog_url_raw,
            "confirmations": self.confirmations,
            "first_observed_at": self.first_observed_at,
            "http_status": self.http_status,
            "last_observed_at": self.last_observed_at,
            "parser_version": self.parser_version,
            "profile_status": self.profile_status.value,
            "source_fingerprint": self.source_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class Observation:
    """The result of one direct request to an icourse.club user page."""

    user_id: int
    observed_at: str
    profile_status: ProfileStatus | str = ProfileStatus.UNKNOWN
    blog_status: BlogStatus | str = BlogStatus.UNKNOWN
    blog_url_raw: str | None = None
    blog_url: str | None = None
    check_result: CheckResult | str = CheckResult.OK
    http_status: int | None = None
    parser_version: str = "unknown"
    source_fingerprint: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.user_id, bool) or not isinstance(self.user_id, int) or self.user_id < 1:
            raise ValueError("user_id must be a positive integer")
        object.__setattr__(self, "observed_at", format_utc(self.observed_at))
        object.__setattr__(self, "profile_status", _enum(ProfileStatus, self.profile_status))
        object.__setattr__(self, "blog_status", _enum(BlogStatus, self.blog_status))
        object.__setattr__(self, "check_result", _enum(CheckResult, self.check_result))
        if self.blog_url is not None:
            object.__setattr__(self, "blog_url", normalize_http_url(self.blog_url))
        if self.blog_url_raw is not None:
            object.__setattr__(self, "blog_url_raw", self.blog_url_raw.strip() or None)
        object.__setattr__(self, "error", sanitize_error(self.error))
        if not _PARSER_VERSION_RE.fullmatch(self.parser_version):
            raise ValueError("parser_version must use 1-100 safe ASCII version characters")
        for field_name in ("blog_url", "blog_url_raw"):
            value = getattr(self, field_name)
            if value is not None and len(value) > 2048:
                raise ValueError(f"{field_name} exceeds 2048 characters")
        if self.check_result is CheckResult.OK:
            if self.profile_status is ProfileStatus.PUBLIC:
                if self.blog_status not in {BlogStatus.PRESENT, BlogStatus.ABSENT}:
                    raise ValueError("a successful public profile needs present/absent blog status")
                if self.blog_status is BlogStatus.PRESENT and self.blog_url is None:
                    raise ValueError("a present blog requires blog_url")
                if self.blog_status is BlogStatus.ABSENT and (
                    self.blog_url is not None or self.blog_url_raw is not None
                ):
                    raise ValueError("an absent blog cannot contain a URL")
            elif self.profile_status in {ProfileStatus.HIDDEN, ProfileStatus.MISSING}:
                if self.blog_status is not BlogStatus.UNKNOWN or self.blog_url is not None:
                    raise ValueError("hidden/missing profiles require unknown blog state")
            else:
                raise ValueError("an unknown profile cannot be a successful observation")
            # Recompute after canonical URL normalization.  Parser callers may
            # have fingerprinted the equivalent empty-root spelling first.
            object.__setattr__(
                self,
                "source_fingerprint",
                semantic_profile_fingerprint(
                    self.profile_status.value,
                    self.blog_status.value,
                    self.blog_url,
                ),
            )

    @property
    def successful(self) -> bool:
        return self.check_result is CheckResult.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "blog_status": self.blog_status.value,
            "blog_url": self.blog_url,
            "blog_url_raw": self.blog_url_raw,
            "check_result": self.check_result.value,
            "error": self.error,
            "http_status": self.http_status,
            "observed_at": self.observed_at,
            "parser_version": self.parser_version,
            "profile_status": self.profile_status.value,
            "source_fingerprint": self.source_fingerprint,
            "user_id": self.user_id,
        }


@dataclass(frozen=True, slots=True)
class UserRecord:
    """The canonical current record for one attempted numeric user ID."""

    id: int
    profile_url: str = ""
    profile_status: ProfileStatus | str = ProfileStatus.UNKNOWN
    blog_status: BlogStatus | str = BlogStatus.UNKNOWN
    blog_url: str | None = None
    blog_url_raw: str | None = None
    first_checked_at: str | None = None
    last_checked_at: str | None = None
    last_confirmed_at: str | None = None
    profile_changed_at: str | None = None
    blog_changed_at: str | None = None
    last_check_result: CheckResult | str = CheckResult.NETWORK_ERROR
    consecutive_failures: int = 0
    parser_version: str = "unknown"
    source_fingerprint: str | None = None
    http_status: int | None = None
    last_error: str | None = None
    pending_observation: PendingObservation | Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.id, bool) or not isinstance(self.id, int) or self.id < 1:
            raise ValueError("id must be a positive integer")
        profile_url = self.profile_url or f"https://icourse.club/user/{self.id}"
        object.__setattr__(self, "profile_url", profile_url)
        object.__setattr__(self, "profile_status", _enum(ProfileStatus, self.profile_status))
        object.__setattr__(self, "blog_status", _enum(BlogStatus, self.blog_status))
        object.__setattr__(self, "last_check_result", _enum(CheckResult, self.last_check_result))
        if self.blog_url is not None:
            object.__setattr__(self, "blog_url", normalize_http_url(self.blog_url))
        if self.blog_url_raw is not None:
            object.__setattr__(self, "blog_url_raw", self.blog_url_raw.strip() or None)
        for name in (
            "first_checked_at",
            "last_checked_at",
            "last_confirmed_at",
            "profile_changed_at",
            "blog_changed_at",
        ):
            object.__setattr__(self, name, _timestamp(getattr(self, name)))
        if self.consecutive_failures < 0:
            raise ValueError("consecutive_failures cannot be negative")
        object.__setattr__(self, "last_error", sanitize_error(self.last_error))
        if isinstance(self.pending_observation, Mapping):
            object.__setattr__(
                self,
                "pending_observation",
                PendingObservation.from_dict(self.pending_observation),
            )
        elif self.pending_observation is not None and not isinstance(
            self.pending_observation, PendingObservation
        ):
            raise ValueError("pending_observation must be an object or null")

    @property
    def has_confirmed_state(self) -> bool:
        return self.last_confirmed_at is not None

    @property
    def is_confirmed_existing_user(self) -> bool:
        return self.has_confirmed_state and self.profile_status in {
            ProfileStatus.PUBLIC,
            ProfileStatus.HIDDEN,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "UserRecord":
        _require_exact_fields(value, USER_RECORD_FIELDS, "user record")
        _require_json_types(
            value,
            {
                "id": int,
                "profile_url": str,
                "profile_status": str,
                "blog_status": str,
                "blog_url": (str, type(None)),
                "blog_url_raw": (str, type(None)),
                "first_checked_at": (str, type(None)),
                "last_checked_at": (str, type(None)),
                "last_confirmed_at": (str, type(None)),
                "profile_changed_at": (str, type(None)),
                "blog_changed_at": (str, type(None)),
                "last_check_result": str,
                "consecutive_failures": int,
                "parser_version": str,
                "source_fingerprint": (str, type(None)),
                "http_status": (int, type(None)),
                "last_error": (str, type(None)),
                "pending_observation": (Mapping, type(None)),
            },
            "user record",
        )
        _require_persisted_parser_version(value["parser_version"], "user record")
        _require_persisted_fingerprint(value["source_fingerprint"], "user record")
        _require_url_lengths(value, "user record")
        if (
            value["last_confirmed_at"] is None
            and value["pending_observation"] is None
            and value["source_fingerprint"] is not None
        ):
            raise ValueError(
                "unconfirmed user record without a pending observation cannot retain "
                "source_fingerprint"
            )
        return cls(
            id=int(value["id"]),
            profile_url=str(value.get("profile_url", "")),
            profile_status=value.get("profile_status", ProfileStatus.UNKNOWN.value),
            blog_status=value.get("blog_status", BlogStatus.UNKNOWN.value),
            blog_url=value.get("blog_url"),
            blog_url_raw=value.get("blog_url_raw"),
            first_checked_at=value.get("first_checked_at"),
            last_checked_at=value.get("last_checked_at"),
            last_confirmed_at=value.get("last_confirmed_at"),
            profile_changed_at=value.get("profile_changed_at"),
            blog_changed_at=value.get("blog_changed_at"),
            last_check_result=value.get("last_check_result", CheckResult.NETWORK_ERROR.value),
            consecutive_failures=int(value.get("consecutive_failures", 0)),
            parser_version=str(value.get("parser_version", "unknown")),
            source_fingerprint=value.get("source_fingerprint"),
            http_status=value.get("http_status"),
            last_error=value.get("last_error"),
            pending_observation=value.get("pending_observation"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "blog_changed_at": self.blog_changed_at,
            "blog_status": self.blog_status.value,
            "blog_url": self.blog_url,
            "blog_url_raw": self.blog_url_raw,
            "consecutive_failures": self.consecutive_failures,
            "first_checked_at": self.first_checked_at,
            "http_status": self.http_status,
            "id": self.id,
            "last_check_result": self.last_check_result.value,
            "last_checked_at": self.last_checked_at,
            "last_confirmed_at": self.last_confirmed_at,
            "last_error": self.last_error,
            "parser_version": self.parser_version,
            "pending_observation": (
                self.pending_observation.to_dict() if self.pending_observation is not None else None
            ),
            "profile_changed_at": self.profile_changed_at,
            "profile_status": self.profile_status.value,
            "profile_url": self.profile_url,
            "source_fingerprint": self.source_fingerprint,
        }


def _validated_blog_snapshot(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"status", "url"}:
        raise ValueError(f"change event {field_name} must contain exactly status and url")
    if not isinstance(value["status"], str):
        raise ValueError(f"change event {field_name}.status must be a string")
    try:
        status = BlogStatus(value["status"])
    except ValueError as exc:
        raise ValueError(f"change event {field_name}.status is invalid") from exc
    url = value["url"]
    if url is not None and not isinstance(url, str):
        raise ValueError(f"change event {field_name}.url must be a string or null")
    if status is BlogStatus.PRESENT:
        if url is None:
            raise ValueError(f"change event {field_name} present status requires a URL")
        url = normalize_http_url(url)
    elif url is not None:
        raise ValueError(f"change event {field_name} non-present status requires null URL")
    return {"status": status.value, "url": url}


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    user_id: int
    changed_at: str
    kind: ChangeKind | str
    old_value: Any
    new_value: Any
    run_id: str | None = None
    event_id: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.user_id, bool) or not isinstance(self.user_id, int) or self.user_id < 1:
            raise ValueError("change event user_id must be a positive integer")
        if self.run_id is not None and (
            not isinstance(self.run_id, str) or not self.run_id.strip()
        ):
            raise ValueError("change event run_id must be a nonempty string or null")
        object.__setattr__(self, "changed_at", format_utc(self.changed_at))
        object.__setattr__(self, "kind", _enum(ChangeKind, self.kind))
        if self.kind is ChangeKind.PROFILE_STATUS:
            try:
                old_value = ProfileStatus(self.old_value).value
                new_value = ProfileStatus(self.new_value).value
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "profile status event values must be valid status strings"
                ) from exc
            if old_value == new_value:
                raise ValueError("profile status event must change its value")
        else:
            old_value = _validated_blog_snapshot(self.old_value, "old_value")
            new_value = _validated_blog_snapshot(self.new_value, "new_value")
            if old_value == new_value:
                raise ValueError("blog event must change its value")
            old_present = old_value["status"] == BlogStatus.PRESENT.value
            new_present = new_value["status"] == BlogStatus.PRESENT.value
            if self.kind is ChangeKind.BLOG_ADDED and (old_present or not new_present):
                raise ValueError("blog_added must transition from non-present to present")
            if self.kind is ChangeKind.BLOG_REMOVED and (
                not old_present or new_value["status"] != BlogStatus.ABSENT.value
            ):
                raise ValueError("blog_removed must transition from present to absent")
            if self.kind is ChangeKind.BLOG_CHANGED and (
                (not old_present and new_present)
                or (old_present and new_value["status"] == BlogStatus.ABSENT.value)
            ):
                raise ValueError("blog change kind does not match its transition")
        object.__setattr__(self, "old_value", old_value)
        object.__setattr__(self, "new_value", new_value)
        expected_event_id = json_fingerprint(
            {
                "changed_at": self.changed_at,
                "kind": self.kind.value,
                "new_value": self.new_value,
                "old_value": self.old_value,
                "user_id": self.user_id,
            }
        )[:24]
        if self.event_id is None:
            object.__setattr__(
                self,
                "event_id",
                expected_event_id,
            )
        elif self.event_id != expected_event_id:
            raise ValueError("change event_id does not match its canonical content")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ChangeEvent":
        _require_exact_fields(value, CHANGE_EVENT_FIELDS, "change event")
        _require_json_types(
            value,
            {
                "event_id": str,
                "user_id": int,
                "changed_at": str,
                "kind": str,
                "run_id": (str, type(None)),
            },
            "change event",
        )
        return cls(
            event_id=value.get("event_id"),
            user_id=int(value["user_id"]),
            changed_at=value["changed_at"],
            kind=value["kind"],
            old_value=value.get("old_value"),
            new_value=value.get("new_value"),
            run_id=value.get("run_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed_at": self.changed_at,
            "event_id": self.event_id,
            "kind": self.kind.value,
            "new_value": self.new_value,
            "old_value": self.old_value,
            "run_id": self.run_id,
            "user_id": self.user_id,
        }


@dataclass(frozen=True, slots=True)
class MergeResult:
    record: UserRecord
    changes: tuple[ChangeEvent, ...] = ()
    confirmed: bool = False
    pending: bool = False


def _semantic_tuple(record: UserRecord) -> tuple[str, str, str | None]:
    return record.profile_status.value, record.blog_status.value, record.blog_url


def _observation_tuple(observation: Observation) -> tuple[str, str, str | None]:
    return (
        observation.profile_status.value,
        observation.blog_status.value,
        observation.blog_url,
    )


def _pending_from(observation: Observation) -> PendingObservation:
    if observation.source_fingerprint is None:
        raise ValueError("a successful observation must have a source fingerprint")
    return PendingObservation(
        profile_status=observation.profile_status,
        blog_status=observation.blog_status,
        blog_url=observation.blog_url,
        blog_url_raw=observation.blog_url_raw,
        first_observed_at=observation.observed_at,
        last_observed_at=observation.observed_at,
        confirmations=1,
        parser_version=observation.parser_version,
        source_fingerprint=observation.source_fingerprint,
        http_status=observation.http_status,
    )


def _pending_matches(pending: PendingObservation, observation: Observation) -> bool:
    return (
        pending.source_fingerprint == observation.source_fingerprint
        and pending.profile_status is observation.profile_status
        and pending.blog_status is observation.blog_status
        and pending.blog_url == observation.blog_url
    )


def _new_record_from_failed(observation: Observation) -> UserRecord:
    return UserRecord(
        id=observation.user_id,
        first_checked_at=observation.observed_at,
        last_checked_at=observation.observed_at,
        last_check_result=observation.check_result,
        consecutive_failures=1,
        parser_version=observation.parser_version,
        http_status=observation.http_status,
        last_error=observation.error,
    )


def _change_events(
    previous: UserRecord,
    updated: UserRecord,
    *,
    changed_at: str,
    run_id: str | None,
) -> tuple[ChangeEvent, ...]:
    events: list[ChangeEvent] = []
    if previous.profile_status is not updated.profile_status:
        events.append(
            ChangeEvent(
                user_id=updated.id,
                changed_at=changed_at,
                kind=ChangeKind.PROFILE_STATUS,
                old_value=previous.profile_status.value,
                new_value=updated.profile_status.value,
                run_id=run_id,
            )
        )
    old_blog = (previous.blog_status.value, previous.blog_url)
    new_blog = (updated.blog_status.value, updated.blog_url)
    if old_blog != new_blog:
        if (
            previous.blog_status is not BlogStatus.PRESENT
            and updated.blog_status is BlogStatus.PRESENT
        ):
            kind = ChangeKind.BLOG_ADDED
        elif (
            previous.blog_status is BlogStatus.PRESENT and updated.blog_status is BlogStatus.ABSENT
        ):
            kind = ChangeKind.BLOG_REMOVED
        else:
            kind = ChangeKind.BLOG_CHANGED
        events.append(
            ChangeEvent(
                user_id=updated.id,
                changed_at=changed_at,
                kind=kind,
                old_value={"status": old_blog[0], "url": old_blog[1]},
                new_value={"status": new_blog[0], "url": new_blog[1]},
                run_id=run_id,
            )
        )
    return tuple(events)


def _commit_observation(
    previous: UserRecord,
    observation: Observation,
    *,
    run_id: str | None,
) -> MergeResult:
    had_baseline = previous.has_confirmed_state
    profile_changed = not had_baseline or previous.profile_status is not observation.profile_status
    blog_changed = (
        not had_baseline
        or previous.blog_status is not observation.blog_status
        or previous.blog_url != observation.blog_url
    )
    updated = replace(
        previous,
        profile_status=observation.profile_status,
        blog_status=observation.blog_status,
        blog_url=observation.blog_url,
        blog_url_raw=observation.blog_url_raw,
        last_checked_at=observation.observed_at,
        last_confirmed_at=observation.observed_at,
        profile_changed_at=(
            observation.observed_at if profile_changed else previous.profile_changed_at
        ),
        blog_changed_at=(observation.observed_at if blog_changed else previous.blog_changed_at),
        last_check_result=CheckResult.OK,
        consecutive_failures=0,
        parser_version=observation.parser_version,
        source_fingerprint=observation.source_fingerprint,
        http_status=observation.http_status,
        last_error=None,
        pending_observation=None,
    )
    changes = (
        _change_events(previous, updated, changed_at=observation.observed_at, run_id=run_id)
        if had_baseline
        else ()
    )
    return MergeResult(record=updated, changes=changes, confirmed=True, pending=False)


def apply_observation(
    previous: UserRecord | None,
    observation: Observation,
    *,
    confirm_changes: bool = True,
    required_confirmations: int = 2,
    run_id: str | None = None,
) -> MergeResult:
    """Merge an attempt without allowing failures to destroy confirmed data.

    First observations that report ``public + present`` are confirmed twice, as
    are all later semantic changes.  First observations of public/no-blog,
    hidden, or explicitly missing pages are accepted immediately; this avoids
    doubling every bootstrap request while still verifying every discovered
    blog.  Reprocessing the exact same timestamp cannot count as an independent
    confirmation.
    """

    if previous is not None and previous.id != observation.user_id:
        raise ValueError("observation user_id does not match the existing record")
    if required_confirmations < 1:
        raise ValueError("required_confirmations must be at least one")

    if not observation.successful:
        if previous is None:
            return MergeResult(record=_new_record_from_failed(observation))
        return MergeResult(
            record=replace(
                previous,
                last_checked_at=observation.observed_at,
                last_check_result=observation.check_result,
                consecutive_failures=previous.consecutive_failures + 1,
                parser_version=observation.parser_version,
                http_status=observation.http_status,
                last_error=observation.error,
            ),
            pending=previous.pending_observation is not None,
        )

    base = previous or UserRecord(
        id=observation.user_id,
        first_checked_at=observation.observed_at,
        last_checked_at=observation.observed_at,
        last_check_result=CheckResult.OK,
        parser_version=observation.parser_version,
        http_status=observation.http_status,
    )
    if base.first_checked_at is None:
        base = replace(base, first_checked_at=observation.observed_at)

    same_as_confirmed = base.has_confirmed_state and _semantic_tuple(base) == _observation_tuple(
        observation
    )
    if same_as_confirmed:
        return MergeResult(
            record=replace(
                base,
                blog_url_raw=observation.blog_url_raw,
                last_checked_at=observation.observed_at,
                last_confirmed_at=observation.observed_at,
                last_check_result=CheckResult.OK,
                consecutive_failures=0,
                parser_version=observation.parser_version,
                source_fingerprint=observation.source_fingerprint,
                http_status=observation.http_status,
                last_error=None,
                pending_observation=None,
            ),
            confirmed=True,
        )

    requires_confirmation = (
        confirm_changes
        and required_confirmations > 1
        and (base.has_confirmed_state or observation.blog_status is BlogStatus.PRESENT)
    )
    if not requires_confirmation:
        return _commit_observation(base, observation, run_id=run_id)

    pending = base.pending_observation
    if pending is None or not _pending_matches(pending, observation):
        new_pending = _pending_from(observation)
    elif parse_utc(observation.observed_at) > parse_utc(pending.last_observed_at):
        new_pending = replace(
            pending,
            confirmations=pending.confirmations + 1,
            last_observed_at=observation.observed_at,
            blog_url_raw=observation.blog_url_raw,
            parser_version=observation.parser_version,
            http_status=observation.http_status,
        )
    else:
        new_pending = pending

    if new_pending.confirmations >= required_confirmations:
        return _commit_observation(base, observation, run_id=run_id)
    return MergeResult(
        record=replace(
            base,
            last_checked_at=observation.observed_at,
            last_check_result=CheckResult.OK,
            consecutive_failures=0,
            http_status=observation.http_status,
            last_error=None,
            pending_observation=new_pending,
        ),
        pending=True,
    )


@dataclass(frozen=True, slots=True)
class Manifest:
    schema_version: int = 1
    generated_at: str | None = None
    updated_at: str | None = None
    phase: str = CrawlPhase.BOOTSTRAP.value
    initialization_status: InitializationStatus | str = InitializationStatus.NOT_STARTED
    record_count: int = 0
    highest_attempted_id: int = 0
    highest_confirmed_user_id: int = 0
    highest_existing_user_id: int = 0
    blog_count: int = 0
    profile_status_counts: Mapping[str, int] = field(
        default_factory=lambda: {"hidden": 0, "missing": 0, "public": 0, "unknown": 0}
    )
    last_successful_run_at: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError(f"unsupported manifest schema_version {self.schema_version!r}")
        object.__setattr__(
            self, "initialization_status", _enum(InitializationStatus, self.initialization_status)
        )
        try:
            object.__setattr__(self, "phase", CrawlPhase(self.phase).value)
        except ValueError as exc:
            raise ValueError(f"invalid manifest phase {self.phase!r}") from exc
        for name in ("generated_at", "updated_at", "last_successful_run_at"):
            object.__setattr__(self, name, _timestamp(getattr(self, name)))
        for name in (
            "record_count",
            "highest_attempted_id",
            "highest_confirmed_user_id",
            "highest_existing_user_id",
            "blog_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.profile_status_counts, Mapping):
            raise ValueError("profile_status_counts must be an object")
        counts = {status.value: 0 for status in ProfileStatus}
        unknown_count_keys = set(self.profile_status_counts).difference(counts)
        if unknown_count_keys:
            raise ValueError(
                "profile_status_counts has unknown key(s): "
                + ", ".join(sorted(map(str, unknown_count_keys)))
            )
        counts.update({str(key): int(value) for key, value in self.profile_status_counts.items()})
        if any(value < 0 for value in counts.values()):
            raise ValueError("profile_status_counts cannot contain negative values")
        object.__setattr__(self, "profile_status_counts", counts)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Manifest":
        _require_exact_fields(value, MANIFEST_FIELDS, "manifest")
        _require_json_types(
            value,
            {
                "schema_version": int,
                "generated_at": (str, type(None)),
                "updated_at": (str, type(None)),
                "phase": str,
                "initialization_status": str,
                "record_count": int,
                "highest_attempted_id": int,
                "highest_confirmed_user_id": int,
                "highest_existing_user_id": int,
                "blog_count": int,
                "profile_status_counts": Mapping,
                "last_successful_run_at": (str, type(None)),
            },
            "manifest",
        )
        counts = value["profile_status_counts"]
        expected_count_keys = {status.value for status in ProfileStatus}
        if set(counts) != expected_count_keys:
            raise ValueError(
                "manifest profile_status_counts must contain exactly all profile statuses"
            )
        if any(type(count) is not int for count in counts.values()):
            raise ValueError("manifest profile_status_counts values must be integers")
        return cls(
            schema_version=int(value.get("schema_version", 1)),
            generated_at=value.get("generated_at"),
            updated_at=value.get("updated_at"),
            phase=str(value.get("phase", CrawlPhase.BOOTSTRAP.value)),
            initialization_status=value.get(
                "initialization_status", InitializationStatus.NOT_STARTED.value
            ),
            record_count=int(value.get("record_count", 0)),
            highest_attempted_id=int(value.get("highest_attempted_id", 0)),
            highest_confirmed_user_id=int(value.get("highest_confirmed_user_id", 0)),
            highest_existing_user_id=int(
                value.get(
                    "highest_existing_user_id",
                    value.get("highest_confirmed_user_id", 0),
                )
            ),
            blog_count=int(value.get("blog_count", 0)),
            profile_status_counts=value.get("profile_status_counts", {}),
            last_successful_run_at=value.get("last_successful_run_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "blog_count": self.blog_count,
            "generated_at": self.generated_at,
            "highest_attempted_id": self.highest_attempted_id,
            "highest_confirmed_user_id": self.highest_confirmed_user_id,
            "highest_existing_user_id": self.highest_existing_user_id,
            "initialization_status": self.initialization_status.value,
            "last_successful_run_at": self.last_successful_run_at,
            "phase": self.phase,
            "profile_status_counts": dict(sorted(self.profile_status_counts.items())),
            "record_count": self.record_count,
            "schema_version": self.schema_version,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class CrawlerState:
    schema_version: int = 1
    phase: CrawlPhase | str = CrawlPhase.BOOTSTRAP
    next_id: int = 1
    highest_attempted_id: int = 0
    highest_confirmed_user_id: int = 0
    consecutive_missing_after_frontier: int = 0
    boundary_candidate_start: int | None = None
    boundary_confirmation_pending: bool = False
    maintenance_cursor: int = 1
    last_run_id: str | None = None
    updated_at: str | None = None
    paused_from_phase: CrawlPhase | str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError(f"unsupported crawler state schema_version {self.schema_version!r}")
        object.__setattr__(self, "phase", _enum(CrawlPhase, self.phase))
        object.__setattr__(self, "updated_at", _timestamp(self.updated_at))
        if self.paused_from_phase is not None:
            paused_from = _enum(CrawlPhase, self.paused_from_phase)
            if paused_from is CrawlPhase.PAUSED:
                raise ValueError("paused_from_phase cannot itself be paused")
            object.__setattr__(self, "paused_from_phase", paused_from)
        if self.boundary_candidate_start is not None:
            if (
                isinstance(self.boundary_candidate_start, bool)
                or not isinstance(self.boundary_candidate_start, int)
                or self.boundary_candidate_start < 1
            ):
                raise ValueError("boundary_candidate_start must be a positive integer")
        for name in (
            "next_id",
            "highest_attempted_id",
            "highest_confirmed_user_id",
            "consecutive_missing_after_frontier",
            "maintenance_cursor",
        ):
            value = getattr(self, name)
            minimum = 1 if name in {"next_id", "maintenance_cursor"} else 0
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CrawlerState":
        _require_exact_fields(value, CRAWLER_STATE_FIELDS, "crawler state")
        _require_json_types(
            value,
            {
                "schema_version": int,
                "phase": str,
                "next_id": int,
                "highest_attempted_id": int,
                "highest_confirmed_user_id": int,
                "consecutive_missing_after_frontier": int,
                "boundary_candidate_start": (int, type(None)),
                "boundary_confirmation_pending": bool,
                "maintenance_cursor": int,
                "last_run_id": (str, type(None)),
                "updated_at": (str, type(None)),
                "paused_from_phase": (str, type(None)),
            },
            "crawler state",
        )
        return cls(
            schema_version=int(value.get("schema_version", 1)),
            phase=value.get("phase", CrawlPhase.BOOTSTRAP.value),
            next_id=int(value.get("next_id", 1)),
            highest_attempted_id=int(value.get("highest_attempted_id", 0)),
            highest_confirmed_user_id=int(value.get("highest_confirmed_user_id", 0)),
            consecutive_missing_after_frontier=int(
                value.get("consecutive_missing_after_frontier", 0)
            ),
            boundary_candidate_start=value.get("boundary_candidate_start"),
            boundary_confirmation_pending=bool(value.get("boundary_confirmation_pending", False)),
            maintenance_cursor=int(value.get("maintenance_cursor", 1)),
            last_run_id=value.get("last_run_id"),
            updated_at=value.get("updated_at"),
            paused_from_phase=value.get("paused_from_phase"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "boundary_candidate_start": self.boundary_candidate_start,
            "boundary_confirmation_pending": self.boundary_confirmation_pending,
            "consecutive_missing_after_frontier": self.consecutive_missing_after_frontier,
            "highest_attempted_id": self.highest_attempted_id,
            "highest_confirmed_user_id": self.highest_confirmed_user_id,
            "last_run_id": self.last_run_id,
            "maintenance_cursor": self.maintenance_cursor,
            "next_id": self.next_id,
            "paused_from_phase": (
                self.paused_from_phase.value if self.paused_from_phase is not None else None
            ),
            "phase": self.phase.value,
            "schema_version": self.schema_version,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    mode: str
    started_at: str
    finished_at: str | None = None
    start_id: int | None = None
    end_id: int | None = None
    attempted: int = 0
    confirmed: int = 0
    changed: int = 0
    errors: int = 0
    rate_limited: int = 0
    stopped_reason: str | None = None
    crawler_version: str = "unknown"

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id cannot be empty")
        if not self.mode.strip() or not self.crawler_version.strip():
            raise ValueError("run mode and crawler_version cannot be empty")
        object.__setattr__(self, "started_at", format_utc(self.started_at))
        object.__setattr__(self, "finished_at", _timestamp(self.finished_at))
        object.__setattr__(self, "stopped_reason", sanitize_error(self.stopped_reason))
        for name in ("attempted", "confirmed", "changed", "errors", "rate_limited"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"run {name} must be a non-negative integer")
        for name in ("start_id", "end_id"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                raise ValueError(f"run {name} must be a positive integer or null")
        if self.confirmed > self.attempted or self.errors > self.attempted:
            raise ValueError("run confirmed/errors cannot exceed attempted")
        if self.rate_limited > self.errors:
            raise ValueError("run rate_limited cannot exceed errors")
        if self.finished_at is not None and parse_utc(self.finished_at) < parse_utc(
            self.started_at
        ):
            raise ValueError("run finished_at cannot precede started_at")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunRecord":
        _require_exact_fields(value, RUN_RECORD_FIELDS, "run record")
        _require_json_types(
            value,
            {
                "run_id": str,
                "mode": str,
                "started_at": str,
                "finished_at": (str, type(None)),
                "start_id": (int, type(None)),
                "end_id": (int, type(None)),
                "attempted": int,
                "confirmed": int,
                "changed": int,
                "errors": int,
                "rate_limited": int,
                "stopped_reason": (str, type(None)),
                "crawler_version": str,
            },
            "run record",
        )
        return cls(
            run_id=str(value["run_id"]),
            mode=str(value["mode"]),
            started_at=value["started_at"],
            finished_at=value.get("finished_at"),
            start_id=value.get("start_id"),
            end_id=value.get("end_id"),
            attempted=int(value.get("attempted", 0)),
            confirmed=int(value.get("confirmed", 0)),
            changed=int(value.get("changed", 0)),
            errors=int(value.get("errors", 0)),
            rate_limited=int(value.get("rate_limited", 0)),
            stopped_reason=value.get("stopped_reason"),
            crawler_version=str(value.get("crawler_version", "unknown")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "changed": self.changed,
            "confirmed": self.confirmed,
            "crawler_version": self.crawler_version,
            "end_id": self.end_id,
            "errors": self.errors,
            "finished_at": self.finished_at,
            "mode": self.mode,
            "rate_limited": self.rate_limited,
            "run_id": self.run_id,
            "start_id": self.start_id,
            "started_at": self.started_at,
            "stopped_reason": self.stopped_reason,
        }


def manifest_from_records(
    records: Iterable[UserRecord],
    *,
    generated_at: str,
    initialization_status: InitializationStatus | str,
    phase: CrawlPhase | str,
    last_successful_run_at: str | None = None,
) -> Manifest:
    materialized = list(records)
    counts = {status.value: 0 for status in ProfileStatus}
    for record in materialized:
        counts[record.profile_status.value] += 1
    highest_attempted = max((record.id for record in materialized), default=0)
    highest_existing = max(
        (record.id for record in materialized if record.is_confirmed_existing_user),
        default=0,
    )
    blogs = sum(
        record.profile_status is ProfileStatus.PUBLIC
        and record.blog_status is BlogStatus.PRESENT
        and record.blog_url is not None
        for record in materialized
    )
    phase_value = phase.value if isinstance(phase, CrawlPhase) else str(phase)
    return Manifest(
        generated_at=generated_at,
        updated_at=generated_at,
        phase=phase_value,
        initialization_status=initialization_status,
        record_count=len(materialized),
        highest_attempted_id=highest_attempted,
        highest_confirmed_user_id=highest_existing,
        highest_existing_user_id=highest_existing,
        blog_count=blogs,
        profile_status_counts=counts,
        last_successful_run_at=last_successful_run_at,
    )


__all__ = [
    "BlogStatus",
    "ChangeEvent",
    "ChangeKind",
    "CheckResult",
    "CrawlerState",
    "CrawlPhase",
    "InitializationStatus",
    "Manifest",
    "MergeResult",
    "Observation",
    "PendingObservation",
    "PENDING_OBSERVATION_FIELDS",
    "ProfileStatus",
    "RunRecord",
    "RUN_RECORD_FIELDS",
    "UserRecord",
    "USER_RECORD_FIELDS",
    "CHANGE_EVENT_FIELDS",
    "CRAWLER_STATE_FIELDS",
    "MANIFEST_FIELDS",
    "apply_observation",
    "manifest_from_records",
]
