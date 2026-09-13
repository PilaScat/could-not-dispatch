from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from .constants import (
    API_TIMEOUT_SECONDS,
    CHAINS_REFRESH_SECONDS,
    PLUGIN_VERSION,
    RECOVERY_POLL_SECONDS,
    RECOVERY_SETTLED_SECONDS,
    RECOVERY_WAIT_SECONDS,
)

ERROR_BODY_CHARS = 200


class ApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class SlatedChannel:
    uuid: str
    name: str
    clients: int
    stream_id: int


def _as_int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _rows(payload: object) -> list[dict]:
    if isinstance(payload, dict):
        payload = payload.get("results", [])
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


class ChannelApi(Protocol):
    def channels_on(self, url: str) -> list[SlatedChannel]: ...

    def chains(self) -> dict[str, list[int]]: ...

    def change_stream(self, uuid: str, stream_id: int) -> None: ...


class Dispatcharr:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def channels_on(self, url: str) -> list[SlatedChannel]:
        payload = self._request("/proxy/ts/status")
        rows = payload.get("channels", []) if isinstance(payload, dict) else []
        found: list[SlatedChannel] = []
        for row in _rows(rows):
            uuid = str(row.get("channel_id") or "")
            if not uuid or str(row.get("url") or "") != url:
                continue
            found.append(
                SlatedChannel(
                    uuid=uuid,
                    name=str(row.get("channel_name") or uuid),
                    clients=_as_int(row.get("client_count")),
                    stream_id=_as_int(row.get("stream_id")),
                )
            )
        return found

    def chains(self) -> dict[str, list[int]]:
        chains: dict[str, list[int]] = {}
        for row in _rows(self._request("/api/channels/channels/")):
            uuid = str(row.get("uuid") or "")
            if uuid:
                streams = (_as_int(value) for value in row.get("streams") or [])
                chains[uuid] = [stream for stream in streams if stream]
        return chains

    def change_stream(self, uuid: str, stream_id: int) -> None:
        self._request(
            f"/proxy/ts/change_stream/{uuid}", method="POST", body={"stream_id": stream_id}
        )

    def _request(
        self, path: str, method: str = "GET", body: Mapping[str, object] | None = None
    ) -> object:
        headers = {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": f"could-not-dispatch/{PLUGIN_VERSION}",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read()[:ERROR_BODY_CHARS].decode("utf-8", "ignore").strip()
            raise ApiError(f"{method} {path} returned {error.code} {detail}".strip()) from error
        except (urllib.error.URLError, OSError) as error:
            raise ApiError(f"{method} {path} failed: {error}") from error
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError as error:
            raise ApiError(f"{method} {path} returned invalid JSON") from error


@dataclass
class Episode:
    since: float
    seen_at: float
    due_at: float
    tries: int = 0


class Recovery:
    def __init__(
        self,
        api: ChannelApi,
        slate_url: str,
        watching: Callable[[], int],
        log: Callable[[str], None],
        waits: Sequence[float] = RECOVERY_WAIT_SECONDS,
        settled_seconds: float = RECOVERY_SETTLED_SECONDS,
        poll_seconds: float = RECOVERY_POLL_SECONDS,
        chains_refresh_seconds: float = CHAINS_REFRESH_SECONDS,
    ) -> None:
        self._api = api
        self._slate_url = slate_url
        self._watching = watching
        self._log = log
        self._waits = tuple(waits) or (RECOVERY_WAIT_SECONDS[0],)
        self._settled_seconds = settled_seconds
        self._poll_seconds = poll_seconds
        self._chains_refresh_seconds = chains_refresh_seconds
        self._episodes: dict[str, Episode] = {}
        self._chains: dict[str, list[int]] = {}
        self._chains_at: float | None = None
        self._api_down = False
        self._stopping = threading.Event()

    @property
    def first_wait(self) -> float:
        return self._waits[0]

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._run, daemon=True, name="cnd-recovery")
        thread.start()
        return thread

    def stop(self) -> None:
        self._stopping.set()

    def _run(self) -> None:
        while not self._stopping.wait(self._poll_seconds):
            self.tick(time.monotonic())

    def tick(self, now: float) -> None:
        if self._watching() > 0:
            self._check(now)
        self._forget_settled(now)

    def _check(self, now: float) -> None:
        try:
            slated = self._api.channels_on(self._slate_url)
        except ApiError as error:
            self._report_unavailable(error)
            return
        self._api_down = False
        for channel in slated:
            if channel.clients > 0:
                self._consider(channel, now)

    def _consider(self, channel: SlatedChannel, now: float) -> None:
        episode = self._episodes.get(channel.uuid)
        if episode is None or now - episode.seen_at > 2 * self._poll_seconds:
            tries = episode.tries if episode is not None else 0
            episode = Episode(since=now, seen_at=now, due_at=now + self._wait(tries), tries=tries)
            self._episodes[channel.uuid] = episode
        episode.seen_at = now
        if now >= episode.due_at:
            self._send_back(channel, episode, now)

    def _send_back(self, channel: SlatedChannel, episode: Episode, now: float) -> None:
        episode.tries += 1
        episode.due_at = now + self._wait(episode.tries)
        waited = now - episode.since
        try:
            target = self._first_source(channel, now)
            if target is None:
                self._log(f"{channel.name} has no stream besides the fallback")
                return
            self._api.change_stream(channel.uuid, target)
        except ApiError as error:
            self._log(
                f"could not send {channel.name} back after {waited:.0f}s on the fallback: {error}"
            )
            return
        self._log(
            f"sent {channel.name} back to stream {target} after {waited:.0f}s on the fallback"
        )

    def _first_source(self, channel: SlatedChannel, now: float) -> int | None:
        stale = self._chains_at is None or now - self._chains_at >= self._chains_refresh_seconds
        if stale or channel.uuid not in self._chains:
            self._chains = self._api.chains()
            self._chains_at = now
        chain = self._chains.get(channel.uuid, [])
        return next((stream for stream in chain if stream != channel.stream_id), None)

    def _wait(self, tries: int) -> float:
        return self._waits[min(tries, len(self._waits) - 1)]

    def _forget_settled(self, now: float) -> None:
        for uuid, episode in list(self._episodes.items()):
            if now - episode.seen_at >= self._settled_seconds:
                del self._episodes[uuid]

    def _report_unavailable(self, error: ApiError) -> None:
        if not self._api_down:
            self._log(f"Dispatcharr API unavailable: {error}")
        self._api_down = True
