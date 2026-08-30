"""Conservative, SSRF-resistant health checks for user-supplied blog URLs.

The checker deliberately does less than a browser: it executes no JavaScript,
stores/sends no cookies, follows only ordinary HTTP redirects, and reads only a
small response prefix.  Each redirect target is resolved and checked again.

DNS validation alone has a time-of-check/time-of-use gap.  To avoid that gap,
requests are sent to one of the already validated IP addresses while retaining
the original HTTP ``Host`` header and TLS SNI hostname.  Keep-alive is disabled
so a connection pinned for one hostname cannot be reused for another hostname
that happens to share the same address.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import httpcore


class LinkHealthStatus(StrEnum):
    """Stable status values written to ``data/link-health.jsonl``."""

    REACHABLE = "reachable"
    REDIRECTED = "redirected"
    CLIENT_ERROR = "client_error"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    DNS_ERROR = "dns_error"
    TLS_ERROR = "tls_error"
    BLOCKED = "blocked"


HEALTHY_STATUSES = frozenset({LinkHealthStatus.REACHABLE, LinkHealthStatus.REDIRECTED})


@dataclass(frozen=True, slots=True)
class LinkHealthResult:
    """Result of one logical check, including any in-run retries."""

    url: str
    status: LinkHealthStatus
    http_status: int | None
    final_url: str | None
    checked_at: str
    consecutive_failures: int
    attempts: int
    redirect_count: int
    error: str | None = None
    failure_confirmed: bool = False

    @property
    def healthy(self) -> bool:
        return self.status in HEALTHY_STATUSES

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable record with a stable field order."""

        return {
            "url": self.url,
            "status": self.status.value,
            "http_status": self.http_status,
            "final_url": self.final_url,
            "checked_at": self.checked_at,
            "consecutive_failures": self.consecutive_failures,
            "attempts": self.attempts,
            "redirect_count": self.redirect_count,
            "error": self.error,
            "failure_confirmed": self.failure_confirmed,
        }


class Resolver(Protocol):
    def __call__(self, hostname: str, port: int) -> Iterable[str]: ...


@dataclass(frozen=True, slots=True)
class _ParsedURL:
    logical_url: str
    hostname: str
    port: int
    explicit_port: bool
    scheme: str


@dataclass(frozen=True, slots=True)
class _RequestTarget:
    logical_url: str
    request_url: str
    host_header: str
    sni_hostname: str


@dataclass(frozen=True, slots=True)
class _Observation:
    status: LinkHealthStatus
    http_status: int | None
    final_url: str | None
    redirect_count: int
    error: str | None
    retryable: bool = False
    retry_after: float | None = None


class _BlockedTarget(ValueError):
    pass


class _DNSLookupError(OSError):
    pass


class _DeadlineStream(httpcore.NetworkStream):
    """Cap every blocking socket operation by one absolute hop deadline."""

    def __init__(
        self,
        stream: httpcore.NetworkStream,
        deadline: float | None,
        monotonic: Callable[[], float],
    ) -> None:
        self._stream = stream
        self._deadline = deadline
        self._monotonic = monotonic

    def _remaining(
        self,
        timeout: float | None,
        exception_type: type[Exception],
    ) -> float | None:
        if self._deadline is None:
            return timeout
        remaining = self._deadline - self._monotonic()
        if remaining <= 0:
            raise exception_type("total per-hop response deadline exceeded")
        return remaining if timeout is None else min(timeout, remaining)

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._stream.read(
            max_bytes,
            self._remaining(timeout, httpcore.ReadTimeout),
        )

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._stream.write(
            buffer,
            self._remaining(timeout, httpcore.WriteTimeout),
        )

    def close(self) -> None:
        self._stream.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        stream = self._stream.start_tls(
            ssl_context,
            server_hostname,
            self._remaining(timeout, httpcore.ConnectTimeout),
        )
        return _DeadlineStream(stream, self._deadline, self._monotonic)

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)


