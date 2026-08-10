from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from .constants import (
    ALLOWED_MEDIA_ROOTS,
    DOWNLOAD_HEADERS,
    IMAGE_FORMAT_NAMES,
    KIND_IMAGE,
    KIND_VIDEO,
    MEDIA_DOWNLOAD_TIMEOUT,
    MEDIA_MAX_BYTES,
)

SAFE_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".bmp",
        ".gif",
        ".tif",
        ".tiff",
        ".mp4",
        ".mkv",
        ".mov",
        ".webm",
        ".ts",
        ".m4v",
        ".avi",
    }
)


class MediaError(Exception):
    pass


@dataclass(frozen=True)
class Media:
    path: Path
    kind: str
    has_audio: bool
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class Resolved:
    path: Path
    from_cache: bool


def is_remote(source: str) -> bool:
    return urlparse(source).scheme in {"http", "https"}


def cache_name(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    suffix = Path(unquote(urlparse(url).path)).suffix.lower()
    return f"{digest}{suffix if suffix in SAFE_SUFFIXES else '.bin'}"


def validate_local_path(
    source: str, allowed_roots: Iterable[Path] = ALLOWED_MEDIA_ROOTS
) -> Path:
    path = Path(source).expanduser()
    if not path.is_absolute():
        raise MediaError(f"'{source}' is not an absolute path.")
    resolved = path.resolve()
    roots = [Path(root).resolve() for root in allowed_roots]
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        readable = ", ".join(str(root) for root in roots)
        raise MediaError(f"'{source}' is outside the allowed directories ({readable}).")
    if not resolved.is_file():
        raise MediaError(f"'{source}' does not exist or is not a file.")
    return resolved


def download(
    url: str,
    cache_dir: Path,
    timeout: tuple[int, int] = MEDIA_DOWNLOAD_TIMEOUT,
    max_bytes: int = MEDIA_MAX_BYTES,
) -> Resolved:
    import requests

    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / cache_name(url)
    partial = target.with_name(target.name + ".part")

    try:
        with requests.get(
            url, stream=True, timeout=timeout, headers=DOWNLOAD_HEADERS
        ) as response:
            response.raise_for_status()
            written = 0
            with partial.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        raise MediaError(
                            f"{url} is larger than the {max_bytes // (1024 * 1024)} MB limit."
                        )
                    handle.write(chunk)
        if written == 0:
            raise MediaError(f"{url} returned an empty body.")
        partial.replace(target)
        return Resolved(target, from_cache=False)
    except MediaError:
        partial.unlink(missing_ok=True)
        raise
    except Exception as exc:
        partial.unlink(missing_ok=True)
        if target.is_file():
            return Resolved(target, from_cache=True)
        raise MediaError(f"Could not download {url}: {exc}") from exc


def resolve(source: str, cache_dir: Path) -> Resolved:
    cleaned = (source or "").strip()
    if not cleaned:
        raise MediaError("No image or video configured.")
    if is_remote(cleaned):
        return download(cleaned, cache_dir)
    return Resolved(validate_local_path(cleaned), from_cache=False)


def _run_ffprobe(path: Path) -> dict:
    executable = shutil.which("ffprobe")
    if not executable:
        raise MediaError("ffprobe was not found; it ships with the Dispatcharr image.")
    command = [
        executable,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=30, check=False)
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"ffprobe timed out on {path}.") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "ignore").strip() or "unknown error"
        raise MediaError(f"ffprobe could not read {path}: {detail}")
    try:
        return json.loads(completed.stdout.decode("utf-8", "ignore"))
    except ValueError as exc:
        raise MediaError(f"ffprobe returned unreadable output for {path}.") from exc


def classify(probe: dict) -> tuple[str, bool]:
    streams = probe.get("streams") or []
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    if not video_streams:
        raise MediaError("The file has no video or image track.")

    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    format_names = {
        name.strip()
        for name in (probe.get("format", {}).get("format_name") or "").split(",")
        if name.strip()
    }

    if format_names & IMAGE_FORMAT_NAMES:
        return KIND_IMAGE, has_audio
    if not has_audio and video_streams[0].get("nb_frames") == "1":
        return KIND_IMAGE, has_audio
    return KIND_VIDEO, has_audio


def dimensions(probe: dict) -> tuple[int, int]:
    for stream in probe.get("streams") or []:
        if stream.get("codec_type") != "video":
            continue
        try:
            return int(stream["width"]), int(stream["height"])
        except (KeyError, TypeError, ValueError):
            return 0, 0
    return 0, 0


def inspect(path: Path) -> Media:
    probe = _run_ffprobe(path)
    kind, has_audio = classify(probe)
    width, height = dimensions(probe)
    return Media(path=path, kind=kind, has_audio=has_audio, width=width, height=height)
