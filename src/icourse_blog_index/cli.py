"""Command-line orchestration for serial, checkpointed repository updates."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import __version__
from .fetcher import FetcherConfig, ProfileFetcher, resolve_repository_url
from .link_checker import HEALTHY_STATUSES, LinkChecker, LinkHealthStatus
from .models import (
    BlogStatus,
    CheckResult,
    CrawlerState,
    CrawlPhase,
    InitializationStatus,
    ProfileStatus,
    RunRecord,
    UserRecord,
    apply_observation,
)
from .renderer import (
    INDEX_BEGIN,
    INDEX_END,
    RenderError,
    load_link_health,
    render_repository,
)
from .scheduler import DEFAULT_POLICY, frontier_probe_start, select_due_users
from .storage import DatasetCorruptionError, RepositoryStore
from .utils import atomic_write_text, canonical_json, format_utc, parse_utc, utc_now
from .validator import validate_dataset


EXIT_USAGE = 2
EXIT_SAFETY_STOP = 3
EXIT_INVALID_DATA = 4

_PARSE_FAILURE_RESULTS = frozenset({CheckResult.PARSE_ERROR, CheckResult.SUSPECTED_STALE})
_TRANSIENT_FAILURE_RESULTS = frozenset(
    {
        CheckResult.HTTP_ERROR,
        CheckResult.NETWORK_ERROR,
        CheckResult.SERVER_ERROR,
        CheckResult.TIMEOUT,
    }
)


class CommandError(RuntimeError):
    """A concise, expected CLI failure with a stable exit status."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class CrawlCounters:
    attempted: int = 0
    confirmed: int = 0
    changed: int = 0
    errors: int = 0
    rate_limited: int = 0
    last_id: int | None = None


@dataclass(frozen=True)
class ProcessedUser:
    record: UserRecord
    confirmed: bool
    changes: tuple[Any, ...]
    check_results: tuple[CheckResult, ...]


class CrawlSession:
    """Hold one command's in-memory records and flush bounded checkpoints."""

    def __init__(
        self,
        store: RepositoryStore,
        fetcher: ProfileFetcher,
        run_id: str,
        *,
        checkpoint_every: int,
    ) -> None:
        self.store = store
        self.fetcher = fetcher
        self.run_id = run_id
        self.checkpoint_every = checkpoint_every
        self.records = {record.id: record for record in store.iter_users()}
        self._dirty: dict[int, UserRecord] = {}
        self._changes: list[Any] = []
        self._since_flush = 0

    def process(self, user_id: int) -> ProcessedUser:
        previous = self.records.get(user_id)
        # Confirmed public profiles are the records whose blog field can
        # change.  Their scheduled rechecks use a unique same-path query from
        # the first request, so an unlabelled intermediary cache cannot keep an
        # old blog URL alive for another refresh interval.  This changes no
        # request count and bootstrap remains cache-friendly for non-blog IDs.
        force_fresh = previous is not None and previous.profile_status is ProfileStatus.PUBLIC
        observation = self.fetcher.fetch_and_parse(user_id, cache_bust=force_fresh)
        first = apply_observation(previous, observation, run_id=self.run_id)
        final = first
        check_results = [observation.check_result]
        changes = list(first.changes)

        # A discovered blog and every semantic change require two independent
        # direct observations.  The query token also bypasses intermediary
        # caches while normal no-cache headers remain in force.
        if first.pending and observation.successful:
            confirmation = self.fetcher.fetch_and_parse(user_id, cache_bust=True)
            check_results.append(confirmation.check_result)
            final = apply_observation(first.record, confirmation, run_id=self.run_id)
            changes.extend(final.changes)

        self.records[user_id] = final.record
        self._dirty[user_id] = final.record
        self._changes.extend(changes)
        self._since_flush += 1
        return ProcessedUser(
            record=final.record,
            confirmed=final.confirmed,
            changes=tuple(changes),
            check_results=tuple(check_results),
        )

    def checkpoint(self, state: CrawlerState, *, force: bool = False) -> None:
        if not force and self._since_flush < self.checkpoint_every:
            return
        if self._dirty:
            self.store.upsert_users(self._dirty[user_id] for user_id in sorted(self._dirty))
            self._dirty.clear()
        if self._changes:
            self.store.append_changes(self._changes)
            self._changes.clear()
        self.store.save_state(state)
        self._since_flush = 0


