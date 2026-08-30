from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import icourse_blog_index.cli as cli
from icourse_blog_index.models import ChangeEvent, CrawlerState, Manifest, RunRecord
from icourse_blog_index.storage import DatasetCorruptionError, RepositoryStore
from icourse_blog_index.utils import canonical_json
from icourse_blog_index.validator import validate_dataset


def _change(*, changed_at: str = "2026-08-30T00:00:01Z") -> ChangeEvent:
    return ChangeEvent(
        user_id=1,
        changed_at=changed_at,
        kind="profile_status",
        old_value="public",
        new_value="hidden",
        run_id="run-1",
    )


def _run(*, finished: bool = True) -> RunRecord:
    return RunRecord(
        run_id="run-1",
        mode="bootstrap",
        started_at="2026-08-30T00:00:00Z",
        finished_at="2026-08-30T00:01:00Z" if finished else None,
        start_id=1,
        end_id=1 if finished else None,
        attempted=1 if finished else 0,
        confirmed=1 if finished else 0,
        changed=1 if finished else 0,
        crawler_version="test",
    )


def _health_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "url": "https://example.com/",
        "status": "reachable",
        "http_status": 200,
        "final_url": "https://example.com/",
        "checked_at": "2026-08-30T00:00:00Z",
        "consecutive_failures": 0,
        "attempts": 1,
        "redirect_count": 0,
        "error": None,
        "failure_confirmed": False,
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize("directory", ["changes", "runs"])
def test_audit_directories_reject_unexpected_jsonl_names(tmp_path: Path, directory: str) -> None:
    store = RepositoryStore(tmp_path)
    store.ensure_directories()
    path = tmp_path / "data" / directory / "private.jsonl"
    path.write_text('{"email":"private@example.com"}\n', encoding="utf-8")

    iterator = store.iter_changes if directory == "changes" else store.iter_runs
    with pytest.raises(DatasetCorruptionError, match="invalid"):
        list(iterator())


def test_append_change_validates_existing_rows_and_nested_shape(tmp_path: Path) -> None:
    store = RepositoryStore(tmp_path)
    store.ensure_directories()
    raw = ChangeEvent(
        user_id=1,
        changed_at="2026-08-30T00:00:01Z",
        kind="blog_changed",
        old_value={"status": "present", "url": "https://old.example/"},
        new_value={"status": "present", "url": "https://new.example/"},
        run_id="run-1",
    ).to_dict()
    raw["old_value"]["email"] = "hidden"  # type: ignore[index]
    path = store.changes_dir / "2026.jsonl"
    path.write_text(canonical_json(raw) + "\n", encoding="utf-8")

    with pytest.raises(DatasetCorruptionError, match="old_value"):
        store.append_change(_change(changed_at="2026-08-30T00:00:02Z"))


def test_append_run_cannot_rewrite_a_finalized_summary(tmp_path: Path) -> None:
    store = RepositoryStore(tmp_path)
    store.append_run(_run())

    with pytest.raises(DatasetCorruptionError, match="cannot overwrite"):
        store.append_run(replace(_run(), stopped_reason="different"))


def test_append_run_allows_only_unfinished_to_finished_transition(tmp_path: Path) -> None:
    store = RepositoryStore(tmp_path)
    assert store.append_run(_run(finished=False)) == 1
    assert store.append_run(_run()) == 1
    assert store.append_run(_run()) == 0


def test_validate_dataset_reads_bad_audit_json(tmp_path: Path) -> None:
    store = RepositoryStore(tmp_path)
    store.ensure_directories()
    store.write_manifest(Manifest())
    store.save_state(CrawlerState())
    (store.runs_dir / "2026.jsonl").write_text("{bad json}\n", encoding="utf-8")

    report = validate_dataset(store)
    assert [issue.code for issue in report.errors] == ["dataset_read"]


def test_link_health_loader_enforces_exact_fields_types_and_sorted_urls(
    tmp_path: Path,
) -> None:
    store = RepositoryStore(tmp_path)
    store.ensure_directories()
    path = store.data_dir / "link-health.jsonl"
    bad = _health_row(email="private@example.com")
    path.write_text(canonical_json(bad) + "\n", encoding="utf-8")
    with pytest.raises(DatasetCorruptionError, match="unknown field.*email"):
        store.load_link_health()

    wrong_type = _health_row(attempts="1")
    path.write_text(canonical_json(wrong_type) + "\n", encoding="utf-8")
    with pytest.raises(DatasetCorruptionError, match="attempts must be an integer"):
        store.load_link_health()


def test_blocked_private_redirect_is_valid_audit_data(tmp_path: Path) -> None:
    store = RepositoryStore(tmp_path)
    store.ensure_directories()
    row = _health_row(
        status="blocked",
        http_status=None,
        final_url="http://127.0.0.1/admin",
        consecutive_failures=1,
        error="redirect target is not public",
    )
    (store.data_dir / "link-health.jsonl").write_text(canonical_json(row) + "\n", encoding="utf-8")

    assert store.load_link_health()["https://example.com/"] == row


def test_cli_validate_rejects_duplicate_json_keys_at_any_depth(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = RepositoryStore(tmp_path)
    store.ensure_directories()
    store.write_manifest(Manifest())
    store.save_state(CrawlerState())
    (tmp_path / "README.md").write_text("# Test\n", encoding="utf-8")
    (store.data_dir / "link-health.jsonl").write_text(
        '{"attempts":1,"attempts":2}\n', encoding="utf-8"
    )

    assert cli.main(["--root", str(tmp_path), "validate"]) == cli.EXIT_INVALID_DATA
    assert "duplicate JSON key 'attempts'" in capsys.readouterr().err


def test_manifest_rejects_nested_duplicate_json_keys(tmp_path: Path) -> None:
    store = RepositoryStore(tmp_path)
    store.ensure_directories()
    raw = json.dumps(Manifest().to_dict())
    raw = raw.replace('"public": 0', '"public": 0, "public": 1')
    store.manifest_path.write_text(raw, encoding="utf-8")

    with pytest.raises(DatasetCorruptionError, match="duplicate JSON key 'public'"):
        store.load_manifest()


@pytest.mark.parametrize(
    "relative_path",
    ["data/private.json", "state/email.txt"],
)
def test_dataset_layout_rejects_unexpected_data_and_state_entries(
    tmp_path: Path, relative_path: str
) -> None:
    store = RepositoryStore(tmp_path)
    store.ensure_directories()
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("private", encoding="utf-8")

    with pytest.raises(DatasetCorruptionError, match="unexpected entry"):
        store.validate_layout()
