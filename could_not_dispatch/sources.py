from __future__ import annotations

import http.client
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass

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
    clock: Callable[[], float] = time.monotonic,
    budget: float = SOURCE_CHECK_BUDGET_SECONDS,
) -> int | None:
    started = clock()
    for source in sources:
        if source.full:
            return source.stream_id
        if not source.url:
            continue
        if not source.url.startswith(CHECKABLE_SCHEMES):
            return source.stream_id
        if clock() - started + timeout > budget:
            return source.stream_id
        if check(source.url, source.user_agent, timeout):
            return source.stream_id
    return None


def startup_timeout() -> float:
    try:
        from apps.proxy.live_proxy.config_helper import ConfigHelper

        return float(ConfigHelper.stream_startup_timeout())
    except Exception:
        return SOURCE_CHECK_FALLBACK_SECONDS


def resolve(stream_ids: Sequence[int]) -> list[Source]:
    from apps.channels.models import Stream
    from apps.m3u.connection_pool import pool_has_capacity_for_profile
    from apps.proxy.live_proxy.url_utils import _resolve_live_stream_url
    from core.models import CoreSettings
    from core.utils import RedisClient

    redis_client = RedisClient.get_client()
    streams = Stream.objects.select_related("m3u_account__user_agent").in_bulk(list(stream_ids))
    sources: list[Source] = []
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
        profiles = sorted(
            account.profiles.filter(is_active=True), key=lambda profile: not profile.is_default
        )
        free = next(
            (p for p in profiles if pool_has_capacity_for_profile(p, redis_client)), None
        )
        if free is None:
            sources.append(Source(stream_id, None, full=True))
            continue
        sources.append(
            Source(
                stream_id,
                _resolve_live_stream_url(stream, account, free),
                account.get_user_agent_string(),
            )
        )
    return sources
