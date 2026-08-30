from __future__ import annotations

import pytest

from icourse_blog_index.fetcher import CacheMetadata, FetchOutcome, FetchResult
from icourse_blog_index.models import CheckResult
from icourse_blog_index.parser import (
    UnsafeBlogURL,
    normalize_blog_url,
    observation_from_fetch,
    parse_profile,
)


@pytest.mark.parametrize(
    ("html", "profile_status", "blog_status"),
    [
        ("<main>用户不存在！</main>", "missing", "unknown"),
        ("<main>此用户的个人主页未公开！</main>", "hidden", "unknown"),
        ("<ul><li>博客：暂无</li></ul>", "public", "absent"),
    ],
)
def test_parse_known_profile_states(html: str, profile_status: str, blog_status: str) -> None:
    parsed = parse_profile(html, 42)

    assert parsed.profile_status == profile_status
    assert parsed.blog_status == blog_status
    assert parsed.blog_url is None
    assert parsed.check_result == "ok"


def test_parse_only_uses_labelled_blog_field() -> None:
    parsed = parse_profile(
        """
        <main>
          <p>简介：https://biography.example/ignore-me</p>
          <ul>
            <li>博客：<a href="https://Example.COM:443/math/">
              <bdi>https://Example.COM:443/math/</bdi>
            </a></li>
          </ul>
        </main>
        """,
        11706,
    )

    assert parsed.profile_status == "public"
    assert parsed.blog_status == "present"
    assert parsed.blog_url_raw == "https://Example.COM:443/math/"
    assert parsed.blog_url == "https://example.com/math/"
    assert parsed.check_result == "ok"


def test_parse_plain_labelled_url_without_anchor() -> None:
    parsed = parse_profile("<dl><dd>博客: https://example.org/posts</dd></dl>", 7)

    assert parsed.profile_status == "public"
    assert parsed.blog_status == "present"
    assert parsed.blog_url == "https://example.org/posts"


def test_unknown_markup_is_never_treated_as_missing_or_blog_absent() -> None:
    parsed = parse_profile(
        "<html><title>Profile</title><body><p>template changed</p></body></html>",
        9,
    )

    assert parsed.profile_status == "unknown"
    assert parsed.blog_status == "unknown"
    assert parsed.check_result == "parse_error"
    assert "not found" in (parsed.error or "")


def test_challenge_page_is_unknown() -> None:
    parsed = parse_profile("<title>Just a moment...</title><div id='cf-chl-test'></div>", 10)

    assert parsed.profile_status == "unknown"
    assert parsed.blog_status == "unknown"
    assert parsed.check_result == "parse_error"
    assert "challenge" in (parsed.error or "")


def test_conflicting_blog_fields_are_unknown() -> None:
    parsed = parse_profile(
        """
        <ul>
          <li>博客：<a href="https://one.example">https://one.example</a></li>
          <li>博客：<a href="https://two.example">https://two.example</a></li>
        </ul>
        """,
        11,
    )

    assert parsed.profile_status == "unknown"
    assert parsed.blog_status == "unknown"
    assert parsed.check_result == "parse_error"
    assert "conflicting" in (parsed.error or "")


def test_blog_label_and_target_must_agree() -> None:
    parsed = parse_profile(
        '<li>博客：<a href="https://target.example">https://label.example</a></li>',
        12,
    )

    assert parsed.profile_status == "unknown"
    assert parsed.blog_status == "unknown"
    assert parsed.blog_url is None
    assert "disagree" in (parsed.error or "")


def test_oversized_malformed_blog_value_becomes_bounded_failed_observation() -> None:
    oversized = "https://example.com/" + ("x" * 2_100)
    fetch = FetchResult(
        user_id=12,
        requested_url="https://icourse.club/user/12",
        final_url="https://icourse.club/user/12",
        fetched_at="2026-08-30T12:00:00Z",
        outcome=FetchOutcome.OK,
        http_status=200,
        body=f'<li>博客：<a href="{oversized}">{oversized}</a></li>',
        attempts=1,
        elapsed_seconds=0.1,
    )

    observation = observation_from_fetch(fetch)

    assert observation.check_result is CheckResult.PARSE_ERROR
    assert observation.blog_url is None
    assert observation.blog_url_raw is None
    assert "too long" in (observation.error or "")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" HTTPS://Example.COM:443/ ", "https://example.com/"),
        ("http://Example.COM:80/a?q=1#part", "http://example.com/a?q=1#part"),
        ("https://例子.测试/博客", "https://xn--fsqu00a.xn--0zwm56d/博客"),
    ],
)
def test_normalize_blog_url(raw: str, expected: str) -> None:
    assert normalize_blog_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "javascript:alert(1)",
        "https://user:secret@example.com/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "https://localhost/",
        "https://example.com/%0aInjected",
        "https://example.com/a b",
        "https://example.com\\@attacker.example/",
    ],
)
def test_normalize_blog_url_rejects_unsafe_values(raw: str) -> None:
    with pytest.raises(UnsafeBlogURL):
        normalize_blog_url(raw)


def test_fetch_metadata_marks_suspected_stale_without_changing_parsed_values() -> None:
    parsed = parse_profile(
        '<li>博客：<a href="https://current.example">https://current.example</a></li>',
        11706,
    )
    with_metadata = parsed.with_fetch_metadata(
        observed_at="2026-08-30T12:00:00Z",
        http_status=200,
        suspected_stale=True,
    )

    assert with_metadata.check_result == "suspected_stale"
    assert with_metadata.http_status == 200
    assert with_metadata.observed_at == "2026-08-30T12:00:00Z"
    assert with_metadata.profile_status == parsed.profile_status
    assert with_metadata.blog_url == parsed.blog_url


def test_cached_fetch_becomes_non_confirming_suspected_stale_observation() -> None:
    fetch = FetchResult(
        user_id=11706,
        requested_url="https://icourse.club/user/11706",
        final_url="https://icourse.club/user/11706",
        fetched_at="2026-08-30T12:00:00Z",
        outcome=FetchOutcome.OK,
        http_status=200,
        body=(
            '<li>博客：<a href="https://stardust-math.pages.dev">'
            "https://stardust-math.pages.dev</a></li>"
        ),
        attempts=1,
        elapsed_seconds=0.1,
        cache_metadata=CacheMetadata(cf_cache_status="HIT", age_seconds=300),
        suspected_stale=True,
        stale_reasons=("CF-Cache-Status=HIT", "Age=300"),
    )

    observation = observation_from_fetch(fetch)

    assert fetch.cache_status == "HIT"
    assert fetch.age_seconds == 300
    assert observation.check_result is CheckResult.SUSPECTED_STALE
    assert observation.profile_status.value == "public"
    assert observation.blog_url == "https://stardust-math.pages.dev/"