def _new_run_id(mode: str) -> str:
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    return f"{mode}-{stamp}-{uuid.uuid4().hex[:8]}"


def _json_print(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _fetcher_from_args(args: argparse.Namespace) -> ProfileFetcher:
    config = FetcherConfig(
        repository_url=args.repository_url,
        min_delay_seconds=args.min_delay_seconds,
        max_delay_seconds=args.max_delay_seconds,
    )
    return ProfileFetcher(config)


def _ensure_robots_allowed(fetcher: ProfileFetcher) -> None:
    result = fetcher.check_robots(force=True)
    if not result.allowed:
        raise CommandError(
            f"safety stop: {result.reason}",
            EXIT_SAFETY_STOP,
        )


def _is_existing(record: UserRecord) -> bool:
    return record.is_confirmed_existing_user


def _is_confirmed_missing(record: UserRecord) -> bool:
    return record.has_confirmed_state and record.profile_status is ProfileStatus.MISSING


def _ordinary_budget_exhausted(started: float, budget: float, reserve: float = 35.0) -> bool:
    return time.monotonic() - started >= max(0.0, budget - reserve)


def _fatal_result(results: Iterable[CheckResult]) -> str | None:
    values = set(results)
    if CheckResult.BLOCKED_OR_CHALLENGE in values:
        return "source returned a block/challenge or disallowed the crawler"
    if CheckResult.RATE_LIMITED in values:
        return "source remained rate-limited after polite retries"
    return None


def _updated_state(
    state: CrawlerState,
    *,
    user_id: int,
    record: UserRecord,
) -> CrawlerState:
    highest_existing = state.highest_confirmed_user_id
    if _is_existing(record):
        highest_existing = max(highest_existing, user_id)
    return replace(
        state,
        highest_attempted_id=max(state.highest_attempted_id, user_id),
        highest_confirmed_user_id=highest_existing,
        updated_at=format_utc(),
    )


def _initialization_status(state: CrawlerState) -> InitializationStatus:
    if state.phase is CrawlPhase.MAINTENANCE:
        return InitializationStatus.COMPLETE
    if state.phase is CrawlPhase.PAUSED:
        return InitializationStatus.PAUSED
    return InitializationStatus.IN_PROGRESS


def _finish_crawl_run(
    *,
    store: RepositoryStore,
    state: CrawlerState,
    run_id: str,
    mode: str,
    started_at: str,
    start_id: int | None,
    counters: CrawlCounters,
    stopped_reason: str,
) -> None:
    finished_at = format_utc()
    previous_manifest = store.load_manifest()
    store.append_run(
        RunRecord(
            run_id=run_id,
            mode=mode,
            started_at=started_at,
            finished_at=finished_at,
            start_id=start_id,
            end_id=counters.last_id,
            attempted=counters.attempted,
            confirmed=counters.confirmed,
            changed=counters.changed,
            errors=counters.errors,
            rate_limited=counters.rate_limited,
            stopped_reason=stopped_reason,
            crawler_version=__version__,
        )
    )
    store.rebuild_manifest(
        initialization_status=_initialization_status(state),
        phase=state.phase,
        last_successful_run_at=(
            previous_manifest.last_successful_run_at
            if state.phase is CrawlPhase.PAUSED
            else finished_at
        ),
    )


def _record_counters(counters: CrawlCounters, processed: ProcessedUser) -> None:
    counters.attempted += 1
    counters.last_id = processed.record.id
    counters.confirmed += int(processed.confirmed)
    counters.changed += len(processed.changes)
    if any(result is not CheckResult.OK for result in processed.check_results):
        counters.errors += 1
    if CheckResult.RATE_LIMITED in processed.check_results:
        counters.rate_limited += 1


def _pause_state(state: CrawlerState) -> CrawlerState:
    if state.phase is CrawlPhase.PAUSED:
        return state
    return replace(
        state,
        phase=CrawlPhase.PAUSED,
        paused_from_phase=state.phase,
        updated_at=format_utc(),
    )


def command_bootstrap_chunk(args: argparse.Namespace) -> int:
    store = RepositoryStore(args.root)
    state = store.load_state()
    if state.phase is CrawlPhase.PAUSED:
        raise CommandError(
            "crawler is safety-paused; investigate, then use 'resume --acknowledge REASON'",
            EXIT_SAFETY_STOP,
        )
    if state.phase is CrawlPhase.MAINTENANCE:
        _json_print({"status": "already_complete", "next_id": state.next_id})
        return 0

    started_at = format_utc()
    started_clock = time.monotonic()
    run_id = _new_run_id("bootstrap")
    state = replace(state, last_run_id=run_id, updated_at=format_utc())
    counters = CrawlCounters()
    start_id = state.next_id
    stop_reason = "maximum ID count reached"
    parse_failures = 0
    transient_failures = 0

    with _fetcher_from_args(args) as fetcher:
        try:
            _ensure_robots_allowed(fetcher)
        except CommandError as exc:
            state = _pause_state(state)
            store.save_state(state)
            _finish_crawl_run(
                store=store,
                state=state,
                run_id=run_id,
                mode="bootstrap",
                started_at=started_at,
                start_id=start_id,
                counters=counters,
                stopped_reason=str(exc),
            )
            raise

        session = CrawlSession(
            store,
            fetcher,
            run_id,
            checkpoint_every=args.checkpoint_every,
        )
        try:
            while counters.attempted < args.max_ids:
                if _ordinary_budget_exhausted(started_clock, args.time_budget_seconds):
                    stop_reason = "time budget reached"
                    break

                user_id = state.next_id
                processed = session.process(user_id)
                _record_counters(counters, processed)
                state = _updated_state(state, user_id=user_id, record=processed.record)

                fatal = _fatal_result(processed.check_results)
                if fatal is not None:
                    state = _pause_state(state)
                    stop_reason = fatal
                    session.checkpoint(state, force=True)
                    break

                current_result = processed.check_results[-1]
                parse_failures = (
                    parse_failures + 1 if current_result in _PARSE_FAILURE_RESULTS else 0
                )
                transient_failures = (
                    transient_failures + 1 if current_result in _TRANSIENT_FAILURE_RESULTS else 0
                )
                if parse_failures >= args.max_consecutive_parse_failures:
                    state = _pause_state(state)
                    stop_reason = "consecutive parse/staleness safety threshold reached"
                    session.checkpoint(state, force=True)
                    break
                if transient_failures >= args.max_consecutive_transient_failures:
                    state = _pause_state(state)
                    stop_reason = "consecutive transport/HTTP safety threshold reached"
                    session.checkpoint(state, force=True)
                    break

                if state.phase is CrawlPhase.BOUNDARY_CONFIRMATION:
                    boundary_start = state.boundary_candidate_start
                    if boundary_start is None:
                        raise CommandError("boundary state is missing its candidate start")
                    boundary_end = boundary_start + args.boundary_missing_count - 1
                    current_observation_confirmed = processed.confirmed and all(
                        result is CheckResult.OK for result in processed.check_results
                    )
                    if not current_observation_confirmed:
                        state = replace(state, next_id=boundary_start, updated_at=format_utc())
                        stop_reason = "boundary confirmation was inconclusive"
                        session.checkpoint(state, force=True)
                        break
                    if _is_existing(processed.record):
                        state = replace(
                            state,
                            phase=CrawlPhase.BOOTSTRAP,
                            next_id=user_id + 1,
                            boundary_candidate_start=None,
                            boundary_confirmation_pending=False,
                            consecutive_missing_after_frontier=0,
                            updated_at=format_utc(),
                        )
                    elif not _is_confirmed_missing(processed.record):
                        state = replace(state, next_id=boundary_start, updated_at=format_utc())
                        stop_reason = "boundary confirmation was inconclusive"
                        session.checkpoint(state, force=True)
                        break
                    elif user_id >= boundary_end:
                        state = replace(
                            state,
                            phase=CrawlPhase.MAINTENANCE,
                            next_id=user_id + 1,
                            boundary_candidate_start=None,
                            boundary_confirmation_pending=False,
                            consecutive_missing_after_frontier=0,
                            maintenance_cursor=1,
                            updated_at=format_utc(),
                        )
                        stop_reason = "bootstrap boundary confirmed"
                        session.checkpoint(state, force=True)
                        break
                    else:
                        state = replace(state, next_id=user_id + 1, updated_at=format_utc())
                else:
                    missing = state.consecutive_missing_after_frontier
                    if processed.confirmed and _is_existing(processed.record):
                        missing = 0
                    elif (
                        processed.confirmed
                        and all(result is CheckResult.OK for result in processed.check_results)
                        and _is_confirmed_missing(processed.record)
                        and state.highest_confirmed_user_id
                    ):
                        missing += 1
                    else:
                        missing = 0
                    state = replace(
                        state,
                        next_id=user_id + 1,
                        consecutive_missing_after_frontier=missing,
                        updated_at=format_utc(),
                    )
                    if missing >= args.boundary_missing_count:
                        candidate_start = user_id - args.boundary_missing_count + 1
                        state = replace(
                            state,
                            phase=CrawlPhase.BOUNDARY_CONFIRMATION,
                            next_id=candidate_start,
                            boundary_candidate_start=candidate_start,
                            boundary_confirmation_pending=True,
                            updated_at=format_utc(),
                        )
                        stop_reason = "boundary candidate found; independent pass required"
                        session.checkpoint(state, force=True)
                        break

                session.checkpoint(state)
        finally:
            session.checkpoint(state, force=True)

    _finish_crawl_run(
        store=store,
        state=state,
        run_id=run_id,
        mode="bootstrap",
        started_at=started_at,
        start_id=start_id,
        counters=counters,
        stopped_reason=stop_reason,
    )
    _json_print(
        {
            "attempted": counters.attempted,
            "changed": counters.changed,
            "confirmed": counters.confirmed,
            "errors": counters.errors,
            "next_id": state.next_id,
            "phase": state.phase.value,
            "run_id": run_id,
            "stopped_reason": stop_reason,
        }
    )
    return EXIT_SAFETY_STOP if state.phase is CrawlPhase.PAUSED else 0


def command_update(args: argparse.Namespace) -> int:
    store = RepositoryStore(args.root)
    state = store.load_state()
    if state.phase is CrawlPhase.PAUSED:
        raise CommandError(
            "crawler is safety-paused; investigate, then use 'resume --acknowledge REASON'",
            EXIT_SAFETY_STOP,
        )
    if state.phase is not CrawlPhase.MAINTENANCE:
        raise CommandError("initialization is not complete; run bootstrap first")

    started_at = format_utc()
    started_clock = time.monotonic()
    run_id = _new_run_id("update")
    state = replace(state, last_run_id=run_id, updated_at=format_utc())
    counters = CrawlCounters()
    stop_reason = "maintenance batch completed"
    parse_failures = 0
    transient_failures = 0

    with _fetcher_from_args(args) as fetcher:
        try:
            _ensure_robots_allowed(fetcher)
        except CommandError as exc:
            state = _pause_state(state)
            store.save_state(state)
            _finish_crawl_run(
                store=store,
                state=state,
                run_id=run_id,
                mode="update",
                started_at=started_at,
                start_id=None,
                counters=counters,
                stopped_reason=str(exc),
            )
            raise

        session = CrawlSession(
            store,
            fetcher,
            run_id,
            checkpoint_every=args.checkpoint_every,
        )
        due = select_due_users(
            session.records.values(),
            now=format_utc(),
            limit=args.max_existing,
        )
        new_ids = _frontier_probe_ids(
            session.records.values(),
            state,
            max_new=args.max_new,
            sweep_size=args.frontier_sweep_size,
        )
        new_id_set = set(new_ids)
        # Probe the registration frontier first so a slow batch of old retries
        # cannot starve discovery.  Duplicate due records inside the same sweep
        # are processed exactly once as frontier probes.
        work_items = [(user_id, True) for user_id in new_ids]
        work_items.extend((record.id, False) for record in due if record.id not in new_id_set)
        start_id = work_items[0][0] if work_items else None
        sweep_frontier = frontier_probe_start(session.records.values())
        sweep_end = sweep_frontier + args.frontier_sweep_size

        try:
            for user_id, is_frontier_probe in work_items:
                if _ordinary_budget_exhausted(started_clock, args.time_budget_seconds):
                    stop_reason = "time budget reached"
                    break
                processed = session.process(user_id)
                _record_counters(counters, processed)
                state = _updated_state(state, user_id=user_id, record=processed.record)
                if is_frontier_probe:
                    next_cursor = user_id + 1
                    if next_cursor >= sweep_end:
                        next_cursor = sweep_frontier
                    state = replace(
                        state,
                        maintenance_cursor=next_cursor,
                        updated_at=format_utc(),
                    )

                fatal = _fatal_result(processed.check_results)
                if fatal is not None:
                    state = _pause_state(state)
                    stop_reason = fatal
                    session.checkpoint(state, force=True)
                    break
                current_result = processed.check_results[-1]
                parse_failures = (
                    parse_failures + 1 if current_result in _PARSE_FAILURE_RESULTS else 0
                )
                transient_failures = (
                    transient_failures + 1 if current_result in _TRANSIENT_FAILURE_RESULTS else 0
                )
                if parse_failures >= args.max_consecutive_parse_failures:
                    state = _pause_state(state)
                    stop_reason = "consecutive parse/staleness safety threshold reached"
                    session.checkpoint(state, force=True)
                    break
                if transient_failures >= args.max_consecutive_transient_failures:
                    state = _pause_state(state)
                    stop_reason = "consecutive transport/HTTP safety threshold reached"
                    session.checkpoint(state, force=True)
                    break
                if (
                    counters.attempted >= args.mass_change_minimum_attempts
                    and counters.changed >= args.mass_change_minimum_changes
                    and counters.changed / counters.attempted >= args.mass_change_ratio
                ):
                    state = _pause_state(state)
                    stop_reason = "mass-change safety threshold reached"
                    session.checkpoint(state, force=True)
                    break
                session.checkpoint(state)
        finally:
            session.checkpoint(state, force=True)

    _finish_crawl_run(
        store=store,
        state=state,
        run_id=run_id,
        mode="update",
        started_at=started_at,
        start_id=start_id,
        counters=counters,
        stopped_reason=stop_reason,
    )
    _json_print(
        {
            "attempted": counters.attempted,
            "changed": counters.changed,
            "confirmed": counters.confirmed,
            "errors": counters.errors,
            "phase": state.phase.value,
            "run_id": run_id,
            "stopped_reason": stop_reason,
        }
    )
    return EXIT_SAFETY_STOP if state.phase is CrawlPhase.PAUSED else 0


def _frontier_probe_ids(
    records: Iterable[UserRecord],
    state: CrawlerState,
    *,
    max_new: int,
    sweep_size: int,
) -> list[int]:
    """Return one progressive, periodically wrapping frontier probe batch."""

    if max_new < 1 or sweep_size < max_new:
        raise ValueError("frontier sweep size must be at least max_new")
    frontier = frontier_probe_start(records)
    sweep_end = frontier + sweep_size
    start = max(frontier, state.maintenance_cursor)
    if start >= sweep_end:
        start = frontier
    return [frontier + ((start - frontier + offset) % sweep_size) for offset in range(max_new)]


def _health_due_at(item: Mapping[str, Any] | None):
    if not item or not item.get("checked_at"):
        return parse_utc("1970-01-01T00:00:00Z")
    checked = parse_utc(str(item["checked_at"]))
    try:
        status = LinkHealthStatus(str(item.get("status")))
    except ValueError:
        return parse_utc("1970-01-01T00:00:00Z")
    if status in HEALTHY_STATUSES:
        return checked + timedelta(days=30)
    failures = max(1, int(item.get("consecutive_failures", 1)))
    exponent = min(2, failures - 1)
    return checked + timedelta(hours=min(72, 24 * (2**exponent)))


def _write_link_health(root: Path, health: Mapping[str, Mapping[str, Any]]) -> None:
    content = "".join(canonical_json(health[url]) + "\n" for url in sorted(health))
    atomic_write_text(root / "data" / "link-health.jsonl", content)


def command_check_links(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    store = RepositoryStore(root)
    current_urls = sorted(
        {
            record.blog_url
            for record in store.iter_users()
            if record.profile_status is ProfileStatus.PUBLIC
            and record.blog_status is BlogStatus.PRESENT
            and record.blog_url is not None
        }
    )
    health = load_link_health(root)
    now = utc_now()
    due = sorted(
        (url for url in current_urls if _health_due_at(health.get(url)) <= now),
        key=lambda url: (_health_due_at(health.get(url)), url),
    )[: args.max_links]
    started_at = format_utc()
    run_id = _new_run_id("links")
    attempted = 0
    errors = 0
    started_clock = time.monotonic()
    if due:
        repository_url = resolve_repository_url(args.repository_url)
        user_agent = (
            f"icourse-blog-index/{__version__} (+{repository_url}; external-link health check)"
        )
        with LinkChecker(user_agent=user_agent) as checker:
            for url in due:
                if _ordinary_budget_exhausted(started_clock, args.time_budget_seconds, reserve=15):
                    break
                result = checker.check(url, previous=health.get(url))
                health[url] = result.to_dict()
                attempted += 1
                errors += int(not result.healthy)
                _write_link_health(root, health)

    finished_at = format_utc()
    store.append_run(
        RunRecord(
            run_id=run_id,
            mode="check-links",
            started_at=started_at,
            finished_at=finished_at,
            attempted=attempted,
            errors=errors,
            stopped_reason=("time budget reached" if attempted < len(due) else "batch completed"),
            crawler_version=__version__,
        )
    )
    _json_print(
        {
            "attempted": attempted,
            "due": len(due),
            "errors": errors,
            "run_id": run_id,
        }
    )
    return 0


def command_render(args: argparse.Namespace) -> int:
    result = render_repository(args.root, check=args.check)
    _json_print(result)
    return 0


def command_validate(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    store = RepositoryStore(root)
    records = list(store.iter_users())
    manifest = store.load_manifest()
    report = validate_dataset(store)
    errors = [
        f"manifest {issue.message}" if issue.code == "manifest_mismatch" else issue.message
        for issue in report.errors
    ]
    for warning in report.warnings:
        print(str(warning), file=sys.stderr)

    readme = root / "README.md"
    if not readme.exists():
        errors.append("README.md is missing")
    else:
        text = readme.read_text(encoding="utf-8")
        if text.count(INDEX_BEGIN) != 1 or text.count(INDEX_END) != 1:
            errors.append("README generated index markers are missing or duplicated")

    # Derived views are checked even before initialization.  The committed
    # empty README section and CSV header are deterministic, so there is no
    # reason to leave a pre-bootstrap gap where unreviewed rows could hide.
    try:
        render_repository(root, check=True)
    except RenderError as exc:
        errors.append(str(exc))

    try:
        load_link_health(root)
    except RenderError as exc:
        errors.append(str(exc))
    for schema_path in sorted((root / "schemas").glob("*.json")):
        try:
            json.loads(schema_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{schema_path.relative_to(root)} is invalid JSON: {exc}")

    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return EXIT_INVALID_DATA
    _json_print({"blogs": manifest.blog_count, "records": len(records), "status": "valid"})
    return 0


def command_inspect_user(args: argparse.Namespace) -> int:
    with _fetcher_from_args(args) as fetcher:
        _ensure_robots_allowed(fetcher)
        result = fetcher.fetch_user(args.user_id, cache_bust=args.cache_bust)
        retried_after_stale = False
        if result.ok and result.suspected_stale and not args.cache_bust:
            result = fetcher.fetch_user(args.user_id, cache_bust=True)
            retried_after_stale = True
        from .parser import observation_from_fetch

        observation = observation_from_fetch(result)
    _json_print(
        {
            "cache": {
                "age_seconds": result.cache_metadata.age_seconds,
                "cache_control": result.cache_metadata.cache_control,
                "cf_cache_status": result.cache_metadata.cf_cache_status,
                "retried_after_stale": retried_after_stale,
                "suspected_stale": result.suspected_stale,
                "stale_reasons": list(result.stale_reasons),
            },
            "fetch": {
                "attempts": result.attempts,
                "elapsed_seconds": round(result.elapsed_seconds, 3),
                "final_url": result.final_url,
                "http_status": result.http_status,
                "outcome": result.outcome.value,
            },
            "observation": observation.to_dict(),
        }
    )
    return 0 if observation.successful else 1


def command_resume(args: argparse.Namespace) -> int:
    reason = " ".join(args.acknowledge.split())
    if not reason:
        raise CommandError("--acknowledge requires a non-empty review reason", EXIT_USAGE)
    store = RepositoryStore(args.root)
    state = store.load_state()
    if state.phase is not CrawlPhase.PAUSED:
        raise CommandError("crawler is not paused; no state was changed")
    next_phase = state.paused_from_phase
    if next_phase is None:
        raise CommandError("paused crawler state does not record its previous phase")
    with _fetcher_from_args(args) as fetcher:
        _ensure_robots_allowed(fetcher)
        probe = fetcher.fetch_and_parse(state.next_id, cache_bust=True)
        if not probe.successful:
            raise CommandError(
                f"safety stop: a direct profile probe still reports {probe.check_result.value}",
                EXIT_SAFETY_STOP,
            )

    manifest = store.load_manifest()
    resumed_at = format_utc()
    run_id = _new_run_id("resume")
    state = replace(
        state,
        phase=next_phase,
        paused_from_phase=None,
        last_run_id=run_id,
        updated_at=resumed_at,
    )
    store.save_state(state)
    store.rebuild_manifest(
        initialization_status=(
            InitializationStatus.COMPLETE
            if next_phase is CrawlPhase.MAINTENANCE
            else InitializationStatus.IN_PROGRESS
        ),
        phase=next_phase,
        last_successful_run_at=manifest.last_successful_run_at,
    )
    store.append_run(
        RunRecord(
            run_id=run_id,
            mode="resume",
            started_at=resumed_at,
            finished_at=resumed_at,
            stopped_reason=f"operator acknowledgement: {reason}",
            crawler_version=__version__,
        )
    )
    _json_print({"phase": next_phase.value, "reason": reason, "run_id": run_id})
    return 0


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _ratio(value: str) -> float:
    parsed = float(value)
    if not 0 < parsed <= 1:
        raise argparse.ArgumentTypeError("must be in (0, 1]")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="icourse-blog-index",
        description="Build and maintain the auditable public iCourse blog index.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--root",
        default=os.environ.get("ICOURSE_REPOSITORY_ROOT", "."),
        help="repository root (default: current directory)",
    )
    parser.add_argument(
        "--repository-url",
        default=None,
        help="public HTTPS repository URL used in the crawler User-Agent",
    )
    parser.add_argument("--min-delay-seconds", type=float, default=2.5)
    parser.add_argument("--max-delay-seconds", type=float, default=3.5)
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap = subparsers.add_parser("bootstrap-chunk", help="continue initial ID scan")
    bootstrap.add_argument("--max-ids", type=_positive_int, default=4_000)
    bootstrap.add_argument("--time-budget-seconds", type=_positive_float, default=17_100)
    bootstrap.add_argument("--checkpoint-every", type=_positive_int, default=25)
    bootstrap.add_argument(
        "--boundary-missing-count",
        type=_positive_int,
        default=DEFAULT_POLICY.boundary_missing_count,
        help=argparse.SUPPRESS,
    )
    bootstrap.add_argument("--max-consecutive-parse-failures", type=_positive_int, default=5)
    bootstrap.add_argument("--max-consecutive-transient-failures", type=_positive_int, default=10)
    bootstrap.set_defaults(handler=command_bootstrap_chunk)

    update = subparsers.add_parser("update", help="run one maintenance batch")
    update.add_argument("--max-existing", type=_positive_int, default=500)
    update.add_argument("--max-new", type=_positive_int, default=64)
    update.add_argument("--frontier-sweep-size", type=_positive_int, default=256)
    update.add_argument("--time-budget-seconds", type=_positive_float, default=17_100)
    update.add_argument("--checkpoint-every", type=_positive_int, default=25)
    update.add_argument("--mass-change-minimum-attempts", type=_positive_int, default=20)
    update.add_argument("--mass-change-minimum-changes", type=_positive_int, default=10)
    update.add_argument("--mass-change-ratio", type=_ratio, default=0.25)
    update.add_argument("--max-consecutive-parse-failures", type=_positive_int, default=5)
    update.add_argument("--max-consecutive-transient-failures", type=_positive_int, default=10)
    update.set_defaults(handler=command_update)

    links = subparsers.add_parser("check-links", help="check a bounded external-link batch")
    links.add_argument("--max-links", type=_positive_int, default=200)
    links.add_argument("--time-budget-seconds", type=_positive_float, default=10_800)
    links.set_defaults(handler=command_check_links)

    render = subparsers.add_parser("render", help="regenerate README and CSV views")
    render.add_argument("--check", action="store_true", help="fail instead of writing if stale")
    render.set_defaults(handler=command_render)

    validate = subparsers.add_parser("validate", help="validate canonical and derived data")
    validate.set_defaults(handler=command_validate)

    inspect = subparsers.add_parser("inspect-user", help="live diagnostic without data writes")
    inspect.add_argument("user_id", type=_positive_int)
    inspect.add_argument("--cache-bust", action="store_true")
    inspect.set_defaults(handler=command_inspect_user)

    resume = subparsers.add_parser("resume", help="resume after an investigated safety pause")
    resume.add_argument(
        "--acknowledge",
        required=True,
        metavar="REASON",
        help="non-empty operator review note written to the audit log",
    )
    resume.set_defaults(handler=command_resume)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.min_delay_seconds < 0 or args.max_delay_seconds < args.min_delay_seconds:
        parser.error("request delay range is invalid")
    try:
        return int(args.handler(args))
    except CommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except (DatasetCorruptionError, RenderError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID_DATA
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


__all__ = ["build_parser", "main"]
