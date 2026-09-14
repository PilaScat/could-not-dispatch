from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .constants import (
    API_TIMEOUT_SECONDS,
    CHAINS_REFRESH_SECONDS,
    PLUGIN_VERSION,
    RECOVERY_CROWDED_MEMORY_SECONDS,
    RECOVERY_FAST_POLL_SECONDS,
    RECOVERY_POLL_SECONDS,
    RECOVERY_SETTLED_SECONDS,
    RECOVERY_WAIT_SECONDS,
)

ERROR_BODY_CHARS = 200
CAPACITY_MARKER = "capacity"


class ApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class SlatedChannel:
    uuid: str
    name: str
    clients: int
    stream_id: int


@dataclass(frozen=True)
class Status:
    slated: list[SlatedChannel] = field(default_factory=list)
    usage: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Catalogue:
    chains: dict[str, list[int]] = field(default_factory=dict)
    accounts: dict[int, int] = field(default_factory=dict)
    limits: dict[int, dict[int, int]] = field(default_factory=dict)


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


def _local_path(url: object) -> str:
    if not url:
        return ""
    parts = urllib.parse.urlparse(str(url))
    return f"{parts.path}?{parts.query}" if parts.query else parts.path


class ChannelApi(Protocol):
    def status(self, url: str) -> Status: ...

    def catalogue(self) -> Catalogue: ...

    def change_stream(self, uuid: str, stream_id: int) -> None: ...


