from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import icourse_blog_index.cli as cli
from icourse_blog_index.fetcher import CacheMetadata, FetchOutcome, FetchResult
from icourse_blog_index.models import CrawlerState, Manifest, Observation, apply_observation
from icourse_blog_index.renderer import INDEX_BEGIN, INDEX_END, render_repository
from icourse_blog_index.storage import RepositoryStore


def _repository_skeleton(root: Path) -> RepositoryStore:
    store = RepositoryStore(root)
    store.ensure_directories()
    store.write_manifest(Manifest())
    store.save_state(CrawlerState())
    (root / "README.md").write_text(
        f"# Test repository\n\n{INDEX_BEGIN}\nplaceholder\n{INDEX_END}\n",
        encoding="utf-8",
    )
    render_repository(root)
    return store


def test_render_check_is_a_read_only_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repository_skeleton(tmp_path)
    assert cli.main(["--root", str(tmp_path), "render"]) == 0
    capsys.readouterr()
    readme = tmp_path / "README.md"
    csv_path = tmp_path / "data" / "blogs.csv"
    before = {readme: readme.read_bytes(), csv_path: csv_path.read_bytes()}

    assert cli.main(["--root", str(tmp_path), "render", "--check"]) == 0
    output = json.loads(capsys.readouterr().out)

    assert output == {"blogs": 0, "records": 0}
    assert {path: path.read_bytes() for path in before} == before


