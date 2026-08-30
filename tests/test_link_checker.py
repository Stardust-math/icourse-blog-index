from __future__ import annotations

import socket
import ssl
from datetime import UTC, datetime
from pathlib import Path

import httpx
import httpcore
import pytest

import icourse_blog_index.link_checker as link_checker_module
from icourse_blog_index.link_checker import (
    LinkChecker,
    LinkHealthResult,
    LinkHealthStatus,
)
from icourse_blog_index.storage import DatasetCorruptionError, RepositoryStore
from icourse_blog_index.utils import canonical_json

NOW = datetime(2026, 8, 30, 12, 34, 56, tzinfo=UTC)
PUBLIC_V4 = "93.184.216.34"


def resolver_for(mapping: dict[str, list[str]]):
    def resolve(hostname: str, _port: int) -> list[str]:
        return mapping[hostname]

    return resolve


def make_checker(
    handler,
    *,
    dns: dict[str, list[str]] | None = None,
    **kwargs,
) -> LinkChecker:
    return LinkChecker(
        transport=httpx.MockTransport(handler),
        resolver=resolver_for(dns or {"example.test": [PUBLIC_V4]}),
        sleeper=lambda _seconds: None,
        clock=lambda: NOW,
        max_retries=kwargs.pop("max_retries", 0),
        **kwargs,
    )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.test/file",
        "file:///etc/passwd",
        "https://user:password@example.test/",
        "https://example.test\\@127.0.0.1/",
        "https://[fe80::1%25eth0]/",
        "https:///missing-host",
    ],
)
def test_rejects_unsupported_or_ambiguous_urls_without_a_request(url: str) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    with make_checker(handler) as checker:
        result = checker.check(url)

    assert result.status is LinkHealthStatus.BLOCKED
    assert result.http_status is None
    assert calls == 0


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.1",  # private
        "127.0.0.1",  # loopback
        "169.254.169.254",  # link-local metadata endpoint
        "224.0.0.1",  # multicast
        "0.0.0.0",  # unspecified
        "240.0.0.1",  # reserved
        "::1",  # IPv6 loopback
        "fc00::1",  # IPv6 unique-local/private
        "fe80::1",  # IPv6 link-local
        "ff02::1",  # IPv6 multicast
        "::",  # IPv6 unspecified
        "2001:db8::1",  # IPv6 documentation/reserved
        "::ffff:127.0.0.1",  # mapped IPv4 loopback
    ],
)
def test_blocks_non_public_ipv4_and_ipv6(address: str) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    with make_checker(handler, dns={"example.test": [address]}) as checker:
        result = checker.check("https://example.test/")

    assert result.status is LinkHealthStatus.BLOCKED
    assert "non-public address" in (result.error or "")
    assert calls == 0