class _DeadlineBackend(httpcore.NetworkBackend):
    """Wrap HTTP Core's backend so header and body trickles share a deadline."""

    def __init__(self, monotonic: Callable[[], float]) -> None:
        self._backend = httpcore.SyncBackend()
        self._monotonic = monotonic
        self._local = threading.local()

    def set_deadline(self, deadline: float) -> None:
        self._local.deadline = deadline

    def _deadline(self) -> float | None:
        value = getattr(self._local, "deadline", None)
        return value if isinstance(value, float) else None

    def _connect_timeout(self, timeout: float | None) -> float | None:
        deadline = self._deadline()
        if deadline is None:
            return timeout
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise httpcore.ConnectTimeout("total per-hop response deadline exceeded")
        return remaining if timeout is None else min(timeout, remaining)

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        deadline = self._deadline()
        stream = self._backend.connect_tcp(
            host,
            port,
            self._connect_timeout(timeout),
            local_address,
            socket_options,
        )
        return _DeadlineStream(stream, deadline, self._monotonic)

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        deadline = self._deadline()
        stream = self._backend.connect_unix_socket(
            path,
            self._connect_timeout(timeout),
            socket_options,
        )
        return _DeadlineStream(stream, deadline, self._monotonic)

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_RETRYABLE_SERVER_STATUSES = frozenset({500, 502, 503, 504})
_CHALLENGE_MARKERS = (
    b"challenge-platform",
    b"cf-chl-",
    b"just a moment",
    b"checking your browser",
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _isoformat_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _default_resolver(hostname: str, port: int) -> tuple[str, ...]:
    try:
        answers = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise _DNSLookupError(f"DNS lookup failed: {exc}") from exc

    addresses: list[str] = []
    for answer in answers:
        address = answer[4][0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise _DNSLookupError("DNS lookup returned no addresses")
    return tuple(addresses)


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _format_host(hostname: str) -> str:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return hostname
    return f"[{address.compressed}]" if address.version == 6 else address.compressed


def _parse_http_url(url: str) -> _ParsedURL:
    candidate = url.strip()
    if not candidate or _has_control_characters(candidate):
        raise _BlockedTarget("URL is empty or contains control characters")

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise _BlockedTarget(f"invalid URL: {exc}") from exc

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise _BlockedTarget("only http:// and https:// URLs are allowed")
    if not parts.netloc or parts.hostname is None:
        raise _BlockedTarget("URL has no hostname")
    if "\\" in parts.netloc:
        raise _BlockedTarget("backslashes are not allowed in URL authorities")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise _BlockedTarget("credentials are not allowed in URLs")

    hostname = parts.hostname
    if "%" in hostname:
        # IPv6 zone identifiers can select a local interface and are never
        # meaningful for an Internet blog link.
        raise _BlockedTarget("IPv6 zone identifiers are not allowed")
    try:
        hostname = hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise _BlockedTarget("hostname cannot be encoded as IDNA") from exc
    if not hostname:
        raise _BlockedTarget("URL has no hostname")

    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise _BlockedTarget(f"invalid port: {exc}") from exc
    if not 1 <= port <= 65535:
        raise _BlockedTarget("port is outside the valid range")

    default_port = 443 if scheme == "https" else 80
    explicit_port = port != default_port
    authority = _format_host(hostname)
    if explicit_port:
        authority = f"{authority}:{port}"
    path = parts.path or "/"
    normalized = urlunsplit((scheme, authority, path, parts.query, ""))
    return _ParsedURL(normalized, hostname, port, explicit_port, scheme)


def _address_is_forbidden(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        if _address_is_forbidden(address.ipv4_mapped):
            return True
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        or not address.is_global
    )


def _coerce_status(value: object) -> LinkHealthStatus | None:
    if isinstance(value, LinkHealthStatus):
        return value
    if isinstance(value, str):
        try:
            return LinkHealthStatus(value)
        except ValueError:
            return None
    return None


class LinkChecker:
    """Check external links without allowing them to reach private networks.

    Parameters are intentionally explicit so tests and callers can use a
    mocked transport and resolver.  ``max_retries`` counts retries after the
    first attempt.  Failures become ``failure_confirmed`` only after
    ``failure_confirmation_count`` consecutive scheduled checks; an in-run
    retry does not inflate that counter.
    """

    def __init__(
        self,
        *,
        timeout: float = 12.0,
        connect_timeout: float = 6.0,
        max_redirects: int = 5,
        max_body_bytes: int = 32 * 1024,
        max_response_seconds: float = 30.0,
        max_retries: int = 1,
        retry_backoff: float = 0.5,
        max_retry_after: float = 10.0,
        failure_confirmation_count: int = 2,
        user_agent: str = "icourse-blog-index/1.0 (external-link health check)",
        resolver: Resolver | None = None,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout <= 0 or connect_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        if max_response_seconds <= 0:
            raise ValueError("max_response_seconds must be positive")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if retry_backoff < 0 or max_retry_after < 0:
            raise ValueError("retry delays cannot be negative")
        if failure_confirmation_count < 1:
            raise ValueError("failure_confirmation_count must be at least one")

        self.max_redirects = max_redirects
        self.max_body_bytes = max_body_bytes
        self.max_response_seconds = max_response_seconds
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.max_retry_after = max_retry_after
        self.failure_confirmation_count = failure_confirmation_count
        self._resolver = resolver or _default_resolver
        self._sleep = sleeper
        self._clock = clock
        self._monotonic = monotonic
        self._deadline_backend: _DeadlineBackend | None = None
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache, max-age=0",
            "Pragma": "no-cache",
            "Range": f"bytes=0-{max_body_bytes - 1}",
            "Connection": "close",
        }
        limits = httpx.Limits(max_connections=4, max_keepalive_connections=0)
        if transport is None:
            transport = httpx.HTTPTransport(
                trust_env=False,
                limits=limits,
                http2=False,
            )
            self._deadline_backend = _DeadlineBackend(monotonic)
            # HTTPX does not yet expose HTTP Core's public ``network_backend``
            # constructor argument.  Replacing this one pool component lets us
            # enforce an absolute socket deadline without changing HTTPX's TLS,
            # certificate, or exception handling.  The supported dependency
            # range is tested against this integration point.
            transport._pool._network_backend = self._deadline_backend  # type: ignore[attr-defined]
        self._client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            follow_redirects=False,
            trust_env=False,
            limits=limits,
            http2=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LinkChecker:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def check(
        self,
        url: str,
        previous: LinkHealthResult | Mapping[str, object] | None = None,
    ) -> LinkHealthResult:
        """Check ``url`` and merge its failure count with a prior record."""

        started_at = self._clock()
        observation: _Observation | None = None
        attempts = 0

        for attempt_index in range(self.max_retries + 1):
            attempts += 1
            observation = self._check_once(url, address_offset=attempt_index)
            if not observation.retryable or attempt_index >= self.max_retries:
                break

            delay = observation.retry_after
            if delay is not None and delay > self.max_retry_after:
                break
            if delay is None:
                delay = self.retry_backoff * (2**attempt_index)
            if delay > 0:
                self._sleep(delay)

        assert observation is not None
        result = LinkHealthResult(
            url=url.strip(),
            status=observation.status,
            http_status=observation.http_status,
            final_url=observation.final_url,
            checked_at=_isoformat_z(started_at),
            consecutive_failures=0,
            attempts=attempts,
            redirect_count=observation.redirect_count,
            error=observation.error,
        )
        return self._merge_confirmation(result, previous)

    def _check_once(self, url: str, *, address_offset: int) -> _Observation:
        try:
            parsed = _parse_http_url(url)
        except _BlockedTarget as exc:
            return _Observation(
                LinkHealthStatus.BLOCKED,
                None,
                None,
                0,
                str(exc),
            )

        current_url = parsed.logical_url
        seen = {current_url}
        redirect_count = 0

        while True:
            try:
                target = self._make_request_target(current_url, address_offset)
                response_data = self._request(target)
            except _BlockedTarget as exc:
                return _Observation(
                    LinkHealthStatus.BLOCKED,
                    None,
                    current_url,
                    redirect_count,
                    str(exc),
                )
            except _DNSLookupError as exc:
                return _Observation(
                    LinkHealthStatus.DNS_ERROR,
                    None,
                    current_url,
                    redirect_count,
                    str(exc),
                    retryable=True,
                )
            except httpx.TimeoutException as exc:
                return _Observation(
                    LinkHealthStatus.TIMEOUT,
                    None,
                    current_url,
                    redirect_count,
                    _short_error(exc),
                    retryable=True,
                )
            except httpx.InvalidURL as exc:
                return _Observation(
                    LinkHealthStatus.BLOCKED,
                    None,
                    current_url,
                    redirect_count,
                    _short_error(exc),
                )
            except httpx.TransportError as exc:
                status = _transport_error_status(exc)
                return _Observation(
                    status,
                    None,
                    current_url,
                    redirect_count,
                    _short_error(exc),
                    retryable=status
                    in {
                        LinkHealthStatus.DNS_ERROR,
                        LinkHealthStatus.TIMEOUT,
                        LinkHealthStatus.CLIENT_ERROR,
                    },
                )

            status_code, headers, body_prefix = response_data
            if status_code in _REDIRECT_STATUSES:
                location = headers.get("location")
                if not location:
                    return _Observation(
                        LinkHealthStatus.CLIENT_ERROR,
                        status_code,
                        current_url,
                        redirect_count,
                        "redirect response has no Location header",
                    )
                if redirect_count >= self.max_redirects:
                    return _Observation(
                        LinkHealthStatus.CLIENT_ERROR,
                        status_code,
                        current_url,
                        redirect_count,
                        "redirect limit exceeded",
                    )

                candidate = urljoin(current_url, location)
                try:
                    next_url = _parse_http_url(candidate).logical_url
                except _BlockedTarget as exc:
                    return _Observation(
                        LinkHealthStatus.BLOCKED,
                        status_code,
                        current_url,
                        redirect_count,
                        f"unsafe redirect target: {exc}",
                    )
                if next_url in seen:
                    return _Observation(
                        LinkHealthStatus.CLIENT_ERROR,
                        status_code,
                        current_url,
                        redirect_count,
                        "redirect loop detected",
                    )
                seen.add(next_url)
                current_url = next_url
                redirect_count += 1
                continue

            return self._classify_response(
                status_code,
                headers,
                body_prefix,
                current_url,
                redirect_count,
            )

    def _make_request_target(self, logical_url: str, address_offset: int) -> _RequestTarget:
        parsed = _parse_http_url(logical_url)
        addresses = self._public_addresses(parsed.hostname, parsed.port)
        selected = addresses[address_offset % len(addresses)]
        selected_host = _format_host(selected)
        request_authority = selected_host
        if parsed.explicit_port:
            request_authority = f"{request_authority}:{parsed.port}"

        logical_parts = urlsplit(parsed.logical_url)
        request_url = urlunsplit(
            (
                parsed.scheme,
                request_authority,
                logical_parts.path,
                logical_parts.query,
                "",
            )
        )
        host_header = _format_host(parsed.hostname)
        if parsed.explicit_port:
            host_header = f"{host_header}:{parsed.port}"
        return _RequestTarget(
            parsed.logical_url,
            request_url,
            host_header,
            parsed.hostname,
        )

    def _public_addresses(self, hostname: str, port: int) -> tuple[str, ...]:
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            literal = None

        if literal is not None:
            candidates: Iterable[object] = (literal.compressed,)
        else:
            try:
                candidates = self._resolver(hostname, port)
            except _DNSLookupError:
                raise
            except (OSError, socket.gaierror) as exc:
                raise _DNSLookupError(f"DNS lookup failed: {exc}") from exc

        addresses: list[str] = []
        try:
            for candidate in candidates:
                address = ipaddress.ip_address(str(candidate))
                if _address_is_forbidden(address):
                    raise _BlockedTarget(
                        f"hostname resolves to non-public address {address.compressed}"
                    )
                if address.compressed not in addresses:
                    addresses.append(address.compressed)
        except _BlockedTarget:
            raise
        except ValueError as exc:
            raise _DNSLookupError("DNS lookup returned an invalid IP address") from exc

        if not addresses:
            raise _DNSLookupError("DNS lookup returned no addresses")
        return tuple(addresses)

    def _request(self, target: _RequestTarget) -> tuple[int, httpx.Headers, bytes]:
        # Clear both before and after each hop: Set-Cookie headers must never be
        # replayed, including to another host in a redirect chain.
        self._client.cookies.clear()
        headers = dict(self._headers)
        headers["Host"] = target.host_header
        extensions: dict[str, object] = {}
        if target.request_url.startswith("https://"):
            extensions["sni_hostname"] = target.sni_hostname

        request = self._client.build_request(
            "GET",
            target.request_url,
            headers=headers,
            extensions=extensions,
        )
        hop_started = self._monotonic()
        if self._deadline_backend is not None:
            self._deadline_backend.set_deadline(hop_started + self.max_response_seconds)
        response = self._client.send(request, stream=True)
        try:
            self._raise_if_response_deadline_exceeded(hop_started, request)
            prefix = bytearray()
            if response.is_stream_consumed:
                # MockTransport and some custom transports may return an
                # already-buffered response even when ``stream=True``.  The
                # built-in network transport takes the streaming branch.
                prefix.extend(response.content[: self.max_body_bytes])
            else:
                # ``chunk_size=None`` exposes transport chunks as soon as they
                # arrive.  Asking HTTPX to coalesce 8 KiB here would let a peer
                # drip bytes forever without returning control to our explicit
                # wall-clock deadline check.
                for chunk in response.iter_raw(chunk_size=None):
                    self._raise_if_response_deadline_exceeded(hop_started, request)
                    remaining = self.max_body_bytes - len(prefix)
                    if remaining <= 0:
                        break
                    prefix.extend(chunk[:remaining])
                    if len(prefix) >= self.max_body_bytes:
                        break
            return response.status_code, response.headers, bytes(prefix)
        finally:
            response.close()
            self._client.cookies.clear()

    def _raise_if_response_deadline_exceeded(
        self,
        hop_started: float,
        request: httpx.Request,
    ) -> None:
        if self._monotonic() - hop_started >= self.max_response_seconds:
            raise httpx.ReadTimeout(
                "response exceeded the total per-hop time limit",
                request=request,
            )

    def _classify_response(
        self,
        status_code: int,
        headers: httpx.Headers,
        body_prefix: bytes,
        current_url: str,
        redirect_count: int,
    ) -> _Observation:
        lower_prefix = body_prefix.lower()
        challenge = headers.get("cf-mitigated", "").lower() == "challenge" or any(
            marker in lower_prefix for marker in _CHALLENGE_MARKERS
        )
        if status_code in {403, 429} or challenge:
            retry_after = _parse_retry_after(headers.get("retry-after"), self._clock())
            return _Observation(
                LinkHealthStatus.BLOCKED,
                status_code,
                current_url,
                redirect_count,
                "remote site blocked or challenged the automated request",
                retryable=status_code == 429 and retry_after is not None,
                retry_after=retry_after,
            )

        if 200 <= status_code < 300:
            status = LinkHealthStatus.REDIRECTED if redirect_count else LinkHealthStatus.REACHABLE
            return _Observation(status, status_code, current_url, redirect_count, None)
        if 300 <= status_code < 400:
            return _Observation(
                LinkHealthStatus.CLIENT_ERROR,
                status_code,
                current_url,
                redirect_count,
                "unhandled 3xx response",
            )
        if 400 <= status_code < 500:
            return _Observation(
                LinkHealthStatus.CLIENT_ERROR,
                status_code,
                current_url,
                redirect_count,
                f"HTTP {status_code}",
            )
        if 500 <= status_code < 600:
            return _Observation(
                LinkHealthStatus.SERVER_ERROR,
                status_code,
                current_url,
                redirect_count,
                f"HTTP {status_code}",
                retryable=status_code in _RETRYABLE_SERVER_STATUSES,
            )
        return _Observation(
            LinkHealthStatus.CLIENT_ERROR,
            status_code,
            current_url,
            redirect_count,
            f"unexpected HTTP status {status_code}",
        )

    def _merge_confirmation(
        self,
        result: LinkHealthResult,
        previous: LinkHealthResult | Mapping[str, object] | None,
    ) -> LinkHealthResult:
        if result.healthy:
            return replace(result, consecutive_failures=0, failure_confirmed=False)

        prior_url: object | None = None
        prior_status: LinkHealthStatus | None = None
        prior_failures = 0
        if isinstance(previous, LinkHealthResult):
            prior_url = previous.url
            prior_status = previous.status
            prior_failures = previous.consecutive_failures
        elif isinstance(previous, Mapping):
            prior_url = previous.get("url")
            prior_status = _coerce_status(previous.get("status"))
            raw_failures = previous.get("consecutive_failures", 0)
            if isinstance(raw_failures, int) and raw_failures >= 0:
                prior_failures = raw_failures

        if prior_url == result.url and prior_status not in HEALTHY_STATUSES:
            consecutive = prior_failures + 1
        else:
            consecutive = 1
        return replace(
            result,
            consecutive_failures=consecutive,
            failure_confirmed=consecutive >= self.failure_confirmation_count,
        )


def _parse_retry_after(value: str | None, now: datetime) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return max(0.0, (parsed - now).total_seconds())


def _exception_chain(exception: BaseException) -> Iterable[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exception
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _transport_error_status(exception: httpx.TransportError) -> LinkHealthStatus:
    chain = tuple(_exception_chain(exception))
    if any(isinstance(item, (ssl.SSLError, ssl.CertificateError)) for item in chain):
        return LinkHealthStatus.TLS_ERROR
    if any(isinstance(item, socket.gaierror) for item in chain):
        return LinkHealthStatus.DNS_ERROR
    message = " ".join(str(item).lower() for item in chain)
    if "certificate" in message or "tls" in message or "ssl" in message:
        return LinkHealthStatus.TLS_ERROR
    if "name or service not known" in message or "nodename nor servname" in message:
        return LinkHealthStatus.DNS_ERROR
    return LinkHealthStatus.CLIENT_ERROR


def _short_error(exception: BaseException) -> str:
    message = " ".join(str(exception).split()) or exception.__class__.__name__
    return message[:240]


__all__ = ["HEALTHY_STATUSES", "LinkChecker", "LinkHealthResult", "LinkHealthStatus"]
