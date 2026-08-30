from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from icourse_blog_index.fetcher import (
    FetchOutcome,
    FetcherConfig,
    ProfileFetcher,
    resolve_repository_url,
)


ROBOTS_ALLOW = "User-agent: *\nAllow: /\n"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )


def _config(**overrides: object) -> FetcherConfig:
    values: dict[str, object] = {
        "repository_url": "https://github.com/example/icourse-blog-index",
        "min_delay_seconds": 0,
        "max_delay_seconds": 0,
        "max_attempts": 1,
        "backoff_base_seconds": 0,
        "backoff_cap_seconds": 0,
    }
    values.update(overrides)
    return FetcherConfig(**values)  # type: ignore[arg-type]


def test_repository_url_resolution_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ICOURSE_REPOSITORY_URL", "https://github.com/environment/index")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.example.edu")
    monkeypatch.setenv("GITHUB_REPOSITORY", "actions/index")

    assert (
        resolve_repository_url("https://github.com/explicit/index/")
        == "https://github.com/explicit/index"
    )
    assert resolve_repository_url() == "https://github.com/environment/index"

    monkeypatch.delenv("ICOURSE_REPOSITORY_URL")
    assert resolve_repository_url() == "https://github.example.edu/actions/index"


def test_repository_url_is_required_and_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ICOURSE_REPOSITORY_URL", "GITHUB_SERVER_URL", "GITHUB_REPOSITORY"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match="repository URL is required"):
        resolve_repository_url()
    with pytest.raises(ValueError, match="credential-free HTTPS"):
        resolve_repository_url("http://github.com/example/index")
    with pytest.raises(ValueError, match="credential-free HTTPS"):
        resolve_repository_url("https://github.com/example/index?token=secret")


def test_robots_allow_and_absent_are_permissive() -> None:
    for response in (
        httpx.Response(200, text=ROBOTS_ALLOW),
        httpx.Response(404, text="not found"),
    ):
        client = _client(lambda request, response=response: response)
        try:
            fetcher = ProfileFetcher(_config(), client=client)
            result = fetcher.check_robots()
        finally:
            client.close()

        assert result.allowed is True


def test_robots_disallow_prevents_any_profile_request() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text="User-agent: *\nDisallow: /user/\n")

    with _client(handler) as client:
        fetcher = ProfileFetcher(_config(), client=client)
        robots = fetcher.check_robots()
        result = fetcher.fetch_user(11706)

    assert robots.allowed is False
    assert result.outcome is FetchOutcome.ROBOTS_DISALLOWED
    assert result.attempts == 0
    assert requested == ["https://icourse.club/robots.txt"]


