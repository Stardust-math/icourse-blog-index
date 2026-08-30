"""Small, dependency-free utilities shared by the crawler modules.

The data repository is intentionally plain text.  These helpers keep timestamps,
JSON and file replacement deterministic so that scheduled runs produce small,
reviewable commits.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit


UTC = timezone.utc
SHARD_SIZE = 1_000
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def utc_now() -> datetime:
    """Return the current time as an aware UTC ``datetime``."""

    return datetime.now(UTC)


def parse_utc(value: str | datetime) -> datetime:
    """Parse an ISO-8601 timestamp and normalize it to UTC.

    Serialized project timestamps must use ``Z``.  Aware ``datetime`` inputs are
    accepted to make the public API convenient, while naive values are rejected
    because silently guessing their timezone would corrupt scheduling.
    """

    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("a UTC timestamp must be timezone-aware")
        return value.astimezone(UTC)
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a non-empty ISO-8601 string")
    if not _UTC_RE.fullmatch(value):
        raise ValueError(f"timestamp must be UTC and end in 'Z': {value!r}")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"invalid UTC timestamp: {value!r}") from exc
    return parsed.astimezone(UTC)


def format_utc(value: str | datetime | None = None) -> str:
    """Return the canonical second-resolution representation of a UTC time."""

    instant = utc_now() if value is None else parse_utc(value)
    return instant.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically without escaping readable Unicode."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def pretty_json(value: Any) -> str:
    """Serialize a human-edited JSON document deterministically."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )


def json_fingerprint(value: Any) -> str:
    """Return a stable SHA-256 digest for JSON-compatible semantic data."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def atomic_write_text(path: str | Path, text: str, *, mode: int = 0o644) -> None:
    """Atomically replace *path* with UTF-8 *text*.

    The temporary file lives beside the destination, so ``os.replace`` remains
    atomic on normal filesystems.  Both the file and its containing directory are
    flushed where supported.  A failed write leaves the previous file intact.
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, destination)
        temporary_name = None
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def atomic_write_json(path: str | Path, value: Any) -> None:
    """Atomically write an indented deterministic JSON document."""

    atomic_write_text(path, pretty_json(value))


def read_json(path: str | Path, default: Any = None) -> Any:
    """Read JSON, returning *default* only when the file does not exist."""

    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def shard_start(user_id: int, *, shard_size: int = SHARD_SIZE) -> int:
    """Return the inclusive lower bound of the shard containing ``user_id``."""

    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 0:
        raise ValueError("user_id must be a non-negative integer")
    if isinstance(shard_size, bool) or not isinstance(shard_size, int) or shard_size <= 0:
        raise ValueError("shard_size must be a positive integer")
    return (user_id // shard_size) * shard_size


def shard_filename(user_id: int, *, shard_size: int = SHARD_SIZE) -> str:
    """Return a sortable filename such as ``01000-01999.jsonl``."""

    start = shard_start(user_id, shard_size=shard_size)
    end = start + shard_size - 1
    width = max(5, len(str(end)))
    return f"{start:0{width}d}-{end:0{width}d}.jsonl"


def sanitize_error(value: str | None, *, limit: int = 500) -> str | None:
    """Make an exception message safe and compact for the public dataset."""

    if value is None:
        return None
    compact = " ".join(str(value).split())
    if not compact:
        return None
    return compact[:limit]


def normalize_http_url(value: str) -> str:
    """Apply conservative normalization to an HTTP(S) URL.

    This does not fetch the address and deliberately avoids opinionated path or
    query rewriting.  It lowercases the scheme/host, removes a default port and
    ensures a path, which is sufficient for deterministic comparisons.
    """

    if not isinstance(value, str):
        raise ValueError("URL must be a string")
    candidate = value.strip()
    parsed = urlsplit(candidate)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must be an absolute HTTP or HTTPS URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL contains an invalid port") from exc
    host = parsed.hostname.lower().rstrip(".")
    if not host:
        raise ValueError("URL host is empty")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    port_part = "" if port is None or default_port else f":{port}"
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs containing credentials are not accepted")
    netloc = f"{host}{port_part}"
    # Canonicalize the equivalent empty and slash root spellings to one value.
    path = parsed.path or "/"
    # A fragment can be part of the page the user intentionally chose, so keep
    # it in the published canonical link.  The health checker may separately
    # omit it from the HTTP request because fragments are client-side only.
    return urlunsplit((scheme, netloc, path, parsed.query, parsed.fragment))


def is_public_http_url(value: str) -> bool:
    """Return whether a URL is syntactically suitable for a public link check.

    Hostnames are allowed here because DNS resolution is performed by the link
    checker, which must validate every resolved address and every redirect.  IP
    literals are rejected when they are not globally routable.
    """

    try:
        normalized = normalize_http_url(value)
        hostname = urlsplit(normalized).hostname
        if hostname is None:
            return False
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return hostname.lower() != "localhost" and not hostname.lower().endswith(".localhost")
        return address.is_global
    except ValueError:
        return False


def semantic_profile_fingerprint(
    profile_status: str,
    blog_status: str,
    blog_url: str | None,
) -> str:
    """Hash only the normalized fields that define a profile observation."""

    return json_fingerprint(
        {
            "blog_status": blog_status,
            "blog_url": blog_url,
            "profile_status": profile_status,
        }
    )


def without_none(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Return a shallow copy excluding keys whose value is ``None``."""

    return {key: value for key, value in mapping.items() if value is not None}
