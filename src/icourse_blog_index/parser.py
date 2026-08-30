"""Parse the small, public profile summary exposed by ``icourse.club``.

Only the labelled ``博客`` field is read.  In particular, URLs in a user's
biography, reviews, navigation, or scripts are deliberately ignored.
"""

from __future__ import annotations

import hashlib
import html as html_module
import ipaddress
import json
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from urllib.parse import SplitResult, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from .utils import semantic_profile_fingerprint

PARSER_VERSION = "1.0.0"

# Visible text is NFKC-normalised before comparison, including punctuation.
_MISSING_MARKER = "用户不存在!"
_HIDDEN_MARKER = "此用户的个人主页未公开!"
_BLOG_LABEL_RE = re.compile(r"^\s*博客\s*[：:]\s*")
_PLAIN_URL_RE = re.compile(r"^(?:https?://)[^\s]+$", re.IGNORECASE)
_PERCENT_ENCODED_CONTROL_RE = re.compile(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", re.IGNORECASE)
_DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home", ".lan")
_CHALLENGE_MARKERS = (
    "cf-chl-",
    "challenge-platform",
)


class UnsafeBlogURL(ValueError):
    """Raised when a profile's blog value is not a safe public HTTP(S) URL."""


@dataclass(frozen=True, slots=True)
class ParsedProfile:
    """A parser result independent of the repository's persistence layer."""

    user_id: int
    observed_at: str
    profile_status: str
    blog_status: str
    blog_url_raw: str | None
    blog_url: str | None
    check_result: str
    http_status: int | None
    parser_version: str
    source_fingerprint: str
    error: str | None = None

    def with_fetch_metadata(
        self,
        *,
        observed_at: str | None = None,
        http_status: int | None = None,
        suspected_stale: bool = False,
    ) -> ParsedProfile:
        """Attach transport metadata without changing parsed profile values."""

        return replace(
            self,
            observed_at=observed_at or self.observed_at,
            http_status=http_status,
            check_result=(
                "suspected_stale"
                if suspected_stale and self.check_result == "ok"
                else self.check_result
            ),
        )

    def to_observation(self):
        """Convert to :class:`models.Observation` without a module-level cycle."""

        from .models import Observation

        # Malformed fields are useful while classifying the in-memory parse,
        # but failed observations must not carry arbitrary user-authored text
        # into diagnostics or trip persistence bounds.  The concise parser
        # error and structural fingerprint are sufficient for retry/audit.
        confirmed_value = self.check_result in {"ok", "suspected_stale"}

        return Observation(
            user_id=self.user_id,
            observed_at=self.observed_at,
            profile_status=self.profile_status,
            blog_status=self.blog_status,
            blog_url_raw=self.blog_url_raw if confirmed_value else None,
            blog_url=self.blog_url if confirmed_value else None,
            check_result=self.check_result,
            http_status=self.http_status,
            parser_version=self.parser_version,
            source_fingerprint=self.source_fingerprint,
            error=self.error,
        )


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_user_id(user_id: int) -> None:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ValueError("user_id must be a positive integer")


def _semantic_fingerprint(**values: object) -> str:
    encoded = json.dumps(
        {"parser_version": PARSER_VERSION, **values},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalise_visible_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _has_exact_visible_marker(soup: BeautifulSoup, marker: str) -> bool:
    return any(_normalise_visible_text(text) == marker for text in soup.stripped_strings)


def _normalise_host(hostname: str) -> str:
    hostname = hostname.rstrip(".").lower()
    if not hostname:
        raise UnsafeBlogURL("URL has no hostname")

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None

    if address is not None:
        if not address.is_global:
            raise UnsafeBlogURL("URL points to a non-public IP address")
        return address.compressed

    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise UnsafeBlogURL("URL hostname is not valid IDNA") from exc

    if (
        ascii_hostname == "localhost"
        or ascii_hostname.endswith(_BLOCKED_HOST_SUFFIXES)
        or "." not in ascii_hostname
    ):
        raise UnsafeBlogURL("URL hostname is not a public Internet name")

    labels = ascii_hostname.split(".")
    if len(ascii_hostname) > 253 or any(not _DOMAIN_LABEL_RE.fullmatch(label) for label in labels):
        raise UnsafeBlogURL("URL hostname is malformed")
    if labels[-1].isdigit():
        raise UnsafeBlogURL("URL top-level domain is malformed")
    return ascii_hostname


def normalize_blog_url(value: str) -> str:
    """Return a conservative canonical HTTP(S) URL suitable for publication.

    The function does not resolve DNS or make a request.  Link-health checks
    must repeat the private-address check after DNS resolution to prevent DNS
    rebinding.
    """

    if not isinstance(value, str):
        raise UnsafeBlogURL("URL must be text")

    candidate = unicodedata.normalize("NFKC", html_module.unescape(value)).strip()
    if not candidate or len(candidate) > 2048:
        raise UnsafeBlogURL("URL is empty or too long")
    if "\\" in candidate:
        raise UnsafeBlogURL("backslashes are not allowed in URLs")
    if any(
        character.isspace() or unicodedata.category(character).startswith("C")
        for character in candidate
    ):
        raise UnsafeBlogURL("whitespace or control characters are not allowed in URLs")
    if _PERCENT_ENCODED_CONTROL_RE.search(candidate):
        raise UnsafeBlogURL("percent-encoded control characters are not allowed in URLs")

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise UnsafeBlogURL("URL cannot be parsed") from exc

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeBlogURL("only HTTP and HTTPS blog URLs are accepted")
    if not parts.netloc or parts.username is not None or parts.password is not None:
        raise UnsafeBlogURL("URL must have a host and no embedded credentials")

    hostname = parts.hostname
    if hostname is None:
        raise UnsafeBlogURL("URL has no hostname")
    hostname = _normalise_host(hostname)

    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeBlogURL("URL port is invalid") from exc

    if port == 0:
        raise UnsafeBlogURL("URL port must be between 1 and 65535")

    if port in {80 if scheme == "http" else 443}:
        port = None
    if ":" in hostname:
        hostname = f"[{hostname}]"
    netloc = hostname if port is None else f"{hostname}:{port}"

    # A root slash and an empty path designate the same homepage.  Choosing
    # the slash spelling matches the canonical repository data model.
    path = parts.path or "/"
    normalised = SplitResult(scheme, netloc, path, parts.query, parts.fragment)
    return urlunsplit(normalised)


def _raw_value_from_anchor(anchor: Tag) -> str:
    bdi = anchor.find("bdi")
    if bdi is not None:
        value = bdi.get_text(" ", strip=True)
        if value:
            return value
    return anchor.get_text(" ", strip=True)


def _normalise_displayed_value(raw_value: str, href: str) -> str:
    """Validate displayed text against the authoritative anchor target."""

    href_parts = urlsplit(href)
    displayed = raw_value.strip()
    if not urlsplit(displayed).scheme and href_parts.scheme in {"http", "https"}:
        displayed = f"{href_parts.scheme}://{displayed}"
    return normalize_blog_url(displayed)


def _parse_blog_container(container: Tag) -> tuple[str, str | None, str | None, str | None]:
    """Return ``(status, raw, normalised, error)`` for one labelled element."""

    full_text = _normalise_visible_text(container.get_text(" ", strip=True))
    match = _BLOG_LABEL_RE.match(full_text)
    if match is None:
        return "not_a_blog_field", None, None, None

    anchors = container.find_all("a", href=True)
    if anchors:
        # The official template has exactly one anchor inside the blog <li>.
        # More than one makes the page ambiguous, so do not guess.
        if len(anchors) != 1:
            return "unknown", None, None, "blog field contains multiple links"
        anchor = anchors[0]
        href = str(anchor.get("href", "")).strip()
        raw_value = _raw_value_from_anchor(anchor)
        if not raw_value:
            raw_value = href
        try:
            normalised_href = normalize_blog_url(href)
            normalised_display = _normalise_displayed_value(raw_value, normalised_href)
        except (UnsafeBlogURL, ValueError) as exc:
            return "unknown", raw_value or None, None, f"unsafe or malformed blog URL: {exc}"
        if normalised_display != normalised_href:
            return "unknown", raw_value, None, "blog label and link target disagree"
        return "present", raw_value, normalised_href, None

    remainder = full_text[match.end() :].strip()
    if remainder == "暂无":
        return "absent", None, None, None

    # This fallback remains label-specific and accommodates a future template
    # that prints an absolute URL without an anchor.
    if _PLAIN_URL_RE.fullmatch(remainder):
        try:
            return "present", remainder, normalize_blog_url(remainder), None
        except UnsafeBlogURL as exc:
            return "unknown", remainder, None, f"unsafe or malformed blog URL: {exc}"

    return "unknown", remainder or None, None, "blog field has an unrecognised structure"


def _candidate_blog_containers(soup: BeautifulSoup) -> list[Tag]:
    candidates: list[Tag] = []
    seen: set[int] = set()

    # Official markup uses an <li>.  The small fallback set handles harmless
    # semantic template changes while still requiring the exact field label.
    for tag_name in ("li", "dd", "tr", "p"):
        for tag in soup.find_all(tag_name):
            text = _normalise_visible_text(tag.get_text(" ", strip=True))
            if _BLOG_LABEL_RE.match(text) and id(tag) not in seen:
                candidates.append(tag)
                seen.add(id(tag))
    return candidates


def parse_profile(html: str, user_id: int) -> ParsedProfile:
    """Classify one direct ``/user/{id}`` response body.

    A successful HTTP response that does not match a known site state is
    returned as ``unknown``/``parse_error``.  It is never silently treated as
    a missing user or as a public profile without a blog.
    """

    _validate_user_id(user_id)
    if not isinstance(html, str):
        raise TypeError("html must be text")

    observed_at = _utc_now()
    soup = BeautifulSoup(html, "html.parser")
    visible_text = _normalise_visible_text(soup.get_text(" ", strip=True))
    lowered_html = html.lower()

    parsed_fields = [_parse_blog_container(tag) for tag in _candidate_blog_containers(soup)]
    parsed_fields = [item for item in parsed_fields if item[0] != "not_a_blog_field"]

    # A real public profile always has the labelled blog field (including the
    # literal ``暂无``).  Checking it first prevents a phrase in user-authored
    # text from being mistaken for a site-level state marker.
    if not parsed_fields and _has_exact_visible_marker(soup, _MISSING_MARKER):
        return ParsedProfile(
            user_id=user_id,
            observed_at=observed_at,
            profile_status="missing",
            blog_status="unknown",
            blog_url_raw=None,
            blog_url=None,
            check_result="ok",
            http_status=None,
            parser_version=PARSER_VERSION,
            source_fingerprint=semantic_profile_fingerprint("missing", "unknown", None),
        )

    if not parsed_fields and _has_exact_visible_marker(soup, _HIDDEN_MARKER):
        return ParsedProfile(
            user_id=user_id,
            observed_at=observed_at,
            profile_status="hidden",
            blog_status="unknown",
            blog_url_raw=None,
            blog_url=None,
            check_result="ok",
            http_status=None,
            parser_version=PARSER_VERSION,
            source_fingerprint=semantic_profile_fingerprint("hidden", "unknown", None),
        )

    title = (
        _normalise_visible_text(soup.title.get_text(" ", strip=True)).lower() if soup.title else ""
    )
    looks_like_challenge = (
        any(marker in lowered_html for marker in _CHALLENGE_MARKERS)
        or title in {"just a moment...", "checking your browser"}
        or (
            "verify you are human" in visible_text.lower()
            and any(marker in lowered_html for marker in ("turnstile", "captcha", "challenge"))
        )
    )
    if looks_like_challenge:
        error = "received a bot-challenge page instead of a user profile"
        return ParsedProfile(
            user_id=user_id,
            observed_at=observed_at,
            profile_status="unknown",
            blog_status="unknown",
            blog_url_raw=None,
            blog_url=None,
            check_result="parse_error",
            http_status=None,
            parser_version=PARSER_VERSION,
            source_fingerprint=_semantic_fingerprint(profile_status="unknown", reason="challenge"),
            error=error,
        )

    if not parsed_fields:
        error = "known profile-state markers and the labelled blog field were not found"
        # Store only a hash of a short normalised structural hint, never HTML.
        hint = visible_text[:512]
        return ParsedProfile(
            user_id=user_id,
            observed_at=observed_at,
            profile_status="unknown",
            blog_status="unknown",
            blog_url_raw=None,
            blog_url=None,
            check_result="parse_error",
            http_status=None,
            parser_version=PARSER_VERSION,
            source_fingerprint=_semantic_fingerprint(profile_status="unknown", hint=hint),
            error=error,
        )

    unique_fields = list(dict.fromkeys(parsed_fields))
    if len(unique_fields) != 1:
        error = "multiple conflicting labelled blog fields were found"
        return ParsedProfile(
            user_id=user_id,
            observed_at=observed_at,
            profile_status="unknown",
            blog_status="unknown",
            blog_url_raw=None,
            blog_url=None,
            check_result="parse_error",
            http_status=None,
            parser_version=PARSER_VERSION,
            source_fingerprint=_semantic_fingerprint(
                profile_status="unknown", reason="conflicting_fields"
            ),
            error=error,
        )

    blog_status, raw_url, normalised_url, error = unique_fields[0]
    profile_status = "public" if blog_status in {"present", "absent"} else "unknown"
    check_result = "ok" if profile_status == "public" else "parse_error"
    return ParsedProfile(
        user_id=user_id,
        observed_at=observed_at,
        profile_status=profile_status,
        blog_status=blog_status,
        blog_url_raw=raw_url,
        blog_url=normalised_url,
        check_result=check_result,
        http_status=None,
        parser_version=PARSER_VERSION,
        source_fingerprint=(
            semantic_profile_fingerprint(profile_status, blog_status, normalised_url)
            if check_result == "ok"
            else _semantic_fingerprint(
                profile_status=profile_status,
                blog_status=blog_status,
                blog_url=normalised_url,
                error=error,
            )
        ),
        error=error,
    )


def observation_from_fetch(fetch_result):
    """Convert a fetch result into the canonical persistence observation."""

    if fetch_result.ok and fetch_result.body is not None:
        parsed = parse_profile(fetch_result.body, fetch_result.user_id).with_fetch_metadata(
            observed_at=fetch_result.fetched_at,
            http_status=fetch_result.http_status,
            suspected_stale=fetch_result.suspected_stale,
        )
        return parsed.to_observation()

    outcome_to_check_result = {
        "rate_limited": "rate_limited",
        "blocked_or_challenge": "blocked_or_challenge",
        "robots_disallowed": "blocked_or_challenge",
        "robots_unavailable": "network_error",
        "server_error": "server_error",
        "timeout": "timeout",
        "network_error": "network_error",
    }
    outcome = getattr(fetch_result.outcome, "value", str(fetch_result.outcome))
    check_result = outcome_to_check_result.get(outcome, "http_error")
    parsed = ParsedProfile(
        user_id=fetch_result.user_id,
        observed_at=fetch_result.fetched_at,
        profile_status="unknown",
        blog_status="unknown",
        blog_url_raw=None,
        blog_url=None,
        check_result=check_result,
        http_status=fetch_result.http_status,
        parser_version=PARSER_VERSION,
        source_fingerprint=_semantic_fingerprint(profile_status="unknown", fetch_outcome=outcome),
        error=fetch_result.error or f"profile fetch failed: {outcome}",
    )
    return parsed.to_observation()


__all__ = [
    "PARSER_VERSION",
    "ParsedProfile",
    "UnsafeBlogURL",
    "normalize_blog_url",
    "observation_from_fetch",
    "parse_profile",
]
