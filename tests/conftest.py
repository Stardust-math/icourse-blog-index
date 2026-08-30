from __future__ import annotations

from collections.abc import Callable

import pytest

from icourse_blog_index.models import Observation, UserRecord
from icourse_blog_index.utils import normalize_http_url, semantic_profile_fingerprint


@pytest.fixture
def timestamps() -> tuple[str, str, str, str]:
    return (
        "2026-08-30T00:00:00Z",
        "2026-08-30T00:01:00Z",
        "2026-08-31T00:00:00Z",
        "2027-03-01T00:00:00Z",
    )


@pytest.fixture
def observation_factory() -> Callable[..., Observation]:
    def make(**overrides: object) -> Observation:
        values: dict[str, object] = {
            "user_id": 11706,
            "observed_at": "2026-08-30T00:00:00Z",
            "profile_status": "public",
            "blog_status": "present",
            "blog_url_raw": "https://stardust-math.pages.dev",
            "blog_url": "https://stardust-math.pages.dev",
            "check_result": "ok",
            "http_status": 200,
            "parser_version": "test",
        }
        values.update(overrides)
        return Observation(**values)  # type: ignore[arg-type]

    return make


@pytest.fixture
def record_factory() -> Callable[..., UserRecord]:
    def make(**overrides: object) -> UserRecord:
        values: dict[str, object] = {
            "id": 11706,
            "profile_status": "public",
            "blog_status": "present",
            "blog_url_raw": "https://stardust-math.pages.dev",
            "blog_url": "https://stardust-math.pages.dev/",
            "first_checked_at": "2026-08-01T00:00:00Z",
            "last_checked_at": "2026-08-29T00:00:00Z",
            "last_confirmed_at": "2026-08-29T00:00:00Z",
            "profile_changed_at": "2026-08-01T00:00:00Z",
            "blog_changed_at": "2026-08-01T00:00:00Z",
            "last_check_result": "ok",
            "consecutive_failures": 0,
            "parser_version": "test",
            "source_fingerprint": "baseline",
            "http_status": 200,
        }
        values.update(overrides)
        if "source_fingerprint" not in overrides:
            blog_url = values["blog_url"]
            normalized_url = normalize_http_url(str(blog_url)) if blog_url is not None else None
            values["source_fingerprint"] = semantic_profile_fingerprint(
                str(values["profile_status"]),
                str(values["blog_status"]),
                normalized_url,
            )
        return UserRecord(**values)  # type: ignore[arg-type]

    return make
