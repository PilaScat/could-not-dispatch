from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


def parse_lines(raw: object) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        candidates: Iterable[str] = [str(item) for item in raw]
    else:
        candidates = str(raw).splitlines()

    seen: set[str] = set()
    result: list[str] = []
    for candidate in candidates:
        cleaned = candidate.strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def split_channel_entries(entries: Sequence[str]) -> tuple[list[float], list[str]]:
    numbers: list[float] = []
    names: list[str] = []
    for entry in entries:
        try:
            numbers.append(float(entry))
        except ValueError:
            names.append(entry)
    return numbers, names


def next_order(max_orders: Mapping[int, int | None], channel_id: int) -> int:
    highest = max_orders.get(channel_id)
    return 0 if highest is None else int(highest) + 1


def target_channel_ids(exclude_groups: object, exclude_channels: object) -> list[int]:
    from apps.channels.models import Channel
    from django.db.models import Q

    queryset = Channel.objects.all()

    groups = parse_lines(exclude_groups)
    if groups:
        group_filter = Q()
        for name in groups:
            group_filter |= Q(channel_group__name__iexact=name)
        queryset = queryset.exclude(group_filter)

    numbers, names = split_channel_entries(parse_lines(exclude_channels))
    channel_filter = Q()
    if numbers:
        channel_filter |= Q(channel_number__in=numbers)
    for name in names:
        channel_filter |= Q(name__iexact=name)
    if channel_filter:
        queryset = queryset.exclude(channel_filter)

    return list(queryset.values_list("id", flat=True))


@dataclass(frozen=True)
class Link:
    id: int
    channel_id: int
    stream_id: int


@dataclass(frozen=True)
class FallbackPlan:
    shared_links: list[int]
    uncovered_channels: list[int]
    spare_streams: list[int]


@dataclass(frozen=True)
class Attachment:
    attached: int
    separated: int
    created: int


def plan_fallbacks(
    links: Sequence[Link], stream_ids: Sequence[int], channel_ids: Sequence[int]
) -> FallbackPlan:
    owned: set[int] = set()
    shared_links: list[int] = []
    for link in sorted(links, key=lambda item: (item.stream_id, item.channel_id)):
        if link.stream_id in owned:
            shared_links.append(link.id)
        else:
            owned.add(link.stream_id)

    covered = {link.channel_id for link in links}
    return FallbackPlan(
        shared_links=shared_links,
        uncovered_channels=[channel_id for channel_id in channel_ids if channel_id not in covered],
        spare_streams=sorted(stream_id for stream_id in stream_ids if stream_id not in owned),
    )


def _fallback_streams(name: str) -> Any:
    from apps.channels.models import Stream

    return Stream.objects.filter(name=name, is_custom=True)


def attach(name: str, url: str, channel_ids: Sequence[int]) -> Attachment:
    from apps.channels.models import ChannelStream, Stream
    from django.db import transaction
    from django.db.models import Max

    with transaction.atomic():
        streams = _fallback_streams(name)
        streams.exclude(url=url).update(url=url)
        stream_ids = list(streams.values_list("id", flat=True))
        links = [
            Link(*row)
            for row in ChannelStream.objects.filter(stream_id__in=stream_ids).values_list(
                "id", "channel_id", "stream_id"
            )
        ]
        plan = plan_fallbacks(links, stream_ids, channel_ids)
        spare = list(plan.spare_streams)
        created = 0

        def own_stream() -> int:
            nonlocal created
            if spare:
                return spare.pop(0)
            created += 1
            return int(Stream.objects.create(name=name, url=url).id)

        for link_id in plan.shared_links:
            ChannelStream.objects.filter(id=link_id).update(stream_id=own_stream())

        if plan.uncovered_channels:
            max_orders = {
                row["channel_id"]: row["highest"]
                for row in ChannelStream.objects.filter(channel_id__in=plan.uncovered_channels)
                .values("channel_id")
                .annotate(highest=Max("order"))
            }
            ChannelStream.objects.bulk_create(
                [
                    ChannelStream(
                        channel_id=channel_id,
                        stream_id=own_stream(),
                        order=next_order(max_orders, channel_id),
                    )
                    for channel_id in plan.uncovered_channels
                ],
                ignore_conflicts=True,
            )

        if spare:
            Stream.objects.filter(id__in=spare).delete()

    return Attachment(
        attached=len(plan.uncovered_channels),
        separated=len(plan.shared_links),
        created=created,
    )


def detach(name: str) -> int:
    from apps.channels.models import ChannelStream

    deleted, _ = ChannelStream.objects.filter(stream__in=_fallback_streams(name)).delete()
    return int(deleted)


def delete_streams(name: str) -> int:
    deleted, _ = _fallback_streams(name).delete()
    return int(deleted)


def attached_count(name: str) -> int:
    from apps.channels.models import ChannelStream

    return int(ChannelStream.objects.filter(stream__in=_fallback_streams(name)).count())
