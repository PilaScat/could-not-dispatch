from __future__ import annotations

import itertools
import threading
import time
import urllib.error
import urllib.request

import pytest
from conftest import FakeProcess

from could_not_dispatch.server import Broadcaster, SlateServer, Subscriber


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


class _Served:
    def __init__(self, broadcaster: Broadcaster, chunk_timeout: float = 1.0) -> None:
        self.broadcaster = broadcaster
        self.server = SlateServer(("127.0.0.1", 0), broadcaster, "/slate.ts", chunk_timeout)
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
        self.server.server_close()


@pytest.fixture
def served():
    made: list[_Served] = []

    def build(broadcaster: Broadcaster, chunk_timeout: float = 1.0) -> _Served:
        made.append(_Served(broadcaster, chunk_timeout))
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