def test_robots_rules_are_applied_to_each_exact_profile_url() -> None:
    requested: list[str] = []
    rules = "\n".join(
        (
            "User-agent: *",
            "Disallow: /user/11706",
            "Allow: /user/1",
            "",
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=rules)
        return httpx.Response(
            200, text="<li>博客：暂无</li>", headers={"Content-Type": "text/html"}
        )

    with _client(handler) as client:
        fetcher = ProfileFetcher(_config(), client=client)
        policy = fetcher.check_robots()
        blocked = fetcher.fetch_user(11706)

    assert policy.allowed is True
    assert blocked.outcome is FetchOutcome.ROBOTS_DISALLOWED
    assert blocked.attempts == 0
    assert requested == ["https://icourse.club/robots.txt"]


def test_robots_rules_are_applied_to_cache_busting_query() -> None:
    requested: list[str] = []
    rules = "\n".join(
        (
            "User-agent: *",
            "Disallow: /user/11706?_icbi_fresh",
            "Allow: /user/1",
            "",
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=rules)

    with _client(handler) as client:
        blocked = ProfileFetcher(_config(), client=client).fetch_user(
            11706,
            cache_bust=True,
            cache_bust_token="policy-test",
        )

    assert blocked.outcome is FetchOutcome.ROBOTS_DISALLOWED
    assert blocked.attempts == 0
    assert requested == ["https://icourse.club/robots.txt"]


@pytest.mark.parametrize("status", [429, 500, 503])
def test_robots_unavailable_fails_closed(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"Retry-After": "5"})

    with _client(handler) as client:
        result = ProfileFetcher(_config(), client=client).fetch_user(1)

    assert result.outcome is FetchOutcome.ROBOTS_UNAVAILABLE
    assert result.attempts == 0
    assert result.body is None


def test_profile_request_has_identifiable_user_agent_and_no_cache_headers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        return httpx.Response(
            200, text="<li>博客：暂无</li>", headers={"Content-Type": "text/html"}
        )

    with _client(handler) as client:
        result = ProfileFetcher(_config(), client=client).fetch_user(8)

    assert result.outcome is FetchOutcome.OK
    profile_request = requests[-1]
    assert profile_request.url == httpx.URL("https://icourse.club/user/8")
    assert profile_request.headers["cache-control"] == "no-cache, max-age=0"
    assert profile_request.headers["pragma"] == "no-cache"
    assert "icourse-blog-index/" in profile_request.headers["user-agent"]
    assert "https://github.com/example/icourse-blog-index" in profile_request.headers["user-agent"]


@pytest.mark.parametrize(
    ("exception_factory", "expected"),
    [
        (lambda request: httpx.ReadTimeout("timed out", request=request), FetchOutcome.TIMEOUT),
        (
            lambda request: httpx.ConnectError("connection failed", request=request),
            FetchOutcome.NETWORK_ERROR,
        ),
    ],
)
def test_transport_exceptions_are_soft_results(
    exception_factory: Callable[[httpx.Request], Exception], expected: FetchOutcome
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        raise exception_factory(request)

    with _client(handler) as client:
        result = ProfileFetcher(_config(), client=client).fetch_user(9)

    assert result.outcome is expected
    assert result.attempts == 1
    assert result.body is None
    assert result.error


def test_cache_hit_and_age_mark_success_as_suspected_stale() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        return httpx.Response(
            200,
            text="<li>博客：暂无</li>",
            headers={
                "Content-Type": "text/html; charset=utf-8",
                "CF-Cache-Status": "HIT",
                "Age": "300",
            },
        )

    with _client(handler) as client:
        result = ProfileFetcher(_config(), client=client).fetch_user(10)

    assert result.outcome is FetchOutcome.OK
    assert result.ok is True
    assert result.suspected_stale is True
    assert result.cache_status == "HIT"
    assert result.age_seconds == 300
    assert result.stale_reasons == ("CF-Cache-Status=HIT", "Age=300")


def test_redirect_is_rejected_without_following() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        return httpx.Response(302, headers={"Location": "https://example.com/login"})

    with _client(handler) as client:
        result = ProfileFetcher(_config(), client=client).fetch_user(11)

    assert result.outcome is FetchOutcome.REDIRECT_REJECTED
    assert result.attempts == 1
    assert len(requests) == 2
    assert all("example.com/login" not in url for url in requests)


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (403, "forbidden", FetchOutcome.BLOCKED_OR_CHALLENGE),
        (
            200,
            "<title>Just a moment...</title><div id='cf-chl-x'></div>",
            FetchOutcome.BLOCKED_OR_CHALLENGE,
        ),
        (429, "rate limited", FetchOutcome.RATE_LIMITED),
    ],
)
def test_blocking_responses_stop_immediately(
    status: int, body: str, expected: FetchOutcome
) -> None:
    profile_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal profile_attempts
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        profile_attempts += 1
        return httpx.Response(status, text=body, headers={"Retry-After": "120"})

    with _client(handler) as client:
        result = ProfileFetcher(
            _config(max_attempts=3, max_retry_after_seconds=0), client=client
        ).fetch_user(12)

    assert result.outcome is expected
    assert result.attempts == 1
    assert profile_attempts == 1


def test_response_size_limit_is_a_soft_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"0123456789", headers={"Content-Type": "text/html"})

    with _client(handler) as client:
        result = ProfileFetcher(_config(max_response_bytes=5), client=client).fetch_user(13)

    assert result.outcome is FetchOutcome.RESPONSE_TOO_LARGE
    assert result.body is None
    assert result.attempts == 1


def test_non_html_success_is_not_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        return httpx.Response(200, content=b"{}", headers={"Content-Type": "application/json"})

    with _client(handler) as client:
        result = ProfileFetcher(_config(), client=client).fetch_user(14)

    assert result.outcome is FetchOutcome.INVALID_CONTENT_TYPE
    assert result.body is None


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_all_requests_are_serially_rate_limited() -> None:
    clock = _Clock()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        return httpx.Response(
            200, text="<li>博客：暂无</li>", headers={"Content-Type": "text/html"}
        )

    with _client(handler) as client:
        fetcher = ProfileFetcher(
            _config(min_delay_seconds=2.5, max_delay_seconds=2.5),
            client=client,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            random_uniform=lambda lower, upper: lower,
        )
        first = fetcher.fetch_user(1)
        second = fetcher.fetch_user(2)

    assert first.ok and second.ok
    assert requested == ["/robots.txt", "/user/1", "/user/2"]
    assert clock.sleeps == [2.5, 2.5]


def test_cache_busting_only_adds_a_query_to_the_direct_profile_url() -> None:
    requested: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        return httpx.Response(
            200, text="<li>博客：暂无</li>", headers={"Content-Type": "text/html"}
        )

    with _client(handler) as client:
        result = ProfileFetcher(_config(), client=client).fetch_user(
            11706,
            cache_bust=True,
            cache_bust_token="test-token",
        )

    assert result.ok
    assert requested[-1].path == "/user/11706"
    assert requested[-1].params["_icbi_fresh"] == "test-token"
