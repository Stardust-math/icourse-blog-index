"""Deterministic, reviewable storage for the crawler's canonical dataset."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, Callable

from .models import (
    ChangeEvent,
    CrawlerState,
    CrawlPhase,
    InitializationStatus,
    Manifest,
    RunRecord,
    UserRecord,
    manifest_from_records,
)
from .utils import (
    SHARD_SIZE,
    atomic_write_json,
    atomic_write_text,
    canonical_json,
    format_utc,
    is_public_http_url,
    normalize_http_url,
    shard_filename,
    shard_start,
)


_SHARD_RE = re.compile(r"^(\d+)-(\d+)\.jsonl$")
_YEAR_FILE_RE = re.compile(r"^(\d{4})\.jsonl$")
LINK_HEALTH_FIELDS = frozenset(
    {
        "attempts",
        "checked_at",
        "consecutive_failures",
        "error",
        "failure_confirmed",
        "final_url",
        "http_status",
        "redirect_count",
        "status",
        "url",
    }
)
LINK_HEALTH_STATUSES = frozenset(
    {
        "blocked",
        "client_error",
        "dns_error",
        "reachable",
        "redirected",
        "server_error",
        "timeout",
        "tls_error",
    }
)


class DatasetCorruptionError(ValueError):
    """Raised when committed data cannot be read without guessing."""


def _json_lines(rows: Iterable[Mapping[str, Any]]) -> str:
    materialized = list(rows)
    if not materialized:
        return ""
    return "\n".join(canonical_json(row) for row in materialized) + "\n"


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        handle = path.open("r", encoding="utf-8")
    except FileNotFoundError:
        return rows
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                raise DatasetCorruptionError(f"{path}:{line_number}: blank JSONL row")
            try:
                value = json.loads(line, object_pairs_hook=_object_without_duplicate_keys)
            except json.JSONDecodeError as exc:
                raise DatasetCorruptionError(
                    f"{path}:{line_number}: invalid JSON: {exc.msg}"
                ) from exc
            except ValueError as exc:
                raise DatasetCorruptionError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise DatasetCorruptionError(f"{path}:{line_number}: JSONL row must be an object")
            if line.rstrip("\r\n") != canonical_json(value):
                raise DatasetCorruptionError(
                    f"{path}:{line_number}: JSONL row is not canonically encoded"
                )
            rows.append(value)
    return rows


def _read_json_document(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise DatasetCorruptionError(f"{path}: invalid JSON: {exc.msg}") from exc
    except ValueError as exc:
        raise DatasetCorruptionError(f"{path}: invalid JSON: {exc}") from exc


def _canonical_timestamp(value: str, context: str) -> str:
    try:
        normalized = format_utc(value)
    except ValueError as exc:
        raise ValueError(f"{context} must be a UTC timestamp") from exc
    if normalized != value:
        raise ValueError(f"{context} must use canonical second-resolution UTC format")
    return value


def _validate_link_health_row(value: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(value).difference(LINK_HEALTH_FIELDS)
    missing = LINK_HEALTH_FIELDS.difference(value)
    if unknown or missing:
        details: list[str] = []
        if unknown:
            details.append("unknown field(s): " + ", ".join(sorted(map(str, unknown))))
        if missing:
            details.append("missing field(s): " + ", ".join(sorted(missing)))
        raise ValueError("link-health record has " + "; ".join(details))

    for field_name in ("url", "status", "checked_at"):
        if not isinstance(value[field_name], str):
            raise ValueError(f"link-health {field_name} must be a string")
    for field_name in ("http_status", "final_url", "error"):
        if value[field_name] is not None and not isinstance(
            value[field_name], str if field_name != "http_status" else int
        ):
            raise ValueError(f"link-health {field_name} has an invalid type")
    if value["http_status"] is not None and type(value["http_status"]) is not int:
        raise ValueError("link-health http_status must be an integer or null")
    for field_name in ("consecutive_failures", "attempts", "redirect_count"):
        if type(value[field_name]) is not int:
            raise ValueError(f"link-health {field_name} must be an integer")
    if type(value["failure_confirmed"]) is not bool:
        raise ValueError("link-health failure_confirmed must be a boolean")

    status = value["status"]
    if status not in LINK_HEALTH_STATUSES:
        raise ValueError(f"invalid link-health status {status!r}")
    url = value["url"]
    if not is_public_http_url(url) or normalize_http_url(url) != url:
        raise ValueError("link-health url must be a canonical public HTTP(S) URL")
    final_url = value["final_url"]
    if final_url is not None:
        try:
            final_url_is_canonical = normalize_http_url(final_url) == final_url
        except ValueError as exc:
            raise ValueError("link-health final_url must be a canonical HTTP(S) URL") from exc
        if not final_url_is_canonical:
            raise ValueError("link-health final_url must be a canonical HTTP(S) URL")
        if status != "blocked" and not is_public_http_url(final_url):
            raise ValueError("non-blocked link-health final_url must be public")
    _canonical_timestamp(value["checked_at"], "link-health checked_at")
    http_status = value["http_status"]
    if http_status is not None and not 100 <= http_status <= 599:
        raise ValueError("link-health http_status must be between 100 and 599")
    if value["consecutive_failures"] < 0:
        raise ValueError("link-health consecutive_failures cannot be negative")
    if value["attempts"] < 1:
        raise ValueError("link-health attempts must be positive")
    if value["redirect_count"] < 0:
        raise ValueError("link-health redirect_count cannot be negative")
    if value["error"] is not None and len(value["error"]) > 500:
        raise ValueError("link-health error exceeds 500 characters")
    if status in {"reachable", "redirected"}:
        if value["consecutive_failures"] != 0 or value["failure_confirmed"]:
            raise ValueError("healthy link-health record cannot retain failures")
        if http_status is None or not 200 <= http_status < 300:
            raise ValueError("healthy link-health record requires a 2xx HTTP status")
        if value["error"] is not None or final_url is None:
            raise ValueError("healthy link-health record requires final_url and null error")
        if status == "reachable" and value["redirect_count"] != 0:
            raise ValueError("reachable link-health status cannot contain redirects")
        if status == "redirected" and value["redirect_count"] < 1:
            raise ValueError("redirected link-health status requires a redirect")
    elif value["consecutive_failures"] < 1:
        raise ValueError("unhealthy link-health record requires a positive failure count")
    else:
        if value["failure_confirmed"] != (value["consecutive_failures"] >= 2):
            raise ValueError("link-health failure confirmation does not match failure count")
        if value["error"] is None or not value["error"].strip():
            raise ValueError("unhealthy link-health record requires a nonempty error")
        if status in {"timeout", "dns_error", "tls_error"} and http_status is not None:
            raise ValueError(f"{status} link-health status requires null http_status")
        if status == "server_error" and (http_status is None or not 500 <= http_status < 600):
            raise ValueError("server_error link-health status requires a 5xx status")
        if (
            status == "client_error"
            and http_status is not None
            and (200 <= http_status < 300 or 500 <= http_status < 600 or http_status in {403, 429})
        ):
            raise ValueError(
                "client_error link-health status cannot contain a 2xx/5xx status or 403/429"
            )
    return dict(value)


class RepositoryStore:
    """Read and atomically update a repository rooted at ``root``.

    Each user shard is rewritten at most once per ``upsert_users`` call.  No
    partially written shard, state file, manifest, or audit log is ever exposed.
    GitHub Actions additionally serializes crawler jobs with a workflow-level
    concurrency group; this class intentionally does not invent a distributed
    lock.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.data_dir = self.root / "data"
        self.users_dir = self.data_dir / "users"
        self.changes_dir = self.data_dir / "changes"
        self.runs_dir = self.data_dir / "runs"
        self.state_dir = self.root / "state"
        self.manifest_path = self.data_dir / "manifest.json"
        self.state_path = self.state_dir / "crawler.json"

    def ensure_directories(self) -> None:
        for directory in (
            self.users_dir,
            self.changes_dir,
            self.runs_dir,
            self.state_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def validate_layout(self) -> None:
        """Reject files that could hide unvalidated data beside canonical artifacts."""

        allowed_data_files = {".gitkeep", "blogs.csv", "link-health.jsonl", "manifest.json"}
        allowed_data_directories = {"changes", "runs", "users"}
        if self.data_dir.exists():
            if self.data_dir.is_symlink() or not self.data_dir.is_dir():
                raise DatasetCorruptionError("data must be a regular directory")
            for path in self.data_dir.iterdir():
                if path.name in allowed_data_directories:
                    if path.is_symlink() or not path.is_dir():
                        raise DatasetCorruptionError(
                            f"data/{path.name} must be a regular directory"
                        )
                    continue
                if path.name not in allowed_data_files:
                    raise DatasetCorruptionError(f"unexpected entry in data: {path.name}")
                if path.is_symlink() or not path.is_file():
                    raise DatasetCorruptionError(f"data/{path.name} must be a regular file")
                if path.name == ".gitkeep" and path.read_text(encoding="utf-8").strip():
                    raise DatasetCorruptionError("data/.gitkeep must be empty")

        if self.state_dir.exists():
            if self.state_dir.is_symlink() or not self.state_dir.is_dir():
                raise DatasetCorruptionError("state must be a regular directory")
            for path in self.state_dir.iterdir():
                if path.name != "crawler.json":
                    raise DatasetCorruptionError(f"unexpected entry in state: {path.name}")
                if path.is_symlink() or not path.is_file():
                    raise DatasetCorruptionError("state/crawler.json must be a regular file")

    def shard_path(self, user_id: int) -> Path:
        return self.users_dir / shard_filename(user_id)

    def _load_shard(self, user_id_or_start: int) -> dict[int, UserRecord]:
        start = shard_start(user_id_or_start)
        path = self.users_dir / shard_filename(start)
        result: dict[int, UserRecord] = {}
        for line_number, value in enumerate(_read_jsonl(path), 1):
            try:
                record = UserRecord.from_dict(value)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise DatasetCorruptionError(
                    f"{path}:{line_number}: invalid user record: {exc}"
                ) from exc
            if record.to_dict() != value:
                raise DatasetCorruptionError(
                    f"{path}:{line_number}: user record is not canonically serialized"
                )
            if shard_start(record.id) != start:
                raise DatasetCorruptionError(
                    f"{path}:{line_number}: user {record.id} belongs in {shard_filename(record.id)}"
                )
            if record.id in result:
                raise DatasetCorruptionError(f"{path}: duplicate user ID {record.id}")
            result[record.id] = record
        return result

    def load_user(self, user_id: int) -> UserRecord | None:
        """Load one user in O(size of one shard), or return ``None``."""

        return self._load_shard(user_id).get(user_id)

    def iter_users(self) -> Iterator[UserRecord]:
        """Yield every canonical record exactly once in increasing ID order."""

        if not self.users_dir.exists():
            return
        previous_id = 0
        seen_shards: set[int] = set()
        paths: list[tuple[int, Path]] = []
        for path in self.users_dir.iterdir():
            if path.name == ".gitkeep":
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.read_text(encoding="utf-8").strip()
                ):
                    raise DatasetCorruptionError(
                        "data/users/.gitkeep must be an empty regular file"
                    )
                continue
            if path.is_symlink() or not path.is_file():
                raise DatasetCorruptionError(f"unexpected entry in data/users: {path.name}")
            match = _SHARD_RE.fullmatch(path.name)
            if match is None:
                raise DatasetCorruptionError(f"invalid user shard filename: {path.name}")
            start, end = int(match.group(1)), int(match.group(2))
            if (
                start % SHARD_SIZE
                or end != start + SHARD_SIZE - 1
                or path.name != shard_filename(start)
            ):
                raise DatasetCorruptionError(f"invalid shard filename: {path.name}")
            if start in seen_shards:
                raise DatasetCorruptionError(f"duplicate shard start: {start}")
            seen_shards.add(start)
            paths.append((start, path))
        for start, _path in sorted(paths):
            shard = self._load_shard(start)
            for user_id in sorted(shard):
                if user_id <= previous_id:
                    raise DatasetCorruptionError(f"duplicate or unsorted user ID {user_id}")
                previous_id = user_id
                yield shard[user_id]

    @staticmethod
    def _year_paths(directory: Path, label: str) -> list[tuple[str, Path]]:
        if not directory.exists():
            return []
        paths: list[tuple[str, Path]] = []
        for path in directory.iterdir():
            if path.name == ".gitkeep":
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.read_text(encoding="utf-8").strip()
                ):
                    raise DatasetCorruptionError(f"{label}/.gitkeep must be an empty regular file")
                continue
            if path.is_symlink() or not path.is_file():
                raise DatasetCorruptionError(f"unexpected entry in {label}: {path.name}")
            match = _YEAR_FILE_RE.fullmatch(path.name)
            if match is None or match.group(1) == "0000":
                raise DatasetCorruptionError(f"invalid {label} filename: {path.name}")
            paths.append((match.group(1), path))
        return sorted(paths)

    @staticmethod
    def _change_rows(path: Path, expected_year: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        previous_order: tuple[str, int, str] | None = None
        for line_number, raw in enumerate(_read_jsonl(path), 1):
            try:
                event = ChangeEvent.from_dict(raw)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise DatasetCorruptionError(
                    f"{path}:{line_number}: invalid change event: {exc}"
                ) from exc
            canonical = event.to_dict()
            if canonical != raw:
                raise DatasetCorruptionError(
                    f"{path}:{line_number}: change event is not canonically serialized"
                )
            if event.changed_at[:4] != expected_year:
                raise DatasetCorruptionError(
                    f"{path}:{line_number}: change event belongs in {event.changed_at[:4]}.jsonl"
                )
            assert event.event_id is not None
            if event.event_id in seen:
                raise DatasetCorruptionError(f"{path}: duplicate change event {event.event_id}")
            seen.add(event.event_id)
            order = (event.changed_at, event.user_id, event.event_id)
            if previous_order is not None and order <= previous_order:
                raise DatasetCorruptionError(f"{path}: change events are not canonically sorted")
            previous_order = order
            rows.append(canonical)
        if not rows:
            raise DatasetCorruptionError(f"{path}: annual change log cannot be empty")
        return rows

    @staticmethod
    def _run_rows(path: Path, expected_year: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        previous_order: tuple[str, str] | None = None
        for line_number, raw in enumerate(_read_jsonl(path), 1):
            try:
                run = RunRecord.from_dict(raw)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise DatasetCorruptionError(
                    f"{path}:{line_number}: invalid run record: {exc}"
                ) from exc
            canonical = run.to_dict()
            if canonical != raw:
                raise DatasetCorruptionError(
                    f"{path}:{line_number}: run record is not canonically serialized"
                )
            if run.started_at[:4] != expected_year:
                raise DatasetCorruptionError(
                    f"{path}:{line_number}: run belongs in {run.started_at[:4]}.jsonl"
                )
            if run.run_id in seen:
                raise DatasetCorruptionError(f"{path}: duplicate run_id {run.run_id}")
            seen.add(run.run_id)
            order = (run.started_at, run.run_id)
            if previous_order is not None and order <= previous_order:
                raise DatasetCorruptionError(f"{path}: run records are not canonically sorted")
            previous_order = order
            rows.append(canonical)
        if not rows:
            raise DatasetCorruptionError(f"{path}: annual run log cannot be empty")
        return rows

    def iter_changes(self) -> Iterator[ChangeEvent]:
        """Yield strict, canonically ordered confirmed change events."""

        seen_global: set[str] = set()
        for year, path in self._year_paths(self.changes_dir, "data/changes"):
            for row in self._change_rows(path, year):
                event = ChangeEvent.from_dict(row)
                assert event.event_id is not None
                if event.event_id in seen_global:
                    raise DatasetCorruptionError(
                        f"duplicate change event across files: {event.event_id}"
                    )
                seen_global.add(event.event_id)
                yield event

    def iter_runs(self) -> Iterator[RunRecord]:
        """Yield strict, canonically ordered crawler run records."""

        seen_global: set[str] = set()
        for year, path in self._year_paths(self.runs_dir, "data/runs"):
            for row in self._run_rows(path, year):
                run = RunRecord.from_dict(row)
                if run.run_id in seen_global:
                    raise DatasetCorruptionError(f"duplicate run_id across files: {run.run_id}")
                seen_global.add(run.run_id)
                yield run

    def load_link_health(self) -> dict[str, dict[str, Any]]:
        """Load the single strict current health row for each canonical URL."""

        path = self.data_dir / "link-health.jsonl"
        health: dict[str, dict[str, Any]] = {}
        previous_url: str | None = None
        for line_number, raw in enumerate(_read_jsonl(path), 1):
            try:
                row = _validate_link_health_row(raw)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise DatasetCorruptionError(
                    f"{path}:{line_number}: invalid link-health record: {exc}"
                ) from exc
            url = row["url"]
            if url in health:
                raise DatasetCorruptionError(f"{path}: duplicate link-health URL {url}")
            if previous_url is not None and url <= previous_url:
                raise DatasetCorruptionError(f"{path}: link-health records are not URL-sorted")
            previous_url = url
            health[url] = row
        return health

    def upsert_users(self, records: Iterable[UserRecord | Mapping[str, Any]]) -> int:
        """Insert or replace records and atomically rewrite affected shards.

        Duplicate IDs in the input are rejected rather than silently choosing a
        winner.  The return value is the number of records supplied.
        """

        grouped: dict[int, list[UserRecord]] = defaultdict(list)
        supplied_ids: set[int] = set()
        for value in records:
            record = value if isinstance(value, UserRecord) else UserRecord.from_dict(value)
            if record.id in supplied_ids:
                raise ValueError(f"duplicate input user ID {record.id}")
            supplied_ids.add(record.id)
            grouped[shard_start(record.id)].append(record)
        self.ensure_directories()
        for start in sorted(grouped):
            merged = self._load_shard(start)
            for record in grouped[start]:
                merged[record.id] = record
            path = self.users_dir / shard_filename(start)
            atomic_write_text(
                path,
                _json_lines(merged[user_id].to_dict() for user_id in sorted(merged)),
            )
        return len(supplied_ids)

    def load_manifest(self) -> Manifest:
        value = _read_json_document(self.manifest_path)
        if value is None:
            return Manifest()
        if not isinstance(value, Mapping):
            raise DatasetCorruptionError("data/manifest.json must contain an object")
        try:
            manifest = Manifest.from_dict(value)
        except (AttributeError, TypeError, ValueError) as exc:
            raise DatasetCorruptionError(f"invalid data/manifest.json: {exc}") from exc
        if manifest.to_dict() != value:
            raise DatasetCorruptionError("data/manifest.json is not canonically serialized")
        return manifest

    def write_manifest(self, manifest: Manifest | Mapping[str, Any]) -> None:
        value = manifest if isinstance(manifest, Manifest) else Manifest.from_dict(manifest)
        atomic_write_json(self.manifest_path, value.to_dict())

    save_manifest = write_manifest

    def rebuild_manifest(
        self,
        *,
        generated_at: str | None = None,
        initialization_status: InitializationStatus | str | None = None,
        phase: CrawlPhase | str | None = None,
        last_successful_run_at: str | None = None,
    ) -> Manifest:
        """Recalculate public summary counters from canonical user shards."""

        previous = self.load_manifest()
        timestamp = format_utc(generated_at)
        chosen_status = initialization_status or previous.initialization_status
        chosen_phase = phase or previous.phase
        chosen_last_run = (
            previous.last_successful_run_at
            if last_successful_run_at is None
            else last_successful_run_at
        )
        manifest = manifest_from_records(
            self.iter_users(),
            generated_at=timestamp,
            initialization_status=chosen_status,
            phase=chosen_phase,
            last_successful_run_at=chosen_last_run,
        )
        self.write_manifest(manifest)
        return manifest

    def load_state(self) -> CrawlerState:
        value = _read_json_document(self.state_path)
        if value is None:
            return CrawlerState()
        if not isinstance(value, Mapping):
            raise DatasetCorruptionError("state/crawler.json must contain an object")
        try:
            state = CrawlerState.from_dict(value)
        except (AttributeError, TypeError, ValueError) as exc:
            raise DatasetCorruptionError(f"invalid state/crawler.json: {exc}") from exc
        if state.to_dict() != value:
            raise DatasetCorruptionError("state/crawler.json is not canonically serialized")
        return state

    def save_state(self, state: CrawlerState | Mapping[str, Any]) -> None:
        value = state if isinstance(state, CrawlerState) else CrawlerState.from_dict(state)
        atomic_write_json(self.state_path, value.to_dict())

    write_state = save_state

    @staticmethod
    def _year(timestamp: str) -> str:
        normalized = format_utc(timestamp)
        return normalized[:4]

    def _merge_audit_rows(
        self,
        path: Path,
        existing: Iterable[dict[str, Any]],
        rows: Iterable[dict[str, Any]],
        *,
        identity: Callable[[dict[str, Any]], str],
        order: Callable[[dict[str, Any]], tuple[Any, ...]],
    ) -> int:
        combined: dict[str, dict[str, Any]] = {}
        existing_rows = list(existing)
        for row in existing_rows:
            key = identity(row)
            if key in combined:
                raise DatasetCorruptionError(f"{path}: duplicate audit identity {key!r}")
            combined[key] = row
        for row in rows:
            key = identity(row)
            old = combined.get(key)
            if old is not None and old != row:
                raise DatasetCorruptionError(f"{path}: audit identity {key!r} has conflicting rows")
            combined[key] = row
        ordered = sorted(combined.values(), key=order)
        atomic_write_text(path, _json_lines(ordered))
        return len(combined) - len(existing_rows)

    def append_changes(self, events: Iterable[ChangeEvent | Mapping[str, Any]]) -> int:
        """Idempotently add confirmed change events, grouped by UTC year."""

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for value in events:
            event = value if isinstance(value, ChangeEvent) else ChangeEvent.from_dict(value)
            grouped[self._year(event.changed_at)].append(event.to_dict())
        self.ensure_directories()
        existing_by_year: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in self.iter_changes():
            existing_by_year[event.changed_at[:4]].append(event.to_dict())
        added = 0
        for year in sorted(grouped):
            added += self._merge_audit_rows(
                self.changes_dir / f"{year}.jsonl",
                existing_by_year[year],
                grouped[year],
                identity=lambda row: str(row["event_id"]),
                order=lambda row: (
                    str(row["changed_at"]),
                    int(row["user_id"]),
                    str(row["event_id"]),
                ),
            )
        return added

    def append_change(self, event: ChangeEvent | Mapping[str, Any]) -> int:
        return self.append_changes([event])

    def append_runs(self, runs: Iterable[RunRecord | Mapping[str, Any]]) -> int:
        """Idempotently add or finalize run summaries, grouped by start year.

        A run may first be written without ``finished_at`` and later replaced by
        its final summary.  All other conflicts are rejected.
        """

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        supplied_ids: set[str] = set()
        for value in runs:
            run = value if isinstance(value, RunRecord) else RunRecord.from_dict(value)
            if run.run_id in supplied_ids:
                raise ValueError(f"duplicate input run_id {run.run_id!r}")
            supplied_ids.add(run.run_id)
            grouped[self._year(run.started_at)].append(run.to_dict())
        self.ensure_directories()
        existing_by_year: dict[str, list[dict[str, Any]]] = defaultdict(list)
        existing_year_by_id: dict[str, str] = {}
        for run in self.iter_runs():
            year = run.started_at[:4]
            existing_by_year[year].append(run.to_dict())
            existing_year_by_id[run.run_id] = year
        changed = 0
        for year in sorted(grouped):
            path = self.runs_dir / f"{year}.jsonl"
            existing_rows = existing_by_year[year]
            by_id = {str(row["run_id"]): row for row in existing_rows}
            for row in grouped[year]:
                key = str(row["run_id"])
                old_year = existing_year_by_id.get(key)
                if old_year is not None and old_year != year:
                    raise DatasetCorruptionError(
                        f"run_id {key!r} already exists in {old_year}.jsonl"
                    )
                previous = by_id.get(key)
                if previous == row:
                    continue
                if previous is not None:
                    if previous["finished_at"] is not None or row["finished_at"] is None:
                        raise DatasetCorruptionError(
                            f"run_id {key!r} cannot overwrite an existing run summary"
                        )
                    for identity_field in (
                        "run_id",
                        "mode",
                        "started_at",
                        "start_id",
                        "crawler_version",
                    ):
                        if previous[identity_field] != row[identity_field]:
                            raise DatasetCorruptionError(
                                f"run_id {key!r} changed identity field {identity_field}"
                            )
                by_id[key] = row
                changed += 1
            atomic_write_text(
                path,
                _json_lines(
                    sorted(
                        by_id.values(),
                        key=lambda row: (str(row["started_at"]), str(row["run_id"])),
                    )
                ),
            )
        return changed

    def append_run(self, run: RunRecord | Mapping[str, Any]) -> int:
        return self.append_runs([run])


__all__ = [
    "DatasetCorruptionError",
    "LINK_HEALTH_FIELDS",
    "LINK_HEALTH_STATUSES",
    "RepositoryStore",
]
