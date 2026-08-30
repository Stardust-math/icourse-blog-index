"""Polite, fail-closed HTTP access to public iCourse profile pages."""

from __future__ import annotations

import os
import random
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from urllib.parse import urlencode, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

BASE_URL = "https://icourse.club"
ROBOTS_URL = f"{BASE_URL}/robots.txt"
USER_AGENT_PRODUCT = "icourse-blog-index"
USER_AGENT_VERSION = "1.0"

_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
_CHALLENGE_TECHNICAL_MARKERS = (
    b"cf-chl-",
    b"challenge-platform",
)
_CHALLENGE_TITLE_MARKERS = (
    b"<title>just a moment...</title>",
    b"<title>checking your browser</title>",
)
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_SELECTED_RESPONSE_HEADERS = (
    "age",
    "cache-control",
    "cf-cache-status",
    "content-type",
    "date",
    "etag",
    "expires",
    "last-modified",
    "retry-after",
    "via",
    "warning",
    "x-cache",
)


class FetchOutcome(StrEnum):
    OK = "ok"
    HTTP_ERROR = "http_error"
    RATE_LIMITED = "rate_limited"
    BLOCKED_OR_CHALLENGE = "blocked_or_challenge"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    ROBOTS_DISALLOWED = "robots_disallowed"
    ROBOTS_UNAVAILABLE = "robots_unavailable"
    RESPONSE_TOO_LARGE = "response_too_large"
    REDIRECT_REJECTED = "redirect_rejected"
    INVALID_CONTENT_TYPE = "invalid_content_type"


@dataclass(frozen=True, slots=True)
class FetcherConfig:
    """Network policy for a single-threaded crawler process."""

    repository_url: str | None = None
    min_delay_seconds: float = 2.5
    max_delay_seconds: float = 3.5
    timeout_seconds: float = 25.0
    max_attempts: int = 3
    backoff_base_seconds: float = 3.0
    backoff_cap_seconds: float = 60.0
    max_retry_after_seconds: float = 120.0
    max_response_bytes: int = 2_000_000
    robots_cache_seconds: float = 3_600.0

    def __post_init__(self) -> None:
        if self.min_delay_seconds < 0 or self.max_delay_seconds < self.min_delay_seconds:
            raise ValueError("request delay range is invalid")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_attempts < 1 or self.max_attempts > 5:
            raise ValueError("max_attempts must be between 1 and 5")
        if self.backoff_base_seconds < 0 or self.backoff_cap_seconds < 0:
            raise ValueError("backoff values cannot be negative")
        if self.max_retry_after_seconds < 0:
            raise ValueError("max_retry_after_seconds cannot be negative")
        if self.max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        if self.robots_cache_seconds < 0:
            raise ValueError("robots_cache_seconds cannot be negative")


@dataclass(frozen=True, slots=True)
class CacheMetadata:
    """Selected non-cookie headers useful when auditing stale responses."""

    cf_cache_status: str | None = None
    age_seconds: int | None = None
    cache_control: str | None = None
    date: str | None = None
    expires: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    via: str | None = None
    warning: str | None = None
    x_cache: str | None = None


@dataclass(frozen=True, slots=True)
class FetchResult:
    user_id: int
    requested_url: str
    final_url: str | None
    fetched_at: str
    outcome: FetchOutcome
    http_status: int | None
    body: str | None
    attempts: int
    elapsed_seconds: float
    cache_metadata: CacheMetadata = field(default_factory=CacheMetadata)
    suspected_stale: bool = False
    stale_reasons: tuple[str, ...] = ()
    retry_after_seconds: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is FetchOutcome.OK and self.body is not None

    @property
    def status_code(self) -> int | None:
        """Compatibility alias for HTTP client conventions."""

        return self.http_status

    @property
    def cache_status(self) -> str | None:
        return self.cache_metadata.cf_cache_status

    @property
    def age_seconds(self) -> int | None:
        return self.cache_metadata.age_seconds


@dataclass(frozen=True, slots=True)
class RobotsResult:
    allowed: bool
    checked_at: str
    status_code: int | None
    reason: str
    crawl_delay_seconds: float | None = None
    retry_after_seconds: float | None = None


