from __future__ import annotations

import http.client
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .constants import (
    SOURCE_CHECK_BUDGET_SECONDS,
    SOURCE_CHECK_FALLBACK_SECONDS,
    TS_PACKET_BYTES,
)

CHECKABLE_SCHEMES = ("http://", "https://")


@dataclass(frozen=True)
class Source:
    stream_id: int
    url: str | None
    user_agent: str = ""
    full: bool = False
    profile_id: int | None = None


@dataclass(frozen=True)
class Verdict:
    stream_id: int | None
    full: bool = False


def answers(url: str, user_agent: str, timeout: float) -> bool:
    request = urllib.request.Request(
        url, headers={"User-Agent": user_agent, "Connection": "close"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return bool(response.read(TS_PACKET_BYTES))
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return False


def first_worth_trying(
    sources: Sequence[Source],
    timeout: float,
    check: Callable[[str, str, float], bool] = answers,
    reserve: Callable[[Source], bool] = lambda source: True,
    release: Callable[[Source], None] = lambda source: None,
    clock: Callable[[], float] = time.monotonic,
    budget: float = SOURCE_CHECK_BUDGET_SECONDS,
) -> Verdict:
    started = clock()
    full = False
    for source in sources:
        if source.full:
            full = True
            continue
        if not source.url:
            continue
        if not source.url.startswith(CHECKABLE_SCHEMES):
            return Verdict(source.stream_id)
        if clock() - started + timeout > budget:
            return Verdict(source.stream_id)
        if not reserve(source):
            full = True
            continue
        try:
            answered = check(source.url, source.user_agent, timeout)
        finally:
            release(source)
        if answered:
            return Verdict(source.stream_id)
    return Verdict(None, full=full)


def startup_timeout() -> float:
    try:
        from apps.proxy.live_proxy.config_helper import ConfigHelper

        return float(ConfigHelper.stream_startup_timeout())
    except Exception:
        return SOURCE_CHECK_FALLBACK_SECONDS


def check(stream_ids: Sequence[int]) -> Verdict:
    from apps.m3u.connection_pool import release_profile_slot, reserve_profile_slot
    from core.utils import RedisClient

    redis_client = RedisClient.get_client()
    sources, profiles = _resolve(stream_ids, redis_client)

    def limited(source: Source) -> Any:
        profile = profiles.get(source.profile_id or 0)
        return profile if profile is not None and profile.max_streams > 0 else None

    def reserve(source: Source) -> bool:
        profile = limited(source)
        return profile is None or bool(reserve_profile_slot(profile, redis_client)[0])

    def release(source: Source) -> None:
        profile = limited(source)
        if profile is not None:
            release_profile_slot(profile.id, redis_client)

    return first_worth_trying(sources, startup_timeout(), reserve=reserve, release=release)


def _resolve(stream_ids: Sequence[int], redis_client: Any) -> tuple[list[Source], dict[int, Any]]:
    from apps.channels.models import Stream
    from apps.m3u.connection_pool import pool_has_capacity_for_profile
    from apps.proxy.live_proxy.url_utils import _resolve_live_stream_url
    from core.models import CoreSettings

    streams = Stream.objects.select_related("m3u_account__user_agent").in_bulk(list(stream_ids))
    sources: list[Source] = []
    profiles: dict[int, Any] = {}
    for stream_id in stream_ids:
        stream = streams.get(stream_id)
        if stream is None:
            continue
        account = stream.m3u_account
        if account is None:
            sources.append(
                Source(stream_id, stream.url, CoreSettings.get_default_user_agent())
            )
            continue
        if not account.is_active:
            continue
        candidates = sorted(
            account.profiles.filter(is_active=True), key=lambda profile: not profile.is_default
        )
        free = next(
            (p for p in candidates if pool_has_capacity_for_profile(p, redis_client)), None
        )
        if free is None:
            sources.append(Source(stream_id, None, full=True))
            continue
        profiles[free.id] = free
        sources.append(
            Source(
                stream_id,
                _resolve_live_stream_url(stream, account, free),
                account.get_user_agent_string(),
                profile_id=free.id,
            )
        )
    return sources, profiles
