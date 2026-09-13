from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from could_not_dispatch import recovery as recovery_module
from could_not_dispatch.recovery import ApiError, Dispatcharr, Recovery, SlatedChannel
from could_not_dispatch.server import Broadcaster, build_recovery

SLATE = 22924
WEB3 = "uuid-web3"
CALCIO = "uuid-calcio"


class FakeApi:
    def __init__(self) -> None:
        self.slated: dict[str, SlatedChannel] = {}
        self.sources: dict[str, int] = {WEB3: 2262, CALCIO: 1289}
        self.changes: list[tuple[str, int]] = []
        self.status_calls = 0
        self.status_fails = False
        self.change_fails = False

    def put_on_slate(self, uuid: str, clients: int = 1) -> None:
        self.slated[uuid] = SlatedChannel(uuid=uuid, name=f"Channel {uuid}", clients=clients)

    def take_off_slate(self, uuid: str) -> None:
        self.slated.pop(uuid, None)

    def channels_on(self, stream_id: int) -> list[SlatedChannel]:
        assert stream_id == SLATE
        self.status_calls += 1
        if self.status_fails:
            raise ApiError("GET /proxy/ts/status failed: timed out")
        return list(self.slated.values())

    def first_sources(self, slate_id: int) -> dict[str, int]:
        assert slate_id == SLATE
        return dict(self.sources)

    def change_stream(self, uuid: str, stream_id: int) -> None:
        if self.change_fails:
            raise ApiError("POST change_stream returned 404")
        self.changes.append((uuid, stream_id))
        self.take_off_slate(uuid)


def build(watching: int = 1) -> tuple[Recovery, FakeApi, list[str]]:
    api = FakeApi()
    lines: list[str] = []
    recovery = Recovery(api, SLATE, lambda: watching, lines.append)
    return recovery, api, lines


def seconds(start: float, end: float, step: float = 10.0) -> Iterator[float]:
    now = start
    while now <= end:
        yield now
        now += step


def run(recovery: Recovery, start: float, end: float) -> None:
    for now in seconds(start, end):
        recovery.tick(now)


def test_a_channel_on_the_fallback_goes_back_to_its_first_stream_after_two_minutes():
    recovery, api, lines = build()
    api.put_on_slate(WEB3)
    run(recovery, 0, 110)
    assert api.changes == []
    recovery.tick(120)
    assert api.changes == [(WEB3, 2262)]
    assert lines[-1] == f"sent Channel {WEB3} back to stream 2262 after 120s on the fallback"


def test_nothing_is_asked_while_nobody_watches_the_fallback():
    recovery, api, _ = build(watching=0)
    api.put_on_slate(WEB3)
    run(recovery, 0, 600)
    assert api.status_calls == 0
    assert api.changes == []


def test_a_channel_nobody_is_watching_stays_where_it_is():
    recovery, api, _ = build()
    api.put_on_slate(WEB3, clients=0)
    run(recovery, 0, 600)
    assert api.changes == []


def test_the_wait_grows_after_each_failed_attempt():
    recovery, api, lines = build()
    api.change_fails = True
    api.put_on_slate(WEB3)
    attempts: list[float] = []
    for now in seconds(0, 1800):
        before = len(lines)
        recovery.tick(now)
        if len(lines) > before:
            attempts.append(now)
    assert attempts == [120, 360, 840, 1740]
    assert "could not send" in lines[0]


def test_a_channel_that_falls_back_again_waits_longer_before_the_next_try():
    recovery, api, _ = build()
    api.put_on_slate(WEB3)
    run(recovery, 0, 120)
    assert len(api.changes) == 1
    run(recovery, 130, 190)
    api.put_on_slate(WEB3)
    run(recovery, 200, 430)
    assert len(api.changes) == 1
    recovery.tick(440)
    assert len(api.changes) == 2


def test_the_wait_starts_over_once_the_channel_has_settled_on_a_real_stream():
    recovery, api, _ = build()
    api.put_on_slate(WEB3)
    run(recovery, 0, 120)
    run(recovery, 130, 1100)
    api.put_on_slate(WEB3)
    run(recovery, 1110, 1220)
    assert len(api.changes) == 1
    recovery.tick(1230)
    assert len(api.changes) == 2


def test_two_channels_on_the_fallback_both_go_back():
    recovery, api, _ = build()
    api.put_on_slate(WEB3)
    api.put_on_slate(CALCIO)
    run(recovery, 0, 120)
    assert sorted(api.changes) == [(CALCIO, 1289), (WEB3, 2262)]


def test_a_channel_whose_only_stream_is_the_fallback_is_left_there():
    recovery, api, lines = build()
    api.sources.pop(WEB3)
    api.put_on_slate(WEB3)
    run(recovery, 0, 120)
    assert api.changes == []
    assert lines[-1] == f"Channel {WEB3} has no stream besides the fallback"


def test_an_unreachable_api_is_reported_once_and_the_loop_carries_on():
    recovery, api, lines = build()
    api.put_on_slate(WEB3)
    api.status_fails = True
    run(recovery, 0, 300)
    assert lines == ["Dispatcharr API unavailable: GET /proxy/ts/status failed: timed out"]
    api.status_fails = False
    run(recovery, 310, 430)
    assert api.changes == [(WEB3, 2262)]


class Captured:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.requests: list[object] = []

    def urlopen(self, request: object, timeout: float) -> Captured:
        self.requests.append(request)
        return self

    def __enter__(self) -> Captured:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


@pytest.fixture
def captured(monkeypatch):
    def install(payload: object) -> Captured:
        fake = Captured(payload)
        monkeypatch.setattr(recovery_module.urllib.request, "urlopen", fake.urlopen)
        return fake

    return install


def test_only_channels_playing_the_fallback_stream_are_reported(captured):
    captured(
        {
            "channels": [
                {"channel_id": WEB3, "channel_name": "DAZN Web 3", "stream_id": SLATE,
                 "client_count": 2},
                {"channel_id": CALCIO, "channel_name": "Sky Calcio", "stream_id": 1289,
                 "client_count": 1},
            ]
        }
    )
    found = Dispatcharr("http://dispatcharr", "key").channels_on(SLATE)
    assert found == [SlatedChannel(uuid=WEB3, name="DAZN Web 3", clients=2)]


def test_the_first_source_is_the_first_stream_that_is_not_the_fallback(captured):
    captured(
        [
            {"uuid": WEB3, "streams": [2262, 2263, 2264, SLATE]},
            {"uuid": "uuid-vetrina", "streams": [SLATE, 22930]},
            {"uuid": "uuid-only-slate", "streams": [SLATE]},
        ]
    )
    sources = Dispatcharr("http://dispatcharr", "key").first_sources(SLATE)
    assert sources == {WEB3: 2262, "uuid-vetrina": 22930}


def test_changing_stream_posts_the_stream_id_with_the_key(captured):
    fake = captured({"message": "ok"})
    Dispatcharr("http://dispatcharr/", "secret").change_stream(WEB3, 2262)
    request = fake.requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == f"http://dispatcharr/proxy/ts/change_stream/{WEB3}"
    assert json.loads(request.data) == {"stream_id": 2262}
    assert request.get_header("X-api-key") == "secret"


def test_the_server_sends_channels_back_only_with_a_key_and_a_stream():
    broadcaster = Broadcaster(["ffmpeg"])
    try:
        assert build_recovery(0, "key", broadcaster) is None
        assert build_recovery(SLATE, "", broadcaster) is None
        assert build_recovery(SLATE, "key", broadcaster) is not None
    finally:
        broadcaster.close()
