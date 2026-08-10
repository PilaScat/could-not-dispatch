from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
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


def attach(stream: Any, channel_ids: Sequence[int]) -> int:
    from apps.channels.models import ChannelStream
    from django.db import transaction
    from django.db.models import Max

    if not channel_ids:
        return 0

    with transaction.atomic():
        already = set(
            ChannelStream.objects.filter(
                stream=stream, channel_id__in=channel_ids
            ).values_list("channel_id", flat=True)
        )
        pending = [channel_id for channel_id in channel_ids if channel_id not in already]
        if not pending:
            return 0

        max_orders = {
            row["channel_id"]: row["highest"]
            for row in ChannelStream.objects.filter(channel_id__in=pending)
            .values("channel_id")
            .annotate(highest=Max("order"))
        }
        ChannelStream.objects.bulk_create(
            [
                ChannelStream(
                    channel_id=channel_id,
                    stream=stream,
                    order=next_order(max_orders, channel_id),
                )
                for channel_id in pending
            ],
            ignore_conflicts=True,
        )
    return len(pending)


def detach(stream: Any) -> int:
    from apps.channels.models import ChannelStream

    deleted, _ = ChannelStream.objects.filter(stream=stream).delete()
    return int(deleted)


def attached_count(stream: Any) -> int:
    from apps.channels.models import ChannelStream

    return int(ChannelStream.objects.filter(stream=stream).count())


def ensure_stream(name: str, url: str, stream_id: object = None) -> tuple[Any, bool]:
    from apps.channels.models import Stream

    stream = None
    if stream_id:
        stream = Stream.objects.filter(id=stream_id).first()
    if stream is None:
        stream = Stream.objects.filter(name=name, is_custom=True).first()
    if stream is None:
        return Stream.objects.create(name=name, url=url), True
    if stream.url != url:
        stream.url = url
        stream.save(update_fields=["url"])
    return stream, False


def find_stream(name: str, stream_id: object = None) -> Any:
    from apps.channels.models import Stream

    if stream_id:
        found = Stream.objects.filter(id=stream_id).first()
        if found is not None:
            return found
    return Stream.objects.filter(name=name, is_custom=True).first()