def test_blocks_entire_dns_answer_if_any_address_is_private() -> None:
    def unexpected(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("transport must not be reached")

    with make_checker(
        unexpected,
        dns={"example.test": [PUBLIC_V4, "127.0.0.1"]},
    ) as checker:
        result = checker.check("https://example.test/")

    assert result.status is LinkHealthStatus.BLOCKED


def test_pins_validated_ips_checks_redirect_dns_and_never_sends_cookies() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            assert request.url.host == PUBLIC_V4
            assert request.headers["host"] == "example.test"
            return httpx.Response(
                302,
                headers={
                    "Location": "https://other.test/new-path?x=1",
                    "Set-Cookie": "secret=must-not-be-replayed; Path=/",
                },
            )
        assert request.url.host == "1.1.1.1"
        assert request.headers["host"] == "other.test"
        assert "cookie" not in request.headers
        return httpx.Response(200, content=b"ok")

    dns = {"example.test": [PUBLIC_V4], "other.test": ["1.1.1.1"]}
    with make_checker(handler, dns=dns) as checker:
        result = checker.check("https://example.test/start")

    assert result.status is LinkHealthStatus.REDIRECTED
    assert result.http_status == 200
    assert result.final_url == "https://other.test/new-path?x=1"
    assert result.redirect_count == 1
    assert len(requests) == 2


def test_private_redirect_is_blocked_before_second_request(tmp_path: Path) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"})

    with make_checker(handler) as checker:
        result = checker.check("https://example.test/")

    assert result.status is LinkHealthStatus.BLOCKED
    assert result.http_status is None
    assert result.final_url == "http://127.0.0.1/admin"
    assert calls == 1

    health_path = tmp_path / "data" / "link-health.jsonl"
    health_path.parent.mkdir(parents=True)
    health_path.write_text(canonical_json(result.to_dict()) + "\n", encoding="utf-8")
    assert RepositoryStore(tmp_path).load_link_health()[result.url] == result.to_dict()


def test_store_rejects_private_final_url_for_non_blocked_result(tmp_path: Path) -> None:
    result = LinkHealthResult(
        url="https://example.test/",
        status=LinkHealthStatus.CLIENT_ERROR,
        http_status=404,
        final_url="http://127.0.0.1/admin",
        checked_at="2026-08-30T12:34:56Z",
        consecutive_failures=1,
        attempts=1,
        redirect_count=1,
        error="HTTP 404",
    )
    health_path = tmp_path / "data" / "link-health.jsonl"
    health_path.parent.mkdir(parents=True)
    health_path.write_text(canonical_json(result.to_dict()) + "\n", encoding="utf-8")

    with pytest.raises(DatasetCorruptionError, match="final_url must be public"):
        RepositoryStore(tmp_path).load_link_health()


@pytest.mark.parametrize("http_status", [200, 403, 429, 500])
def test_store_rejects_unsafe_client_error_http_semantics(tmp_path: Path, http_status: int) -> None:
    result = LinkHealthResult(
        url="https://example.test/",
        status=LinkHealthStatus.CLIENT_ERROR,
        http_status=http_status,
        final_url="https://example.test/",
        checked_at="2026-08-30T12:34:56Z",
        consecutive_failures=1,
        attempts=1,
        redirect_count=0,
        error=f"HTTP {http_status}",
    )
    health_path = tmp_path / "data" / "link-health.jsonl"
    health_path.parent.mkdir(parents=True)
    health_path.write_text(canonical_json(result.to_dict()) + "\n", encoding="utf-8")

    with pytest.raises(DatasetCorruptionError, match="client_error"):
        RepositoryStore(tmp_path).load_link_health()


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (200, LinkHealthStatus.REACHABLE),
        (204, LinkHealthStatus.REACHABLE),
        (403, LinkHealthStatus.BLOCKED),
        (404, LinkHealthStatus.CLIENT_ERROR),
        (429, LinkHealthStatus.BLOCKED),
        (500, LinkHealthStatus.SERVER_ERROR),
    ],
)
def test_classifies_http_statuses_conservatively(
    status_code: int, expected: LinkHealthStatus
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    with make_checker(handler) as checker:
        result = checker.check("https://example.test/")

    assert result.status is expected
    assert result.http_status == status_code


def test_cloudflare_style_503_challenge_is_blocked_not_dead() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            headers={"CF-Mitigated": "challenge"},
            content=b"Just a moment",
        )

    with make_checker(handler) as checker:
        result = checker.check("https://example.test/")

    assert result.status is LinkHealthStatus.BLOCKED
    assert result.http_status == 503


class TrackingStream(httpx.SyncByteStream):
    def __init__(self) -> None:
        self.yield_count = 0
        self.closed = False

    def __iter__(self):
        for _ in range(100):
            self.yield_count += 1
            yield b"01234567"

    def close(self) -> None:
        self.closed = True


def test_streams_only_a_bounded_prefix_and_closes_response() -> None:
    stream = TrackingStream()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    with make_checker(handler, max_body_bytes=10) as checker:
        result = checker.check("https://example.test/")

    assert result.status is LinkHealthStatus.REACHABLE
    assert stream.yield_count < 100
    assert stream.closed


class ManualMonotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class DripStream(httpx.SyncByteStream):
    """Yield often enough to avoid an idle timeout but never finish promptly."""

    def __init__(self, monotonic: ManualMonotonic) -> None:
        self.monotonic = monotonic
        self.yield_count = 0
        self.closed = False

    def __iter__(self):
        while True:
            self.monotonic.advance(9.0)
            self.yield_count += 1
            yield b"x"

    def close(self) -> None:
        self.closed = True


def test_total_per_hop_deadline_stops_a_slow_drip_response() -> None:
    monotonic = ManualMonotonic()
    stream = DripStream(monotonic)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    with make_checker(
        handler,
        max_body_bytes=1_000,
        max_response_seconds=30,
        monotonic=monotonic,
    ) as checker:
        result = checker.check("https://example.test/")

    assert result.status is LinkHealthStatus.TIMEOUT
    assert result.attempts == 1
    assert stream.yield_count == 4
    assert stream.closed


