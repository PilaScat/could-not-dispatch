from __future__ import annotations

import json
import logging
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

try:
    from .could_not_dispatch import media as media_module
    from .could_not_dispatch import process, targeting
    from .could_not_dispatch import state as state_module
    from .could_not_dispatch.constants import (
        API_KEY_ENV,
        DEFAULT_PORT,
        HEALTH_PATH,
        LISTEN_HOST,
        PLUGIN_DESCRIPTION,
        PLUGIN_NAME,
        PLUGIN_VERSION,
        STARTUP_PROBE_SECONDS,
        STREAM_NAME,
        STREAM_PATH,
    )
    from .could_not_dispatch.encoder import EncodeOptions
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from could_not_dispatch import media as media_module
    from could_not_dispatch import process, targeting
    from could_not_dispatch import state as state_module
    from could_not_dispatch.constants import (
        API_KEY_ENV,
        DEFAULT_PORT,
        HEALTH_PATH,
        LISTEN_HOST,
        PLUGIN_DESCRIPTION,
        PLUGIN_NAME,
        PLUGIN_VERSION,
        STARTUP_PROBE_SECONDS,
        STREAM_NAME,
        STREAM_PATH,
    )
    from could_not_dispatch.encoder import EncodeOptions

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / ".runtime"
MEDIA_CACHE_DIR = RUNTIME_DIR / "media"
LOG_PATH = RUNTIME_DIR / "fallback.log"

STATE_PATH = RUNTIME_DIR / "state.json"

HEARTBEAT_INTERVAL_SECONDS = 60.0
DETACHING_REASONS = frozenset({"disable", "delete"})

_heartbeat_lock = threading.Lock()
_heartbeat_checked_at = 0.0


