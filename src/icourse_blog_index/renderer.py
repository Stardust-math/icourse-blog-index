"""Generate human-facing views from the repository's canonical user records.

The JSONL shards under :mod:`data/users` are the source of truth.  This module
deliberately reads those files directly: rendering a README must never mutate
the crawler state or reinterpret an observation.
"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit


INDEX_BEGIN = "<!-- BEGIN GENERATED INDEX -->"
INDEX_END = "<!-- END GENERATED INDEX -->"


class RenderError(RuntimeError):
    """Raised when generated output cannot safely be inserted or written."""


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "to_dict"):
        result = value.to_dict()
        if isinstance(result, Mapping):
            return result
    if hasattr(value, "__dict__"):
        return vars(value)
    raise TypeError(f"cannot render record of type {type(value).__name__}")


def _get(record: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in record and record[name] is not None:
            return _json_value(record[name])
    return default


def _status(value: Any) -> str:
    value = _json_value(value)
    return "" if value is None else str(value).strip().lower()


def _user_id(record: Mapping[str, Any]) -> int:
    value = _get(record, "id", "user_id")
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid user ID")
    return int(value)


def _blog_url(record: Mapping[str, Any]) -> str | None:
    value = _get(
        record,
        "blog_url",
        "blog_url_normalized",
        "normalized_blog_url",
        "blog",
    )
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _is_listed_blog(record: Mapping[str, Any]) -> bool:
    profile_status = _status(_get(record, "profile_status", "status"))
    blog_status = _status(_get(record, "blog_status"))
    return (
        profile_status == "public"
        and blog_status in {"present", "confirmed"}
        and _blog_url(record) is not None
    )


def markdown_escape(text: Any) -> str:
    """Escape text for a Markdown table cell or link label."""

    value = str(text)
    value = value.replace("\\", "\\\\")
    for char in ("|", "[", "]", "*", "_", "`", "<", ">"):
        value = value.replace(char, f"\\{char}")
    return value.replace("\r", " ").replace("\n", "<br>")


def markdown_destination(url: str) -> str:
    """Return a safe Markdown link destination without changing URL meaning."""

    if "\r" in url or "\n" in url:
        raise RenderError("URL contains a line break")
    # Parentheses, angle brackets, backslashes, spaces and non-ASCII characters
    # can break CommonMark destinations.  Existing percent escapes are retained.
    return quote(url, safe=":/?#@!$&'*+,;=%~._-")


def _blog_label(url: str) -> str:
    parsed = urlsplit(url)
    label = parsed.netloc or url
    if parsed.path not in {"", "/"}:
        label += parsed.path.rstrip("/")
    return label


def _format_timestamp(value: Any) -> str:
    value = _json_value(value)
    if value in (None, ""):
        return "—"
    text = str(value)
    # The canonical records use UTC ISO 8601.  A date is easier to scan in the
    # table while the complete timestamp remains available in CSV.
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    return text


def _health_status(health: Mapping[str, Any] | None) -> str:
    if not health:
        return "unchecked"
    status = _status(_get(health, "status", "health_status", default="unchecked")) or "unchecked"
    if status not in {"reachable", "redirected", "unchecked"} and (
        health.get("failure_confirmed") is False
    ):
        return "pending_failure"
    return status


def _health_label(status: str) -> str:
    return {
        "reachable": "可访问",
        "redirected": "已重定向",
        "blocked": "拒绝自动检查",
        "timeout": "检查超时",
        "dns_error": "DNS 错误",
        "tls_error": "TLS 错误",
        "client_error": "HTTP 4xx",
        "server_error": "HTTP 5xx",
        "pending_failure": "待复核",
        "unchecked": "未检查",
    }.get(status, status)


def load_records(root: Path) -> list[dict[str, Any]]:
    """Load canonical user records, rejecting duplicate IDs or invalid JSON."""

    users_dir = root / "data" / "users"
    records: dict[int, dict[str, Any]] = {}
    if not users_dir.exists():
        return []
    for path in sorted(users_dir.glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    uid = _user_id(record)
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    raise RenderError(f"{path}:{line_number}: invalid user record: {exc}") from exc
                if uid in records:
                    raise RenderError(f"duplicate user ID {uid} in data/users")
                records[uid] = record
    return [records[uid] for uid in sorted(records)]


def load_manifest(root: Path) -> dict[str, Any]:
    path = root / "data" / "manifest.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RenderError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RenderError(f"{path}: manifest must be a JSON object")
    return value


def load_link_health(root: Path) -> dict[str, dict[str, Any]]:
    """Load strict canonical health rows without a second permissive parser."""

    from .storage import DatasetCorruptionError, RepositoryStore

    try:
        return RepositoryStore(root).load_link_health()
    except DatasetCorruptionError as exc:
        raise RenderError(str(exc)) from exc


def build_status_section(records: Iterable[Any], manifest: Mapping[str, Any]) -> str:
    mappings = [_as_mapping(record) for record in records]
    profile_counts = Counter(
        _status(_get(record, "profile_status", "status", default="unknown")) or "unknown"
        for record in mappings
    )
    blog_count = sum(1 for record in mappings if _is_listed_blog(record))
    highest = max((_user_id(record) for record in mappings), default=0)
    highest_existing = _get(
        manifest,
        "highest_existing_user_id",
        "highest_valid_user_id",
        "highest_confirmed_user_id",
        default=max(
            (
                _user_id(record)
                for record in mappings
                if _status(_get(record, "profile_status", "status"))
                not in {"missing", "unknown", "transient_error"}
            ),
            default=0,
        ),
    )
    phase = str(_get(manifest, "initialization_status", "phase", default="not started"))
    phase_cell = (
        f"`{phase}`"
        if phase and all(character.isalnum() or character in {"_", "-"} for character in phase)
        else markdown_escape(phase)
    )
    updated_value = _get(manifest, "last_successful_run_at", "updated_at", "generated_at")
    updated = "—" if updated_value in (None, "") else str(updated_value)
    lines = [
        "| 指标 | 当前值 |",
        "|---|---:|",
        f"| 初始化状态 | {phase_cell} |",
        f"| 已记录 ID | {len(mappings):,} |",
        f"| 最高已尝试 ID | {highest:,} |",
        f"| 最高已确认用户 ID | {int(highest_existing or 0):,} |",
        f"| 已确认博客 | {blog_count:,} |",
        f"| 公开 / 隐藏 / 不存在 / 未决 | {profile_counts['public']:,} / "
        f"{profile_counts['hidden']:,} / {profile_counts['missing']:,} / "
        f"{sum(profile_counts[key] for key in ('unknown', 'transient_error', 'parse_error')):,} |",
        f"| 最近成功更新 | {markdown_escape(updated)} |",
    ]
    return "\n".join(lines)


def build_blogs_section(
    records: Iterable[Any], health_by_url: Mapping[str, Mapping[str, Any]] | None = None
) -> str:
    health_by_url = health_by_url or {}
    record_mappings = [_as_mapping(record) for record in records]
    mappings = sorted(
        (record for record in record_mappings if _is_listed_blog(record)),
        key=_user_id,
    )
    if not mappings:
        return "_尚未索引到已确认的博客链接。_"

    rows = [
        "| 用户 ID | 博客 | 可访问性 | 资料最后确认 |",
        "| ---: | --- | --- | --- |",
    ]
    for record in mappings:
        uid = _user_id(record)
        url = _blog_url(record)
        assert url is not None
        profile_url = str(_get(record, "profile_url", default=f"https://icourse.club/user/{uid}"))
        confirmed_at = _get(
            record,
            "last_confirmed_at",
            "last_successful_check_at",
            "last_checked_at",
        )
        health_status = _health_label(_health_status(health_by_url.get(url)))
        rows.append(
            "| "
            f"[{uid}]({markdown_destination(profile_url)}) | "
            f"[{markdown_escape(_blog_label(url))}]({markdown_destination(url)}) | "
            f"{markdown_escape(health_status)} | "
            f"{markdown_escape(_format_timestamp(confirmed_at))} |"
        )
    return "\n".join(rows)


def replace_generated_section(text: str, begin: str, end: str, content: str) -> str:
    """Replace exactly one generated section while preserving authored text."""

    if text.count(begin) != 1 or text.count(end) != 1:
        raise RenderError(f"README must contain exactly one {begin!r} and one {end!r}")
    start = text.index(begin)
    finish = text.index(end)
    if finish < start + len(begin):
        raise RenderError(f"README marker {end!r} precedes {begin!r}")
    return text[:start] + begin + "\n" + content.rstrip() + "\n" + text[finish:]


def render_readme_text(
    template: str,
    records: Iterable[Any],
    manifest: Mapping[str, Any] | None = None,
    health_by_url: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    records_list = list(records)
    generated = "\n".join(
        [
            "## 数据状态",
            "",
            build_status_section(records_list, manifest or {}),
            "",
            "## 博客索引",
            "",
            build_blogs_section(records_list, health_by_url),
        ]
    )
    return replace_generated_section(template, INDEX_BEGIN, INDEX_END, generated)


def render_blogs_csv(
    records: Iterable[Any], health_by_url: Mapping[str, Mapping[str, Any]] | None = None
) -> str:
    health_by_url = health_by_url or {}
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["user_id", "profile_url", "blog_url", "health_status", "last_confirmed_at"])
    record_mappings = [_as_mapping(record) for record in records]
    mappings = sorted(
        (record for record in record_mappings if _is_listed_blog(record)), key=_user_id
    )
    for record in mappings:
        uid = _user_id(record)
        url = _blog_url(record)
        assert url is not None
        writer.writerow(
            [
                uid,
                _get(record, "profile_url", default=f"https://icourse.club/user/{uid}"),
                url,
                _health_status(health_by_url.get(url)),
                _get(
                    record,
                    "last_confirmed_at",
                    "last_successful_check_at",
                    "last_checked_at",
                    default="",
                ),
            ]
        )
    return output.getvalue()


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def render_repository(root: str | Path = ".", *, check: bool = False) -> dict[str, int]:
    """Render README generated sections and ``data/blogs.csv``.

    With ``check=True`` no files are changed and a :class:`RenderError` is
    raised if either generated artifact is stale.
    """

    root_path = Path(root).resolve()
    readme_path = root_path / "README.md"
    if not readme_path.exists():
        raise RenderError(f"README not found: {readme_path}")
    records = load_records(root_path)
    manifest = load_manifest(root_path)
    health = load_link_health(root_path)
    rendered_readme = render_readme_text(
        readme_path.read_text(encoding="utf-8"), records, manifest, health
    )
    rendered_csv = render_blogs_csv(records, health)
    csv_path = root_path / "data" / "blogs.csv"

    if check:
        stale = []
        if rendered_readme != readme_path.read_text(encoding="utf-8"):
            stale.append("README.md")
        current_csv = csv_path.read_text(encoding="utf-8") if csv_path.exists() else None
        if rendered_csv != current_csv:
            stale.append("data/blogs.csv")
        if stale:
            raise RenderError("generated files are stale: " + ", ".join(stale))
    else:
        _atomic_write_text(readme_path, rendered_readme)
        _atomic_write_text(csv_path, rendered_csv)

    return {
        "records": len(records),
        "blogs": sum(1 for record in records if _is_listed_blog(record)),
    }


__all__ = [
    "INDEX_BEGIN",
    "INDEX_END",
    "RenderError",
    "build_blogs_section",
    "build_status_section",
    "load_link_health",
    "load_manifest",
    "load_records",
    "markdown_destination",
    "markdown_escape",
    "render_blogs_csv",
    "render_readme_text",
    "render_repository",
    "replace_generated_section",
]
