from __future__ import annotations

import itertools
import threading
import time
import urllib.error
import urllib.request

import pytest
from conftest import FakeProcess

from could_not_dispatch import server as server_module
from could_not_dispatch.constants import IN_USE_EMPTY_TEXT
from could_not_dispatch.server import (
    Broadcaster,
    SlateServer,
    Subscriber,
    build_recovery,
    parse_args,
)


def _wait_until(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_subscriber_yields_what_it_was_offered():
    subscriber = Subscriber(4)
    subscriber.offer(b"ab")
    subscriber.close()
    assert list(subscriber.chunks(1.0)) == [b"ab"]


def test_subscriber_marks_itself_overrun_once_its_queue_is_full():
    subscriber = Subscriber(1)
    assert subscriber.offer(b"a") is True
    assert subscriber.offer(b"b") is False
    assert subscriber.overrun is True
    assert list(subscriber.chunks(0.1)) == []


def test_subscriber_stops_waiting_after_the_timeout():
    started = time.monotonic()
    assert list(Subscriber(4).chunks(0.05)) == []
    assert time.monotonic() - started < 2.0


def _recording_spawn(spawned: list[FakeProcess]):
    def spawn() -> FakeProcess:
        spawned.append(FakeProcess())
        return spawned[-1]

    return spawn


def test_encoder_starts_only_once_someone_is_listening():
    spawned: list[FakeProcess] = []
    broadcaster = Broadcaster(["ffmpeg"], spawn=_recording_spawn(spawned))
    try:
        assert spawned == []
        broadcaster.subscribe()
        assert len(spawned) == 1
    finally:
        broadcaster.close()


def test_every_listener_receives_the_same_bytes():
    encoder = FakeProcess()
    broadcaster = Broadcaster(["ffmpeg"], spawn=lambda: encoder, chunk_size=8)
    try:
        first = broadcaster.subscribe()
        second = broadcaster.subscribe()
        encoder.feed(b"12345678")
        assert next(first.chunks(3.0)) == b"12345678"
        assert next(second.chunks(3.0)) == b"12345678"
    finally:
        broadcaster.close()


def test_a_listener_that_stops_reading_is_dropped_and_the_others_keep_going():
    encoder = FakeProcess()
    broadcaster = Broadcaster(["ffmpeg"], spawn=lambda: encoder, chunk_size=1, queue_chunks=4)
    received: list[bytes] = []
    try:
        stalled = broadcaster.subscribe()
        healthy = broadcaster.subscribe()

        reader = threading.Thread(
            target=lambda: received.extend(itertools.islice(healthy.chunks(3.0), 40)),
            daemon=True,
        )
        reader.start()
        for _ in range(40):
            encoder.feed(b"z")
            time.sleep(0.002)
        reader.join(timeout=5)

        assert stalled.overrun is True
        assert healthy.overrun is False
        assert len(received) == 40
    finally:
        broadcaster.close()


def test_the_encoder_is_restarted_while_a_listener_is_still_connected():
    spawned: list[FakeProcess] = []

    broadcaster = Broadcaster(
        ["ffmpeg"], spawn=_recording_spawn(spawned), chunk_size=8, backoff=(0.01,)
    )
    try:
        subscriber = broadcaster.subscribe()
        spawned[0].feed(b"first")
        assert next(subscriber.chunks(3.0)) == b"first"

        spawned[0].close_writer()
        assert _wait_until(lambda: len(spawned) == 2)

        spawned[1].feed(b"second")
        assert next(subscriber.chunks(3.0)) == b"second"
    finally:
        broadcaster.close()


def test_the_encoder_is_stopped_once_the_last_listener_leaves():
    encoder = FakeProcess()
    broadcaster = Broadcaster(["ffmpeg"], spawn=lambda: encoder, idle_seconds=0.05)
    try:
        subscriber = broadcaster.subscribe()
        assert encoder.poll() is None
        broadcaster.unsubscribe(subscriber)
        assert _wait_until(lambda: encoder.poll() is not None)
    finally:
        broadcaster.close()


def test_the_encoder_is_not_restarted_after_the_last_listener_left():
    spawned: list[FakeProcess] = []

    broadcaster = Broadcaster(
        ["ffmpeg"], spawn=_recording_spawn(spawned), idle_seconds=0.05, backoff=(0.01,)
    )
    try:
        subscriber = broadcaster.subscribe()
        broadcaster.unsubscribe(subscriber)
        assert _wait_until(lambda: spawned[0].poll() is not None)
        time.sleep(0.2)
        assert len(spawned) == 1
    finally:
        broadcaster.close()


def test_a_later_listener_is_handed_a_head_start_from_a_random_access_point():
    from test_transport import packet

    from could_not_dispatch.transport import TS_PACKET_SIZE, starts_random_access

    encoder = FakeProcess()
    broadcaster = Broadcaster(
        ["ffmpeg"],
        spawn=lambda: encoder,
        chunk_size=TS_PACKET_SIZE,
        prime_target=TS_PACKET_SIZE * 4,
    )
    try:
        early = broadcaster.subscribe()
        for i in range(6):
            encoder.feed(packet(random_access=(i % 3 == 0), payload=i))
        assert len(list(itertools.islice(early.chunks(3.0), 6))) == 6

        late = broadcaster.subscribe()
        primer = next(late.chunks(3.0))
        assert starts_random_access(primer[:TS_PACKET_SIZE])
        assert len(primer) % TS_PACKET_SIZE == 0
        assert len(primer) > TS_PACKET_SIZE
    finally:
        broadcaster.close()


def test_a_listener_after_a_restart_is_not_handed_stale_bytes():
    from test_transport import packet

    from could_not_dispatch.transport import TS_PACKET_SIZE

    spawned: list[FakeProcess] = []
    broadcaster = Broadcaster(
        ["ffmpeg"],
        spawn=_recording_spawn(spawned),
        chunk_size=TS_PACKET_SIZE,
        backoff=(0.01,),
    )
    try:
        listener = broadcaster.subscribe()
        spawned[0].feed(packet(random_access=True, payload=1))
        assert next(listener.chunks(3.0))

        spawned[0].close_writer()
        assert _wait_until(lambda: len(spawned) == 2)

        late = broadcaster.subscribe()
        assert list(late.chunks(0.2)) == []
    finally:
        broadcaster.close()


def test_a_listener_after_an_idle_stop_is_not_handed_the_previous_run():
    from test_transport import packet

    from could_not_dispatch.transport import TS_PACKET_SIZE

    spawned: list[FakeProcess] = []
    broadcaster = Broadcaster(
        ["ffmpeg"],
        spawn=_recording_spawn(spawned),
        chunk_size=TS_PACKET_SIZE,
        idle_seconds=0.05,
    )
    try:
        listener = broadcaster.subscribe()
        spawned[0].feed(packet(random_access=True, payload=1))
        assert next(listener.chunks(3.0))
        broadcaster.unsubscribe(listener)
        assert _wait_until(lambda: spawned[0].poll() is not None)

        late = broadcaster.subscribe()
        assert len(spawned) == 2
        assert list(late.chunks(0.2)) == []
        spawned[1].feed(packet(random_access=True, payload=2))
        assert next(late.chunks(3.0)) == packet(random_access=True, payload=2)
    finally:
        broadcaster.close()


class _Served:
    def __init__(
        self,
        broadcaster: Broadcaster,
        chunk_timeout: float = 1.0,
        crowded_broadcaster: Broadcaster | None = None,
        crowded=None,
    ) -> None:
        self.broadcaster = broadcaster
        self.crowded_broadcaster = crowded_broadcaster
        self.server = SlateServer(
            ("127.0.0.1", 0),
            broadcaster,
            "/slate.ts",
            chunk_timeout,
            crowded_broadcaster=crowded_broadcaster,
            crowded=crowded,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    @property
    def base(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.broadcaster.close()
        if self.crowded_broadcaster is not None:
            self.crowded_broadcaster.close()
        self.server.server_close()


@pytest.fixture
def served():
    made: list[_Served] = []

    def build(broadcaster: Broadcaster, chunk_timeout: float = 1.0, **cards) -> _Served:
        made.append(_Served(broadcaster, chunk_timeout, **cards))
        return made[-1]

    yield build
    for item in made:
        item.close()


def test_health_endpoint_answers_without_starting_the_encoder(served):
    spawned: list[FakeProcess] = []
    running = served(Broadcaster(["ffmpeg"], spawn=_recording_spawn(spawned)))
    with urllib.request.urlopen(f"{running.base}/healthz", timeout=5) as response:
        assert response.status == 200
        assert b'"ok"' in response.read()
    assert spawned == []


def test_an_unknown_path_is_a_404(served):
    running = served(Broadcaster(["ffmpeg"], spawn=FakeProcess))
    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib.request.urlopen(f"{running.base}/nope", timeout=5)
    assert raised.value.code == 404


def test_the_stream_path_serves_transport_stream_bytes(served):
    encoder = FakeProcess()
    running = served(Broadcaster(["ffmpeg"], spawn=lambda: encoder, chunk_size=4))
    feeder = threading.Thread(
        target=lambda: [encoder.feed(b"tsda") or time.sleep(0.01) for _ in range(20)],
        daemon=True,
    )
    feeder.start()
    with urllib.request.urlopen(f"{running.base}/slate.ts", timeout=5) as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "video/mp2t"
        assert response.read(8) == b"tsdatsda"


def test_a_silent_encoder_answers_503_instead_of_an_empty_stream(served):
    running = served(
        Broadcaster(["ffmpeg"], spawn=lambda: FakeProcess(keep_open=False), backoff=(0.01,)),
        chunk_timeout=0.2,
    )
    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib.request.urlopen(f"{running.base}/slate.ts", timeout=10)
    assert raised.value.code == 503


def test_the_listener_is_released_when_the_client_disconnects(served):
    encoder = FakeProcess()
    broadcaster = Broadcaster(["ffmpeg"], spawn=lambda: encoder, chunk_size=4)
    running = served(broadcaster)
    feeder = threading.Thread(
        target=lambda: [encoder.feed(b"tsda") or time.sleep(0.01) for _ in range(200)],
        daemon=True,
    )
    feeder.start()
    response = urllib.request.urlopen(f"{running.base}/slate.ts", timeout=5)
    assert response.read(4) == b"tsda"
    response.close()
    assert _wait_until(lambda: broadcaster.subscriber_count() == 0)


def _feeding(payload: bytes) -> Broadcaster:
    encoder = FakeProcess()
    threading.Thread(
        target=lambda: [encoder.feed(payload) or time.sleep(0.01) for _ in range(50)],
        daemon=True,
    ).start()
    return Broadcaster(["ffmpeg"], spawn=lambda: encoder, chunk_size=4)


@pytest.mark.parametrize(
    ("query", "expected"),
    [("?channel=uuid-full", b"full"), ("?channel=uuid-other", b"card"), ("", b"card")],
)
def test_a_channel_that_found_the_provider_full_gets_the_card_for_it(served, query, expected):
    running = served(
        _feeding(b"card"),
        crowded_broadcaster=_feeding(b"full"),
        crowded=lambda uuid: uuid == "uuid-full",
    )
    with urllib.request.urlopen(f"{running.base}/slate.ts{query}", timeout=5) as response:
        assert response.read(4) == expected


def test_without_a_card_for_a_full_provider_every_channel_gets_the_one_card(served):
    running = served(_feeding(b"card"), crowded=lambda uuid: True)
    with urllib.request.urlopen(f"{running.base}/slate.ts?channel=uuid-full", timeout=5) as r:
        assert r.read(4) == b"card"


def _arguments(*extra: str):
    return parse_args(["--media", "/data/card.png", "--kind", "image", *extra])


FULL_CARD = ("--full-media", "/data/full.png", "--full-kind", "image")


def test_the_channels_in_use_are_drawn_only_with_a_card_for_a_full_provider_and_a_key(
    monkeypatch,
):
    monkeypatch.setattr(server_module, "ffmpeg_supports_caption", lambda: True)
    asked = _arguments(*FULL_CARD, "--in-use-file", "/tmp/in-use.txt")
    assert server_module._caption(asked, "key").text_file.name == "in-use.txt"
    assert server_module._caption(asked, "") is None
    assert server_module._caption(_arguments("--in-use-file", "/tmp/in-use.txt"), "key") is None
    assert server_module._caption(_arguments(*FULL_CARD), "key") is None


def test_an_ffmpeg_that_cannot_draw_the_caption_leaves_the_card_as_it_is(monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr(server_module, "ffmpeg_supports_caption", lambda: False)
    monkeypatch.setattr(server_module, "_log", lines.append)
    asked = _arguments(*FULL_CARD, "--in-use-file", "/tmp/in-use.txt", "--in-use-font", "/f.ttf")
    assert server_module._caption(asked, "key") is None
    assert len(lines) == 1


def test_the_text_file_is_ready_before_the_card_starts(tmp_path):
    path = tmp_path / "in-use.txt"
    broadcaster = Broadcaster(["ffmpeg"])
    try:
        recovery = build_recovery("http://127.0.0.1:9721/slate.ts", "key", broadcaster,
                                  in_use_file=path)
        assert recovery is not None
        assert path.read_text(encoding="utf-8") == IN_USE_EMPTY_TEXT
    finally:
        broadcaster.close()