def _read_manifest() -> dict[str, Any]:
    try:
        with (BASE_DIR / "plugin.json").open(encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


_MANIFEST = _read_manifest()


def _running_inside_uwsgi() -> bool:
    try:
        import uwsgi  # noqa: F401
    except ImportError:
        return False
    return True


def _as_int(value: object, fallback: int) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return fallback


def _api_key(settings: dict) -> str:
    return str(settings.get("api_key") or "").strip()


class Plugin:
    name = _MANIFEST.get("name", PLUGIN_NAME)
    version = _MANIFEST.get("version", PLUGIN_VERSION)
    description = _MANIFEST.get("description", PLUGIN_DESCRIPTION)
    author = _MANIFEST.get("author", "")
    help_url = _MANIFEST.get("help_url", "")
    fields = _MANIFEST.get("fields", [])
    actions = _MANIFEST.get("actions", [])

    def __init__(self) -> None:
        self._key = BASE_DIR.name.replace(" ", "_").lower()

    def run(self, action: str, params: dict, context: dict) -> dict:
        handlers = {
            "apply": self._apply,
            "status": self._status,
            "remove": self._remove,
            "reapply": self._reapply,
            "restart": self._restart,
        }
        handler = handlers.get((action or "").strip().lower())
        if handler is None:
            return {"status": "error", "message": f"Unknown action '{action}'."}
        try:
            return handler(dict(context or {}))
        except media_module.MediaError as exc:
            return {"status": "error", "message": str(exc)}
        except Exception as exc:
            logger.exception("Could Not Dispatch action '%s' failed", action)
            return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

    def stop(self, context: dict | None = None) -> dict:
        context = dict(context or {})
        settings = dict(context.get("settings") or {})
        reason = str(context.get("reason") or "")

        stopped = self._stop_fallback(settings)

        detached = 0
        if reason in DETACHING_REASONS:
            stream = targeting.find_stream(STREAM_NAME, self._state().get("stream_id"))
            if stream is not None:
                detached = targeting.detach(stream)
            self._remember({}, clear=["pid", "token", "signature", "stream_id", "applied"])
        else:
            self._remember({}, clear=["pid", "token"])

        return {
            "status": "ok",
            "message": self._stop_message(reason, stopped, detached),
        }

    def _stop_message(self, reason: str, stopped: bool, detached: int) -> str:
        if reason in DETACHING_REASONS:
            return f"Fallback stopped and detached from {detached} channel(s)."
        return "Fallback stopped." if stopped else "Fallback was not running."

    def _apply(self, context: dict) -> dict:
        settings = dict(context.get("settings") or {})
        port = _as_int(settings.get("port"), DEFAULT_PORT)

        resolved = media_module.resolve(settings.get("media_source", ""), MEDIA_CACHE_DIR)
        media = media_module.inspect(resolved.path)
        options = self._encode_options(settings, media)

        stream, created = targeting.ensure_stream(
            STREAM_NAME, self._stream_url(port), self._state().get("stream_id")
        )
        api_key = _api_key(settings)
        signature = self._signature(media, options, port, self._recovery_stream(stream.id, api_key))

        started = self._ensure_process(signature, media, options, port, stream.id, api_key)

        channel_ids = targeting.target_channel_ids(
            settings.get("exclude_groups"), settings.get("exclude_channels")
        )
        attached = targeting.attach(stream, channel_ids)

        self._remember(
            {"stream_id": stream.id, "signature": signature, "applied": True}
        )

        return {
            "status": "ok",
            "message": self._apply_message(
                resolved, media, options, started, created, attached, len(channel_ids)
            ),
        }

    def _apply_message(
        self,
        resolved: media_module.Resolved,
        media: media_module.Media,
        options: EncodeOptions,
        started: bool,
        created: bool,
        attached: int,
        covered: int,
    ) -> str:
        parts = [
            f"Fallback {'started' if started else 'already running'} from the "
            f"{media.kind} at {options.width}x{options.height}."
        ]
        if resolved.from_cache:
            parts.append("The download failed, so the cached copy is in use.")
        parts.append(
            f"Attached to {attached} new channel(s); {covered} channel(s) covered in total."
        )
        if created:
            parts.append(f"Created the '{STREAM_NAME}' stream.")
        return " ".join(parts)

    def _reapply(self, context: dict) -> dict:
        settings = dict(context.get("settings") or {})
        if not self._state().get("applied"):
            return {"status": "ok", "message": "Nothing to do: the fallback is not applied."}
        if not settings.get("auto_reapply", True) and not context.get("params"):
            return {"status": "ok", "message": "Covering new channels is switched off."}

        stream = targeting.find_stream(STREAM_NAME, self._state().get("stream_id"))
        if stream is None:
            return {"status": "error", "message": "The fallback stream no longer exists."}

        channel_ids = targeting.target_channel_ids(
            settings.get("exclude_groups"), settings.get("exclude_channels")
        )
        attached = targeting.attach(stream, channel_ids)
        return {
            "status": "ok",
            "message": (
                f"Attached to {attached} new channel(s); "
                f"{len(channel_ids)} channel(s) covered in total."
            ),
        }

    def _restart(self, context: dict) -> dict:
        settings = dict(context.get("settings") or {})
        state = self._state()
        if not state.get("applied"):
            return {"status": "ok", "message": "Nothing to do: the fallback is not applied."}
        if not _running_inside_uwsgi():
            return {
                "status": "ok",
                "message": "Skipped: the fallback only starts in the Dispatcharr web process.",
            }
        if not self._heartbeat_due():
            return {"status": "ok", "message": "Checked recently."}

        if process.is_running(state.get("pid"), state.get("token")):
            return {"status": "ok", "message": "Fallback is running."}

        port = _as_int(settings.get("port"), DEFAULT_PORT)
        resolved = media_module.resolve(settings.get("media_source", ""), MEDIA_CACHE_DIR)
        media = media_module.inspect(resolved.path)
        self._start_process(
            media,
            self._encode_options(settings, media),
            port,
            _as_int(state.get("stream_id"), 0),
            _api_key(settings),
        )
        return {"status": "ok", "message": "Fallback restarted."}

    def _status(self, context: dict) -> dict:
        settings = dict(context.get("settings") or {})
        state = self._state()
        port = _as_int(settings.get("port"), DEFAULT_PORT)
        tracked = process.is_running(state.get("pid"), state.get("token"))
        answering = self._probe(port)
        strays = [] if tracked else process.find_servers(port)
        stream = targeting.find_stream(STREAM_NAME, state.get("stream_id"))
        covered = targeting.attached_count(stream) if stream is not None else 0

        if tracked and answering:
            headline = "Fallback is running."
        elif answering:
            headline = "Fallback answers on its port but is not the one this plugin started."
        elif strays:
            headline = (
                "A stale fallback holds the port without answering. "
                "Press Apply to reclaim it."
            )
        else:
            headline = "Fallback is not running."

        return {
            "status": "ok" if tracked and answering else "error",
            "message": f"{headline} {covered} channel(s) carry it.",
            "url": self._stream_url(port),
            "running": tracked and answering,
            "channels": covered,
        }

    def _remove(self, context: dict) -> dict:
        settings = dict(context.get("settings") or {})
        self._stop_fallback(settings)

        detached = 0
        stream = targeting.find_stream(STREAM_NAME, self._state().get("stream_id"))
        if stream is not None:
            detached = targeting.detach(stream)
            stream.delete()

        state_module.save(STATE_PATH, {})
        return {
            "status": "ok",
            "message": f"Fallback removed from {detached} channel(s) and stopped.",
        }

    def _ensure_process(
        self,
        signature: str,
        media: media_module.Media,
        options: EncodeOptions,
        port: int,
        stream_id: int,
        api_key: str,
    ) -> bool:
        state = self._state()
        pid = state.get("pid")
        token = state.get("token")
        if process.is_running(pid, token):
            if state.get("signature") == signature:
                return False
            process.terminate(pid, token)
        self._start_process(media, options, port, stream_id, api_key)
        return True

    def _stop_fallback(self, settings: dict) -> bool:
        state = self._state()
        stopped = process.terminate(state.get("pid"), state.get("token"))
        strays = process.terminate_strays(_as_int(settings.get("port"), DEFAULT_PORT))
        if strays:
            logger.info("Could Not Dispatch stopped %s stray fallback process(es)", strays)
        return stopped or bool(strays)

    def _start_process(
        self,
        media: media_module.Media,
        options: EncodeOptions,
        port: int,
        stream_id: int,
        api_key: str,
    ) -> None:
        if not process.port_is_free(LISTEN_HOST, port):
            reclaimed = process.terminate_strays(port)
            if reclaimed:
                logger.info(
                    "Could Not Dispatch reclaimed port %s from %s stray process(es)",
                    port,
                    reclaimed,
                )
            if not process.port_is_free(LISTEN_HOST, port):
                raise RuntimeError(
                    f"Port {port} on {LISTEN_HOST} is held by something else; "
                    f"pick another one."
                )

        token = uuid.uuid4().hex
        arguments = [
            "--media",
            str(media.path),
            "--kind",
            media.kind,
            "--host",
            LISTEN_HOST,
            "--port",
            str(port),
            "--path",
            STREAM_PATH,
            "--width",
            str(options.width),
            "--height",
            str(options.height),
            "--fps",
            str(options.fps),
            "--video-kbps",
            str(options.video_kbps),
        ]
        if media.has_audio:
            arguments.append("--has-audio")
        recovery_stream = self._recovery_stream(stream_id, api_key)
        if recovery_stream:
            arguments += ["--stream-id", str(recovery_stream)]

        pid = process.spawn(
            BASE_DIR,
            arguments,
            token,
            LOG_PATH,
            extra_env={API_KEY_ENV: api_key} if recovery_stream else None,
        )
        self._remember({"pid": pid, "token": token, "started_at": time.time()})

        if not self._wait_for_port(port):
            process.terminate(pid)
            process.terminate_strays(port)
            self._remember({}, clear=["pid", "token"])
            raise RuntimeError(
                f"The fallback did not answer on port {port}. See {LOG_PATH} for details."
            )

    def _wait_for_port(self, port: int) -> bool:
        deadline = time.monotonic() + STARTUP_PROBE_SECONDS
        while time.monotonic() < deadline:
            if self._probe(port):
                return True
            time.sleep(0.25)
        return False

    def _probe(self, port: int, timeout: float = 2.0) -> bool:
        url = f"http://{LISTEN_HOST}:{port}{HEALTH_PATH}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def _heartbeat_due(self) -> bool:
        global _heartbeat_checked_at
        now = time.monotonic()
        with _heartbeat_lock:
            if now - _heartbeat_checked_at < HEARTBEAT_INTERVAL_SECONDS:
                return False
            _heartbeat_checked_at = now
        return True

    def _encode_options(
        self, settings: dict, media: media_module.Media | None = None
    ) -> EncodeOptions:
        return EncodeOptions.normalized(
            settings.get("width"),
            settings.get("height"),
            settings.get("fps"),
            settings.get("video_kbps"),
            source_width=media.width if media else 0,
            source_height=media.height if media else 0,
        )

    def _signature(
        self,
        media: media_module.Media,
        options: EncodeOptions,
        port: int,
        recovery_stream: int,
    ) -> str:
        return json.dumps(
            {
                "media": str(media.path),
                "kind": media.kind,
                "has_audio": media.has_audio,
                "port": port,
                "width": options.width,
                "height": options.height,
                "fps": options.fps,
                "video_kbps": options.video_kbps,
                "recovery_stream": recovery_stream,
            },
            sort_keys=True,
        )

    def _recovery_stream(self, stream_id: int, api_key: str) -> int:
        return stream_id if stream_id > 0 and api_key else 0

    def _stream_url(self, port: int) -> str:
        return f"http://{LISTEN_HOST}:{port}{STREAM_PATH}"

    def _state(self) -> dict:
        return state_module.load(STATE_PATH)

    def _remember(self, updates: dict, clear: list[str] | None = None) -> dict:
        return state_module.remember(STATE_PATH, updates, clear or [])
