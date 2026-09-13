from __future__ import annotations

import argparse
import contextlib
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import FrameType
from urllib.parse import urlparse

from .constants import (
    API_KEY_ENV,
    CHUNK_SIZE,
    DEFAULT_FPS,
    DEFAULT_HEIGHT,
    DEFAULT_PORT,
    DEFAULT_VIDEO_KBPS,
    DEFAULT_WIDTH,
    DISPATCHARR_URL,
    ENCODER_RESTART_BACKOFF_SECONDS,
    HEALTH_PATH,
    IDLE_SHUTDOWN_SECONDS,
    KIND_IMAGE,
    KIND_VIDEO,
    LISTEN_HOST,
    PLUGIN_VERSION,
    PRIME_BUFFER_BYTES,
    PRIME_TARGET_BYTES,
    STREAM_PATH,
    SUBSCRIBER_QUEUE_CHUNKS,
)
from .encoder import EncodeOptions, build_command
from .media import Media
from .recovery import Dispatcharr, Recovery
from .transport import head_start

CHUNK_TIMEOUT = 20.0


def _log(message: str) -> None:
    print(f"[could-not-dispatch] {message}", file=sys.stderr, flush=True)


class Subscriber:
    def __init__(self, maxsize: int) -> None:
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize)
        self.overrun = False

    def offer(self, chunk: bytes) -> bool:
        try:
            self._queue.put_nowait(chunk)
            return True
        except queue.Full:
            self.overrun = True
            return False

    def close(self) -> None:
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)

    def chunks(self, timeout: float) -> Iterator[bytes]:
        while not self.overrun:
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                return
            if item is None:
                return
            yield item


class Broadcaster:
    def __init__(
        self,
        command: Sequence[str],
        chunk_size: int = CHUNK_SIZE,
        queue_chunks: int = SUBSCRIBER_QUEUE_CHUNKS,
        idle_seconds: float = IDLE_SHUTDOWN_SECONDS,
        backoff: Sequence[float] = ENCODER_RESTART_BACKOFF_SECONDS,
        spawn: Callable[[], subprocess.Popen[bytes]] | None = None,
        prime_bytes: int = PRIME_BUFFER_BYTES,
        prime_target: int = PRIME_TARGET_BYTES,
    ) -> None:
        self._command = list(command)
        self._chunk_size = chunk_size
        self._queue_chunks = queue_chunks
        self._idle_seconds = idle_seconds
        self._backoff = tuple(backoff) or (1.0,)
        self._spawn = spawn or self._spawn_encoder
        self._prime_bytes = prime_bytes
        self._prime_target = prime_target
        self._recent = bytearray()
        self._lock = threading.RLock()
        self._subscribers: set[Subscriber] = set()
        self._process: subprocess.Popen[bytes] | None = None
        self._generation = 0
        self._restarts = 0
        self._idle_timer: threading.Timer | None = None
        self._closed = False

    def _spawn_encoder(self) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            self._command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

    def subscribe(self) -> Subscriber:
        subscriber = Subscriber(self._queue_chunks)
        with self._lock:
            if self._closed:
                raise RuntimeError("The fallback encoder is shutting down.")
            primer = head_start(bytes(self._recent), self._prime_target)
            if primer:
                subscriber.offer(primer)
            self._subscribers.add(subscriber)
            self._cancel_idle_timer()
            self._start_locked()
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)
            if not self._subscribers and not self._closed:
                self._schedule_idle_stop()
        subscriber.close()

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._cancel_idle_timer()
            subscribers = tuple(self._subscribers)
            self._subscribers.clear()
            process = self._take_process_locked()
        for subscriber in subscribers:
            subscriber.close()
        self._terminate(process)

    def _start_locked(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        self._generation += 1
        generation = self._generation
        self._recent.clear()
        try:
            process = self._spawn()
        except Exception as exc:
            _log(f"could not start the encoder: {exc}")
            self._process = None
            return
        self._process = process
        threading.Thread(
            target=self._pump, args=(process, generation), daemon=True, name="cnd-pump"
        ).start()
        threading.Thread(
            target=self._drain_stderr, args=(process,), daemon=True, name="cnd-stderr"
        ).start()

    def _take_process_locked(self) -> subprocess.Popen[bytes] | None:
        process, self._process = self._process, None
        self._generation += 1
        return process

    def _pump(self, process: subprocess.Popen[bytes], generation: int) -> None:
        stdout = process.stdout
        produced = False
        try:
            while stdout is not None:
                chunk = stdout.read(self._chunk_size)
                if not chunk:
                    break
                if not produced:
                    produced = True
                    with self._lock:
                        self._restarts = 0
                self._fanout(chunk)
        except (OSError, ValueError):
            pass
        self._restart_later(generation)

    def _drain_stderr(self, process: subprocess.Popen[bytes]) -> None:
        stderr = process.stderr
        if stderr is None:
            return
        try:
            for line in stderr:
                text = line.decode("utf-8", "ignore").strip()
                if text:
                    _log(f"ffmpeg: {text}")
        except (OSError, ValueError):
            pass

    def _fanout(self, chunk: bytes) -> None:
        with self._lock:
            self._recent.extend(chunk)
            excess = len(self._recent) - self._prime_bytes
            if excess > 0:
                del self._recent[:excess]
            targets = tuple(self._subscribers)
        for subscriber in targets:
            if not subscriber.offer(chunk):
                with self._lock:
                    self._subscribers.discard(subscriber)
                subscriber.close()

    def _restart_later(self, generation: int) -> None:
        with self._lock:
            if self._closed or generation != self._generation:
                return
            if not self._subscribers:
                self._process = None
                return
            attempt = self._restarts
            self._restarts += 1
        delay = self._backoff[min(attempt, len(self._backoff) - 1)]
        _log(f"encoder stopped, restarting in {delay:.0f}s")
        time.sleep(delay)
        with self._lock:
            if self._closed or generation != self._generation or not self._subscribers:
                return
            self._process = None
            self._start_locked()

    def _schedule_idle_stop(self) -> None:
        self._cancel_idle_timer()
        timer = threading.Timer(self._idle_seconds, self._stop_if_idle)
        timer.daemon = True
        self._idle_timer = timer
        timer.start()

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _stop_if_idle(self) -> None:
        with self._lock:
            self._idle_timer = None
            if self._subscribers or self._closed:
                return
            process = self._take_process_locked()
        self._terminate(process)

    def _terminate(self, process: subprocess.Popen[bytes] | None) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
        except OSError:
            pass


class SlateServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        broadcaster: Broadcaster,
        stream_path: str,
        chunk_timeout: float = CHUNK_TIMEOUT,
    ) -> None:
        self.broadcaster = broadcaster
        self.stream_path = stream_path
        self.chunk_timeout = chunk_timeout
        super().__init__(address, SlateHandler)


class SlateHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"CouldNotDispatch/{PLUGIN_VERSION}"
    sys_version = ""

    server: SlateServer

    def do_GET(self) -> None:
        self._dispatch(with_body=True)

    def do_HEAD(self) -> None:
        self._dispatch(with_body=False)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _dispatch(self, with_body: bool) -> None:
        path = urlparse(self.path).path
        self.close_connection = True
        if path == HEALTH_PATH:
            self._send_health(with_body)
        elif path == self.server.stream_path:
            self._send_slate(with_body)
        else:
            self.send_error(404, "Not found")

    def _send_health(self, with_body: bool) -> None:
        payload = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        if with_body:
            self.wfile.write(payload)

    def _send_stream_headers(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

    def _send_slate(self, with_body: bool) -> None:
        if not with_body:
            self._send_stream_headers()
            return

        subscriber = self.server.broadcaster.subscribe()
        try:
            stream = subscriber.chunks(self.server.chunk_timeout)
            first = next(stream, None)
            if first is None:
                self.send_error(503, "The fallback encoder produced no data")
                return
            self._send_stream_headers()
            self.wfile.write(first)
            for chunk in stream:
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.broadcaster.unsubscribe(subscriber)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="could_not_dispatch.server")
    parser.add_argument("--media", required=True)
    parser.add_argument("--kind", choices=[KIND_IMAGE, KIND_VIDEO], required=True)
    parser.add_argument("--has-audio", action="store_true")
    parser.add_argument("--host", default=LISTEN_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--path", default=STREAM_PATH)
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS)
    parser.add_argument("--video-kbps", type=int, default=DEFAULT_VIDEO_KBPS)
    return parser.parse_args(argv)


def slate_url(host: str, port: int, path: str) -> str:
    return f"http://{host}:{port}{path}"


def build_recovery(url: str, api_key: str, broadcaster: Broadcaster) -> Recovery | None:
    if not api_key:
        return None
    return Recovery(Dispatcharr(DISPATCHARR_URL, api_key), url, broadcaster.subscriber_count, _log)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    media = Media(path=Path(args.media), kind=args.kind, has_audio=args.has_audio)
    options = EncodeOptions.normalized(args.width, args.height, args.fps, args.video_kbps)
    broadcaster = Broadcaster(build_command(media, options))
    server = SlateServer((args.host, args.port), broadcaster, args.path)
    recovery = build_recovery(
        slate_url(args.host, args.port, args.path), os.environ.get(API_KEY_ENV, ""), broadcaster
    )

    def request_shutdown(signum: int, frame: FrameType | None) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    for name in ("SIGTERM", "SIGINT"):
        handler = getattr(signal, name, None)
        if handler is not None:
            signal.signal(handler, request_shutdown)

    _log(f"serving {args.path} on {args.host}:{args.port} from {args.media}")
    if recovery is not None:
        recovery.start()
        _log(
            f"sending channels back to their first stream after "
            f"{recovery.first_wait:.0f}s on the fallback"
        )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        if recovery is not None:
            recovery.stop()
        broadcaster.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