def test_validate_accepts_a_pristine_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repository_skeleton(tmp_path)

    assert cli.main(["--root", str(tmp_path), "validate"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "blogs": 0,
        "records": 0,
        "status": "valid",
    }


def test_validate_rejects_tampered_prebootstrap_csv(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repository_skeleton(tmp_path)
    (tmp_path / "data" / "blogs.csv").write_text(
        "user_id,profile_url,blog_url,health_status,last_confirmed_at\n"
        "1,https://icourse.club/user/1,https://private.example/,unchecked,\n",
        encoding="utf-8",
    )

    assert cli.main(["--root", str(tmp_path), "validate"]) == cli.EXIT_INVALID_DATA
    assert "generated files are stale: data/blogs.csv" in capsys.readouterr().err


def test_validate_reports_manifest_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _repository_skeleton(tmp_path)
    store.write_manifest(Manifest(record_count=99))

    assert cli.main(["--root", str(tmp_path), "validate"]) == cli.EXIT_INVALID_DATA
    assert "manifest record_count=99, expected 0" in capsys.readouterr().err


def test_bootstrap_refuses_safety_paused_state_without_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _repository_skeleton(tmp_path)
    store.save_state(CrawlerState(phase="paused"))

    code = cli.main(
        [
            "--root",
            str(tmp_path),
            "--repository-url",
            "https://github.com/example/repo",
            "bootstrap-chunk",
            "--max-ids",
            "1",
        ]
    )

    assert code == cli.EXIT_SAFETY_STOP
    assert "safety-paused" in capsys.readouterr().err


def test_update_refuses_to_run_before_bootstrap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repository_skeleton(tmp_path)

    code = cli.main(
        [
            "--root",
            str(tmp_path),
            "--repository-url",
            "https://github.com/example/repo",
            "update",
        ]
    )

    assert code == 1
    assert "initialization is not complete" in capsys.readouterr().err


class _BootstrapFetcher:
    def __init__(self) -> None:
        self.cache_bust_values: list[bool] = []

    def __enter__(self) -> "_BootstrapFetcher":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def check_robots(self, *, force: bool = False) -> SimpleNamespace:
        assert force is True
        return SimpleNamespace(allowed=True, reason="allowed")

    def fetch_and_parse(self, user_id: int, *, cache_bust: bool = False) -> Observation:
        self.cache_bust_values.append(cache_bust)
        observed_at = "2026-08-30T00:01:00Z" if cache_bust else "2026-08-30T00:00:00Z"
        return Observation(
            user_id=user_id,
            observed_at=observed_at,
            profile_status="public",
            blog_status="present",
            blog_url_raw="https://stardust-math.pages.dev",
            blog_url="https://stardust-math.pages.dev/",
            check_result="ok",
            http_status=200,
            parser_version="test",
        )


def test_bootstrap_checkpoint_confirms_new_blog_twice_without_live_network(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _repository_skeleton(tmp_path)
    fetcher = _BootstrapFetcher()
    monkeypatch.setattr(cli, "_fetcher_from_args", lambda args: fetcher)

    code = cli.main(
        [
            "--root",
            str(tmp_path),
            "--repository-url",
            "https://github.com/example/repo",
            "bootstrap-chunk",
            "--max-ids",
            "1",
            "--time-budget-seconds",
            "60",
            "--checkpoint-every",
            "1",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["attempted"] == 1
    assert output["confirmed"] == 1
    assert fetcher.cache_bust_values == [False, True]
    record = store.load_user(1)
    assert record is not None
    assert record.blog_url == "https://stardust-math.pages.dev/"
    assert record.pending_observation is None
    assert store.load_state().next_id == 2


def test_confirmed_public_profile_recheck_starts_with_cache_bust(tmp_path: Path) -> None:
    store = _repository_skeleton(tmp_path)
    first = Observation(
        user_id=1,
        observed_at="2026-08-29T00:00:00Z",
        profile_status="public",
        blog_status="present",
        blog_url="https://stardust-math.pages.dev/",
        check_result="ok",
        parser_version="test",
    )
    pending = apply_observation(None, first)
    confirmed = apply_observation(
        pending.record,
        Observation(
            user_id=1,
            observed_at="2026-08-29T00:01:00Z",
            profile_status="public",
            blog_status="present",
            blog_url="https://stardust-math.pages.dev/",
            check_result="ok",
            parser_version="test",
        ),
    )
    store.upsert_users([confirmed.record])
    fetcher = _BootstrapFetcher()

    session = cli.CrawlSession(store, fetcher, "test-run", checkpoint_every=1)
    processed = session.process(1)

    assert processed.confirmed
    assert fetcher.cache_bust_values == [True]


class _InspectFetcher:
    def __init__(self) -> None:
        self.cache_bust: bool | None = None

    def __enter__(self) -> "_InspectFetcher":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def check_robots(self, *, force: bool = False) -> SimpleNamespace:
        return SimpleNamespace(allowed=True, reason="allowed")

    def fetch_user(self, user_id: int, *, cache_bust: bool = False) -> FetchResult:
        self.cache_bust = cache_bust
        return FetchResult(
            user_id=user_id,
            requested_url=f"https://icourse.club/user/{user_id}",
            final_url=f"https://icourse.club/user/{user_id}",
            fetched_at="2026-08-30T00:00:00Z",
            outcome=FetchOutcome.OK,
            http_status=200,
            body="<li>博客：暂无</li>",
            attempts=1,
            elapsed_seconds=0.1,
            cache_metadata=CacheMetadata(cf_cache_status="DYNAMIC"),
        )


def test_inspect_user_is_read_only_and_reports_direct_observation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = _InspectFetcher()
    monkeypatch.setattr(cli, "_fetcher_from_args", lambda args: fetcher)

    assert (
        cli.main(
            [
                "--root",
                str(tmp_path),
                "--repository-url",
                "https://github.com/example/repo",
                "inspect-user",
                "11706",
                "--cache-bust",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)

    assert output["fetch"]["outcome"] == "ok"
    assert output["cache"]["cf_cache_status"] == "DYNAMIC"
    assert output["observation"]["user_id"] == 11706
    assert output["observation"]["blog_status"] == "absent"
    assert fetcher.cache_bust is True
    assert list(tmp_path.iterdir()) == []


def test_positive_cli_limits_are_enforced() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["bootstrap-chunk", "--max-ids", "0"])
    assert exc_info.value.code == cli.EXIT_USAGE


def test_render_check_detects_stale_files_without_overwriting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _repository_skeleton(tmp_path)
    render_repository(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace("## 数据状态", "## STALE"),
        encoding="utf-8",
    )
    before = readme.read_bytes()

    code = cli.main(["--root", str(tmp_path), "render", "--check"])

    assert code == cli.EXIT_INVALID_DATA
    assert readme.read_bytes() == before
    assert "generated files are stale" in capsys.readouterr().err