class Dispatcharr:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def status(self, url: str) -> Status:
        payload = self._request("/proxy/ts/status")
        rows = payload.get("channels", []) if isinstance(payload, dict) else []
        slated: list[SlatedChannel] = []
        usage: dict[int, int] = {}
        for row in _rows(rows):
            uuid = str(row.get("channel_id") or "")
            if not uuid:
                continue
            if str(row.get("url") or "") != url:
                profile = _as_int(row.get("m3u_profile_id"))
                if profile:
                    usage[profile] = usage.get(profile, 0) + 1
                continue
            slated.append(
                SlatedChannel(
                    uuid=uuid,
                    name=str(row.get("channel_name") or uuid),
                    clients=_as_int(row.get("client_count")),
                    stream_id=_as_int(row.get("stream_id")),
                )
            )
        return Status(slated=slated, usage=usage)

    def catalogue(self) -> Catalogue:
        chains: dict[str, list[int]] = {}
        for row in self._collect("/api/channels/channels/?page_size=500"):
            uuid = str(row.get("uuid") or "")
            if uuid:
                streams = (_as_int(value) for value in row.get("streams") or [])
                chains[uuid] = [stream for stream in streams if stream]
        accounts: dict[int, int] = {}
        for row in self._collect("/api/channels/streams/?page_size=9000"):
            stream, account = _as_int(row.get("id")), _as_int(row.get("m3u_account"))
            if stream and account:
                accounts[stream] = account
        limits: dict[int, dict[int, int]] = {}
        for row in self._collect("/api/m3u/accounts/"):
            account = _as_int(row.get("id"))
            if not account or row.get("is_active") is False:
                continue
            limits[account] = {
                _as_int(profile.get("id")): _as_int(profile.get("max_streams"))
                for profile in row.get("profiles") or []
                if isinstance(profile, dict) and profile.get("is_active") is not False
            }
        return Catalogue(chains=chains, accounts=accounts, limits=limits)

    def change_stream(self, uuid: str, stream_id: int) -> None:
        self._request(
            f"/proxy/ts/change_stream/{uuid}", method="POST", body={"stream_id": stream_id}
        )

    def _collect(self, path: str) -> list[dict]:
        rows: list[dict] = []
        requested: set[str] = set()
        while path and path not in requested:
            requested.add(path)
            payload = self._request(path)
            if isinstance(payload, list):
                page, path = payload, ""
            elif isinstance(payload, dict):
                page, path = payload.get("results") or [], _local_path(payload.get("next"))
            else:
                break
            rows.extend(row for row in page if isinstance(row, dict))
        return rows

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
    crowded: bool = False
    sent_crowded_at: float | None = None


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
        fast_poll_seconds: float = RECOVERY_FAST_POLL_SECONDS,
        crowded_memory_seconds: float = RECOVERY_CROWDED_MEMORY_SECONDS,
        chains_refresh_seconds: float = CHAINS_REFRESH_SECONDS,
    ) -> None:
        self._api = api
        self._slate_url = slate_url
        self._watching = watching
        self._log = log
        self._waits = tuple(waits) or (RECOVERY_WAIT_SECONDS[0],)
        self._settled_seconds = settled_seconds
        self._poll_seconds = poll_seconds
        self._fast_poll_seconds = fast_poll_seconds
        self._crowded_memory_seconds = crowded_memory_seconds
        self._chains_refresh_seconds = chains_refresh_seconds
        self._episodes: dict[str, Episode] = {}
        self._catalogue = Catalogue()
        self._catalogue_at: float | None = None
        self._full_at: dict[int, float] = {}
        self._api_down = False
        self._stopping = threading.Event()

    @property
    def first_wait(self) -> float:
        return self._waits[0]

    @property
    def interval(self) -> float:
        if any(episode.crowded for episode in self._episodes.values()):
            return self._fast_poll_seconds
        return self._poll_seconds

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._run, daemon=True, name="cnd-recovery")
        thread.start()
        return thread

    def stop(self) -> None:
        self._stopping.set()

    def _run(self) -> None:
        while not self._stopping.wait(self.interval):
            self.tick(time.monotonic())

    def tick(self, now: float) -> None:
        try:
            status = self._api.status(self._slate_url)
            self._refresh(now)
        except ApiError as error:
            self._report_unavailable(error)
            self._forget_settled(now)
            return
        self._api_down = False
        room = self._room(status.usage)
        for account, free in room.items():
            if free <= 0:
                self._full_at[account] = now
        if self._watching() > 0:
            watched = [channel for channel in status.slated if channel.clients > 0]
            for channel in sorted(watched, key=lambda channel: self._since(channel, now)):
                self._consider(channel, now, room)
        self._forget_settled(now)

    def _since(self, channel: SlatedChannel, now: float) -> float:
        episode = self._episodes.get(channel.uuid)
        return episode.since if episode is not None else now

    def _consider(self, channel: SlatedChannel, now: float, room: dict[int, int]) -> None:
        account = self._account_of(self._first_source(channel))
        episode = self._episodes.get(channel.uuid)
        if episode is None or now - episode.seen_at > 2 * self._poll_seconds:
            tries = episode.tries if episode is not None else 0
            crowded = account is not None and self._was_full(account, now)
            if episode is not None and self._bounced(episode, now):
                crowded = False
                tries += 1
            episode = Episode(
                since=now, seen_at=now, due_at=now + self._wait(tries), tries=tries,
                crowded=crowded,
            )
            self._episodes[channel.uuid] = episode
            if crowded:
                self._log(
                    f"{channel.name} is on the fallback while the provider is full; "
                    f"it goes back as soon as a connection frees up"
                )
        episode.seen_at = now
        if episode.crowded:
            if account is None or room.get(account, 1) <= 0:
                return
            if self._send_back(channel, episode, now, counted=False) and account in room:
                room[account] -= 1
            return
        if now >= episode.due_at:
            self._send_back(channel, episode, now, counted=True)

    def _send_back(
        self, channel: SlatedChannel, episode: Episode, now: float, counted: bool
    ) -> bool:
        waited = now - episode.since
        target = self._first_source(channel)
        if target is None:
            episode.tries += 1
            episode.due_at = now + self._wait(episode.tries)
            episode.crowded = False
            self._log(f"{channel.name} has no stream besides the fallback")
            return False
        try:
            self._api.change_stream(channel.uuid, target)
        except ApiError as error:
            if not (episode.crowded and CAPACITY_MARKER in str(error).lower()):
                episode.crowded = False
                episode.tries += 1
                episode.due_at = now + self._wait(episode.tries)
            self._log(
                f"could not send {channel.name} back after {waited:.0f}s on the fallback: {error}"
            )
            return False
        if counted:
            episode.tries += 1
        else:
            episode.sent_crowded_at = now
        episode.due_at = now + self._wait(episode.tries)
        episode.crowded = False
        reason = " as a connection freed up" if not counted else ""
        self._log(
            f"sent {channel.name} back to stream {target} after {waited:.0f}s "
            f"on the fallback{reason}"
        )
        return True

    def _refresh(self, now: float) -> None:
        stale = (
            self._catalogue_at is None
            or now - self._catalogue_at >= self._chains_refresh_seconds
        )
        if stale:
            self._catalogue = self._api.catalogue()
            self._catalogue_at = now

    def _first_source(self, channel: SlatedChannel) -> int | None:
        chain = self._catalogue.chains.get(channel.uuid, [])
        return next((stream for stream in chain if stream != channel.stream_id), None)

    def _account_of(self, stream: int | None) -> int | None:
        if stream is None:
            return None
        account = self._catalogue.accounts.get(stream)
        return account if account in self._catalogue.limits else None

    def _room(self, usage: dict[int, int]) -> dict[int, int]:
        room: dict[int, int] = {}
        for account, profiles in self._catalogue.limits.items():
            if not profiles or any(limit <= 0 for limit in profiles.values()):
                continue
            used = sum(usage.get(profile, 0) for profile in profiles)
            room[account] = sum(profiles.values()) - used
        return room

    def _was_full(self, account: int, now: float) -> bool:
        seen = self._full_at.get(account)
        return seen is not None and now - seen <= self._crowded_memory_seconds

    def _bounced(self, episode: Episode, now: float) -> bool:
        sent = episode.sent_crowded_at
        return sent is not None and now - sent < self._waits[0]

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