class _ResponseTooLarge(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _RawResponse:
    status_code: int
    url: str
    headers: Mapping[str, str]
    content: bytes


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resolve_repository_url(explicit_url: str | None = None) -> str:
    """Resolve and validate the public repository URL used for contact.

    Resolution order is an explicit argument, ``ICOURSE_REPOSITORY_URL``, and
    finally the standard GitHub Actions ``GITHUB_SERVER_URL`` plus
    ``GITHUB_REPOSITORY`` environment.  Network-facing components share this
    helper so every User-Agent identifies the same repository.
    """

    candidate = explicit_url or os.environ.get("ICOURSE_REPOSITORY_URL")
    if not candidate:
        github_repository = os.environ.get("GITHUB_REPOSITORY")
        if github_repository:
            github_server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
            candidate = f"{github_server}/{github_repository}"
    if not candidate:
        raise ValueError(
            "a repository URL is required for the crawler User-Agent; set "
            "ICOURSE_REPOSITORY_URL (GitHub Actions supplies GITHUB_REPOSITORY automatically)"
        )

    if any(character.isspace() or ord(character) < 32 for character in candidate):
        raise ValueError("repository URL contains whitespace or control characters")
    parts = urlsplit(candidate)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("repository URL must be an absolute credential-free HTTPS URL")
    return candidate.rstrip("/")


# Compatibility for internal callers from pre-1.0 development snapshots.
_resolve_repository_url = resolve_repository_url


def _build_user_agent(repository_url: str) -> str:
    return f"{USER_AGENT_PRODUCT}/{USER_AGENT_VERSION} (+{repository_url}; public-profile indexer)"


def _parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        current = now or datetime.now(UTC)
        return max(0.0, (retry_at - current).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _is_challenge(content: bytes) -> bool:
    lowered = content[:250_000].lower()
    if any(marker in lowered for marker in _CHALLENGE_TECHNICAL_MARKERS):
        return True
    if any(marker in lowered for marker in _CHALLENGE_TITLE_MARKERS):
        return True
    return b"verify you are human" in lowered and (
        b"turnstile" in lowered or b"captcha" in lowered or b"challenge" in lowered
    )


def _selected_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {name: headers[name] for name in _SELECTED_RESPONSE_HEADERS if name in headers}


def _cache_metadata(headers: Mapping[str, str]) -> tuple[CacheMetadata, bool, tuple[str, ...]]:
    selected = _selected_headers(headers)
    age: int | None = None
    try:
        if "age" in selected:
            age = max(0, int(selected["age"]))
    except ValueError:
        age = None

    metadata = CacheMetadata(
        cf_cache_status=selected.get("cf-cache-status"),
        age_seconds=age,
        cache_control=selected.get("cache-control"),
        date=selected.get("date"),
        expires=selected.get("expires"),
        etag=selected.get("etag"),
        last_modified=selected.get("last-modified"),
        via=selected.get("via"),
        warning=selected.get("warning"),
        x_cache=selected.get("x-cache"),
    )

    reasons: list[str] = []
    cf_status = (metadata.cf_cache_status or "").upper()
    if cf_status in {"HIT", "STALE", "UPDATING"}:
        reasons.append(f"CF-Cache-Status={cf_status}")
    if age is not None and age > 0:
        reasons.append(f"Age={age}")
    x_cache = (metadata.x_cache or "").upper()
    if "HIT" in x_cache or "STALE" in x_cache:
        reasons.append(f"X-Cache={metadata.x_cache}")
    warning = metadata.warning or ""
    if re.search(r"(?:^|\s)11[01](?:\s|$)", warning):
        reasons.append(f"Warning={warning}")
    return metadata, bool(reasons), tuple(reasons)


def _decode_html(content: bytes, content_type: str | None) -> str:
    charset = "utf-8"
    if content_type:
        match = re.search(r"charset\s*=\s*[\"']?([^;\s\"']+)", content_type, re.IGNORECASE)
        if match:
            charset = match.group(1)
    try:
        return content.decode(charset, errors="strict")
    except (LookupError, UnicodeDecodeError):
        return content.decode("utf-8", errors="replace")


def _profile_url(user_id: int, *, cache_bust: bool, cache_bust_token: str | None = None) -> str:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ValueError("user_id must be a positive integer")
    url = f"{BASE_URL}/user/{user_id}"
    if cache_bust:
        token = cache_bust_token or str(int(time.time() * 1_000))
        url = f"{url}?{urlencode({'_icbi_fresh': token})}"
    return url


class ProfileFetcher:
    """Fetch profile pages serially with rate limiting and safe failure modes.

    ``ProfileFetcher`` is a context manager.  Calling :meth:`close` is only
    necessary when it created its own ``httpx.Client``.
    """

    def __init__(
        self,
        config: FetcherConfig | None = None,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.config = config or FetcherConfig()
        self.repository_url = resolve_repository_url(self.config.repository_url)
        self.user_agent = _build_user_agent(self.repository_url)
        self._sleep = sleep
        self._monotonic = monotonic
        self._random_uniform = random_uniform
        self._owns_client = client is None
        if client is not None and (
            client.headers.get("authorization")
            or client.headers.get("cookie")
            or len(client.cookies) > 0
            or getattr(client, "_auth", None) is not None
        ):
            raise ValueError("the crawler accepts only an unauthenticated, cookie-free HTTP client")
        self._client = client or httpx.Client(
            follow_redirects=False,
            timeout=self.config.timeout_seconds,
            headers={"User-Agent": self.user_agent},
            # Do not silently inherit proxy credentials or change the network
            # path through HTTP(S)_PROXY / ALL_PROXY environment variables.
            trust_env=False,
        )
        self._send_lock = threading.Lock()
        self._robots_lock = threading.Lock()
        self._last_request_started: float | None = None
        self._robots_result: RobotsResult | None = None
        self._robots_parser: RobotFileParser | None = None
        self._robots_checked_monotonic: float | None = None
        self._robots_crawl_delay = 0.0

    def __enter__(self) -> ProfileFetcher:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _request_headers(self, accept: str) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": accept,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
            "Cache-Control": "no-cache, max-age=0",
            "Pragma": "no-cache",
        }

    def _polite_wait(self) -> None:
        configured = self._random_uniform(
            self.config.min_delay_seconds,
            self.config.max_delay_seconds,
        )
        required_delay = max(configured, self._robots_crawl_delay)
        now = self._monotonic()
        if self._last_request_started is not None:
            remaining = self._last_request_started + required_delay - now
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_started = self._monotonic()

    def _get(self, url: str, *, accept: str) -> _RawResponse:
        # This lock is the final guard against accidental concurrent requests,
        # even if a future caller uses the fetcher from multiple threads.
        with self._send_lock:
            self._polite_wait()
            try:
                with self._client.stream(
                    "GET",
                    url,
                    headers=self._request_headers(accept),
                    timeout=self.config.timeout_seconds,
                    follow_redirects=False,
                ) as response:
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            if int(content_length) > self.config.max_response_bytes:
                                raise _ResponseTooLarge(
                                    "response Content-Length exceeds the configured limit"
                                )
                        except ValueError:
                            pass

                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self.config.max_response_bytes:
                            raise _ResponseTooLarge("response body exceeds the configured limit")
                        chunks.append(chunk)

                    return _RawResponse(
                        status_code=response.status_code,
                        url=str(response.url),
                        headers={key.lower(): value for key, value in response.headers.items()},
                        content=b"".join(chunks),
                    )
            finally:
                # Flask or intermediary cookies must not turn later requests
                # into a stateful or authenticated crawl.
                self._client.cookies.clear()

    def _sleep_before_retry(self, attempt: int, retry_after: float | None) -> bool:
        if retry_after is not None:
            if retry_after > self.config.max_retry_after_seconds:
                return False
            delay = retry_after
        else:
            delay = min(
                self.config.backoff_cap_seconds,
                self.config.backoff_base_seconds * (2 ** max(0, attempt - 1)),
            )
            if delay > 0:
                delay = min(
                    self.config.backoff_cap_seconds,
                    self._random_uniform(delay * 0.8, delay * 1.2),
                )
        if delay > 0:
            self._sleep(delay)
        return True

    def check_robots(self, *, force: bool = False) -> RobotsResult:
        """Check ``robots.txt`` and fail closed on temporary uncertainty."""

        with self._robots_lock:
            now = self._monotonic()
            if (
                not force
                and self._robots_result is not None
                and self._robots_checked_monotonic is not None
                and now - self._robots_checked_monotonic < self.config.robots_cache_seconds
            ):
                return self._robots_result

            last_error: str | None = None
            parsed_rules: RobotFileParser | None = None
            for attempt in range(1, self.config.max_attempts + 1):
                try:
                    response = self._get(ROBOTS_URL, accept="text/plain,*/*;q=0.1")
                except httpx.TimeoutException as exc:
                    last_error = f"robots.txt request timed out: {exc}"
                    if attempt < self.config.max_attempts:
                        self._sleep_before_retry(attempt, None)
                        continue
                    result = RobotsResult(False, _utc_now(), None, last_error)
                    break
                except httpx.HTTPError as exc:
                    last_error = f"robots.txt network error: {exc}"
                    if attempt < self.config.max_attempts:
                        self._sleep_before_retry(attempt, None)
                        continue
                    result = RobotsResult(False, _utc_now(), None, last_error)
                    break
                except _ResponseTooLarge as exc:
                    result = RobotsResult(
                        False,
                        _utc_now(),
                        None,
                        f"cannot safely parse robots.txt: {exc}",
                    )
                    break

                status = response.status_code
                retry_after = _parse_retry_after(response.headers.get("retry-after"))
                if status == 200:
                    if _is_challenge(response.content):
                        result = RobotsResult(
                            False,
                            _utc_now(),
                            status,
                            "robots.txt returned a bot-challenge page",
                        )
                        break
                    text = _decode_html(response.content, response.headers.get("content-type"))
                    parser = RobotFileParser()
                    parser.set_url(ROBOTS_URL)
                    parser.parse(text.splitlines())
                    parsed_rules = parser
                    allowed = parser.can_fetch(USER_AGENT_PRODUCT, f"{BASE_URL}/user/1")
                    crawl_delay = parser.crawl_delay(USER_AGENT_PRODUCT)
                    if crawl_delay is None:
                        crawl_delay = parser.crawl_delay("*")
                    if crawl_delay is not None:
                        self._robots_crawl_delay = max(0.0, float(crawl_delay))
                    reason = (
                        "robots.txt permits public user pages"
                        if allowed
                        else "robots.txt disallows public user pages"
                    )
                    result = RobotsResult(
                        allowed,
                        _utc_now(),
                        status,
                        reason,
                        crawl_delay_seconds=float(crawl_delay) if crawl_delay is not None else None,
                    )
                    break

                if status in {404, 410}:
                    result = RobotsResult(
                        True,
                        _utc_now(),
                        status,
                        "robots.txt is absent; no crawl rules are declared",
                    )
                    break
                if status in {401, 403}:
                    result = RobotsResult(
                        False,
                        _utc_now(),
                        status,
                        "robots.txt access is forbidden; crawling is stopped",
                    )
                    break
                if status == 429:
                    if attempt < self.config.max_attempts and self._sleep_before_retry(
                        attempt, retry_after
                    ):
                        continue
                    result = RobotsResult(
                        False,
                        _utc_now(),
                        status,
                        "robots.txt was rate-limited; crawling is stopped",
                        retry_after_seconds=retry_after,
                    )
                    break
                if status >= 500 and attempt < self.config.max_attempts:
                    if self._sleep_before_retry(attempt, retry_after):
                        continue
                result = RobotsResult(
                    False,
                    _utc_now(),
                    status,
                    f"robots.txt returned HTTP {status}; crawling is stopped",
                    retry_after_seconds=retry_after,
                )
                break

            self._robots_result = result
            # A 404/410 intentionally has no parser and means that no policy
            # was published.  Every other parser-less outcome is fail-closed.
            self._robots_parser = parsed_rules
            self._robots_checked_monotonic = self._monotonic()
            return result

    def _cached_robots_allows(self, url: str) -> bool:
        """Apply the last successfully parsed policy to one exact target URL."""

        with self._robots_lock:
            result = self._robots_result
            parser = self._robots_parser
            if result is None or not result.allowed:
                return False
            if result.status_code in {404, 410}:
                return True
            if result.status_code != 200 or parser is None:
                return False
            return parser.can_fetch(USER_AGENT_PRODUCT, url)

    def fetch_user(
        self,
        user_id: int,
        *,
        cache_bust: bool = False,
        cache_bust_token: str | None = None,
    ) -> FetchResult:
        """Fetch one direct profile page without following redirects.

        Expected HTTP and network failures are represented in ``FetchResult``
        instead of being raised.  Configuration/programming errors still raise.
        """

        url = _profile_url(user_id, cache_bust=cache_bust, cache_bust_token=cache_bust_token)
        started = self._monotonic()
        robots = self.check_robots()
        if not robots.allowed:
            robots_outcome = (
                FetchOutcome.ROBOTS_DISALLOWED
                if robots.status_code in {200, 401, 403}
                else FetchOutcome.ROBOTS_UNAVAILABLE
            )
            return FetchResult(
                user_id=user_id,
                requested_url=url,
                final_url=None,
                fetched_at=_utc_now(),
                outcome=robots_outcome,
                http_status=robots.status_code,
                body=None,
                attempts=0,
                elapsed_seconds=self._monotonic() - started,
                retry_after_seconds=robots.retry_after_seconds,
                error=robots.reason,
            )

        if not self._cached_robots_allows(url):
            return FetchResult(
                user_id=user_id,
                requested_url=url,
                final_url=None,
                fetched_at=_utc_now(),
                outcome=FetchOutcome.ROBOTS_DISALLOWED,
                http_status=robots.status_code,
                body=None,
                attempts=0,
                elapsed_seconds=self._monotonic() - started,
                error="robots.txt disallows this exact user-page URL",
            )

        last_outcome = FetchOutcome.NETWORK_ERROR
        last_status: int | None = None
        last_error: str | None = None
        last_url: str | None = None
        retry_after: float | None = None
        metadata = CacheMetadata()
        suspected_stale = False
        stale_reasons: tuple[str, ...] = ()

        for attempt in range(1, self.config.max_attempts + 1):
            try:
                response = self._get(url, accept="text/html,application/xhtml+xml;q=0.9")
            except httpx.TimeoutException as exc:
                last_outcome = FetchOutcome.TIMEOUT
                last_error = f"profile request timed out: {exc}"
                if attempt < self.config.max_attempts:
                    self._sleep_before_retry(attempt, None)
                    continue
                return FetchResult(
                    user_id,
                    url,
                    None,
                    _utc_now(),
                    last_outcome,
                    None,
                    None,
                    attempt,
                    self._monotonic() - started,
                    error=last_error,
                )
            except httpx.HTTPError as exc:
                last_outcome = FetchOutcome.NETWORK_ERROR
                last_error = f"profile network error: {exc}"
                if attempt < self.config.max_attempts:
                    self._sleep_before_retry(attempt, None)
                    continue
                return FetchResult(
                    user_id,
                    url,
                    None,
                    _utc_now(),
                    last_outcome,
                    None,
                    None,
                    attempt,
                    self._monotonic() - started,
                    error=last_error,
                )
            except _ResponseTooLarge as exc:
                return FetchResult(
                    user_id,
                    url,
                    None,
                    _utc_now(),
                    FetchOutcome.RESPONSE_TOO_LARGE,
                    None,
                    None,
                    attempt,
                    self._monotonic() - started,
                    error=str(exc),
                )

            last_status = response.status_code
            last_url = response.url
            metadata, suspected_stale, stale_reasons = _cache_metadata(response.headers)
            retry_after = _parse_retry_after(response.headers.get("retry-after"))

            # follow_redirects=False is deliberate: a redirect cannot silently
            # move the crawler to a login page, mirror, or unrelated origin.
            if 300 <= response.status_code < 400:
                return FetchResult(
                    user_id,
                    url,
                    last_url,
                    _utc_now(),
                    FetchOutcome.REDIRECT_REJECTED,
                    response.status_code,
                    None,
                    attempt,
                    self._monotonic() - started,
                    cache_metadata=metadata,
                    suspected_stale=suspected_stale,
                    stale_reasons=stale_reasons,
                    error="profile request redirected; redirect was not followed",
                )

            if response.status_code in {401, 403} or _is_challenge(response.content):
                return FetchResult(
                    user_id,
                    url,
                    last_url,
                    _utc_now(),
                    FetchOutcome.BLOCKED_OR_CHALLENGE,
                    response.status_code,
                    None,
                    attempt,
                    self._monotonic() - started,
                    cache_metadata=metadata,
                    suspected_stale=suspected_stale,
                    stale_reasons=stale_reasons,
                    error="profile request was forbidden or returned a bot challenge",
                )

            if response.status_code == 429:
                last_outcome = FetchOutcome.RATE_LIMITED
                last_error = "profile request was rate-limited"
                if attempt < self.config.max_attempts and self._sleep_before_retry(
                    attempt, retry_after
                ):
                    continue
                return FetchResult(
                    user_id,
                    url,
                    last_url,
                    _utc_now(),
                    last_outcome,
                    response.status_code,
                    None,
                    attempt,
                    self._monotonic() - started,
                    cache_metadata=metadata,
                    suspected_stale=suspected_stale,
                    stale_reasons=stale_reasons,
                    retry_after_seconds=retry_after,
                    error=last_error,
                )

            if response.status_code in {408, 425}:
                last_outcome = FetchOutcome.HTTP_ERROR
                last_error = f"profile server returned HTTP {response.status_code}"
                if attempt < self.config.max_attempts and self._sleep_before_retry(
                    attempt, retry_after
                ):
                    continue
                return FetchResult(
                    user_id,
                    url,
                    last_url,
                    _utc_now(),
                    last_outcome,
                    response.status_code,
                    None,
                    attempt,
                    self._monotonic() - started,
                    cache_metadata=metadata,
                    suspected_stale=suspected_stale,
                    stale_reasons=stale_reasons,
                    retry_after_seconds=retry_after,
                    error=last_error,
                )

            if response.status_code >= 500:
                last_outcome = FetchOutcome.SERVER_ERROR
                last_error = f"profile server returned HTTP {response.status_code}"
                if (
                    response.status_code in _RETRYABLE_STATUS_CODES
                    and attempt < self.config.max_attempts
                ):
                    if self._sleep_before_retry(attempt, retry_after):
                        continue
                return FetchResult(
                    user_id,
                    url,
                    last_url,
                    _utc_now(),
                    last_outcome,
                    response.status_code,
                    None,
                    attempt,
                    self._monotonic() - started,
                    cache_metadata=metadata,
                    suspected_stale=suspected_stale,
                    stale_reasons=stale_reasons,
                    retry_after_seconds=retry_after,
                    error=last_error,
                )

            if response.status_code != 200:
                return FetchResult(
                    user_id,
                    url,
                    last_url,
                    _utc_now(),
                    FetchOutcome.HTTP_ERROR,
                    response.status_code,
                    None,
                    attempt,
                    self._monotonic() - started,
                    cache_metadata=metadata,
                    suspected_stale=suspected_stale,
                    stale_reasons=stale_reasons,
                    error=f"profile server returned HTTP {response.status_code}",
                )

            content_type = response.headers.get("content-type")
            if content_type and not any(
                kind in content_type.lower() for kind in _HTML_CONTENT_TYPES
            ):
                return FetchResult(
                    user_id,
                    url,
                    last_url,
                    _utc_now(),
                    FetchOutcome.INVALID_CONTENT_TYPE,
                    response.status_code,
                    None,
                    attempt,
                    self._monotonic() - started,
                    cache_metadata=metadata,
                    suspected_stale=suspected_stale,
                    stale_reasons=stale_reasons,
                    error=f"profile returned unexpected Content-Type: {content_type}",
                )

            body = _decode_html(response.content, content_type)
            return FetchResult(
                user_id,
                url,
                last_url,
                _utc_now(),
                FetchOutcome.OK,
                response.status_code,
                body,
                attempt,
                self._monotonic() - started,
                cache_metadata=metadata,
                suspected_stale=suspected_stale,
                stale_reasons=stale_reasons,
            )

        # Defensive fallback; every branch above returns on the final attempt.
        return FetchResult(
            user_id,
            url,
            last_url,
            _utc_now(),
            last_outcome,
            last_status,
            None,
            self.config.max_attempts,
            self._monotonic() - started,
            cache_metadata=metadata,
            suspected_stale=suspected_stale,
            stale_reasons=stale_reasons,
            retry_after_seconds=retry_after,
            error=last_error,
        )

    def fetch_and_parse(self, user_id: int, *, cache_bust: bool = False):
        """Fetch and convert one profile directly to ``models.Observation``.

        A cache-indicating first response is never offered to the persistence
        layer.  It triggers one same-origin, same-path request with a unique
        query token; only that independent result is returned.  A caller that
        already requested ``cache_bust=True`` does not recurse.
        """

        from .parser import observation_from_fetch

        result = self.fetch_user(user_id, cache_bust=cache_bust)
        if result.ok and result.suspected_stale and not cache_bust:
            result = self.fetch_user(user_id, cache_bust=True)
        return observation_from_fetch(result)


__all__ = [
    "BASE_URL",
    "ROBOTS_URL",
    "CacheMetadata",
    "FetchOutcome",
    "FetchResult",
    "FetcherConfig",
    "ProfileFetcher",
    "RobotsResult",
    "resolve_repository_url",
]