class RecordingNetworkStream(httpcore.NetworkStream):
    def __init__(self) -> None:
        self.read_timeouts: list[float | None] = []

    def read(self, _max_bytes: int, timeout: float | None = None) -> bytes:
        self.read_timeouts.append(timeout)
        return b"x"

    def write(self, _buffer: bytes, _timeout: float | None = None) -> None:
        return None

    def close(self) -> None:
        return None

    def start_tls(self, *_args, **_kwargs) -> httpcore.NetworkStream:
        return self


def test_absolute_socket_deadline_shrinks_each_read_timeout() -> None:
    monotonic = ManualMonotonic()
    underlying = RecordingNetworkStream()
    stream = link_checker_module._DeadlineStream(underlying, 30.0, monotonic)

    monotonic.advance(9)
    assert stream.read(1, timeout=12) == b"x"
    monotonic.advance(9)
    assert stream.read(1, timeout=12) == b"x"
    monotonic.advance(9)
    assert stream.read(1, timeout=12) == b"x"
    assert underlying.read_timeouts == [12, 12, 3]

    monotonic.advance(3)
    with pytest.raises(httpcore.ReadTimeout, match="total per-hop"):
        stream.read(1, timeout=12)


def test_retries_a_timeout_without_incrementing_scheduled_failure_count() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200)

    with make_checker(handler, max_retries=1, retry_backoff=0) as checker:
        result = checker.check("https://example.test/")

    assert result.status is LinkHealthStatus.REACHABLE
    assert result.attempts == 2
    assert result.consecutive_failures == 0


def test_failure_confirmation_uses_prior_scheduled_record() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with make_checker(handler, failure_confirmation_count=2) as checker:
        first = checker.check("https://example.test/")
        second = checker.check("https://example.test/", previous=first)

    def recovered_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    assert first.consecutive_failures == 1
    assert not first.failure_confirmed
    assert second.consecutive_failures == 2
    assert second.failure_confirmed

    with make_checker(recovered_handler) as checker:
        recovered = checker.check("https://example.test/", previous=second.to_dict())
    assert recovered.consecutive_failures == 0
    assert not recovered.failure_confirmed


def test_dns_error_and_tls_error_are_distinguished() -> None:
    def dns_failure(_hostname: str, _port: int):
        raise socket.gaierror(socket.EAI_NONAME, "not found")

    def unused(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected after DNS failure")

    checker = LinkChecker(
        transport=httpx.MockTransport(unused),
        resolver=dns_failure,
        max_retries=0,
        clock=lambda: NOW,
    )
    with checker:
        dns_result = checker.check("https://example.test/")
    assert dns_result.status is LinkHealthStatus.DNS_ERROR

    def tls_failure(request: httpx.Request) -> httpx.Response:
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed")
        except ssl.SSLCertVerificationError as exc:
            raise httpx.ConnectError("TLS failed", request=request) from exc

    with make_checker(tls_failure) as checker:
        tls_result = checker.check("https://example.test/")
    assert tls_result.status is LinkHealthStatus.TLS_ERROR


def test_redirect_loop_and_redirect_limit_are_bounded() -> None:
    def loop_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "/"})

    with make_checker(loop_handler) as checker:
        loop = checker.check("https://example.test/")
    assert loop.status is LinkHealthStatus.CLIENT_ERROR
    assert loop.error == "redirect loop detected"

    calls = 0

    def endless_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": f"/step-{calls}"})

    with make_checker(endless_handler, max_redirects=2) as checker:
        limited = checker.check("https://example.test/start")
    assert limited.status is LinkHealthStatus.CLIENT_ERROR
    assert limited.error == "redirect limit exceeded"
    assert calls == 3


def test_result_is_json_serializable_shape() -> None:
    result = LinkHealthResult(
        url="https://example.test/",
        status=LinkHealthStatus.REACHABLE,
        http_status=200,
        final_url="https://example.test/",
        checked_at="2026-08-30T12:34:56Z",
        consecutive_failures=0,
        attempts=1,
        redirect_count=0,
    )
    assert result.to_dict()["status"] == "reachable"
