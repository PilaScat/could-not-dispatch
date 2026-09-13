from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from could_not_dispatch import recovery as recovery_module
from could_not_dispatch.recovery import ApiError, Dispatcharr, Recovery, SlatedChannel
from could_not_dispatch.server import Broadcaster, build_recovery, slate_url

SLATE_URL = "http://127.0.0.1:9721/slate.ts"
WEB3 = "uuid-web3"
CALCIO = "uuid-calcio"
OWN_SLATE = {WEB3: 22924, CALCIO: 22931}


class FakeApi:
    def __init__(self) -> None:
        self.slated: dict[str, SlatedChannel] = {}
        self.chain: dict[str, list[int]] = {
            WEB3: [2262, 2263, OWN_SLATE[WEB3]],
            CALCIO: [1289, OWN_SLATE[CALCIO]],
        }
        self.changes: list[tuple[str, int]] = []
        self.status_calls = 0
        self.status_fails = False
        self.change_fails = False

    def put_on_slate(self, uuid: str, clients: int = 1) -> None:
        self.slated[uuid] = SlatedChannel(
            uuid=uuid, name=f"Channel {uuid}", clients=clients, stream_id=OWN_SLATE[uuid]
        )

    def take_off_slate(self, uuid: str) -> None:
        self.slated.pop(uuid, None)

    def channels_on(self, url: str) -> list[SlatedChannel]:
        assert url == SLATE_URL
        self.status_calls += 1
        if self.status_fails:
            raise ApiError("GET /proxy/ts/status failed: timed out")
        return list(self.slated.values())

    def chains(self) -> dict[str, list[int]]:
        return {uuid: list(streams) for uuid, streams in self.chain.items()}

    def change_stream(self, uuid: str, stream_id: int) -> None:
        if self.change_fails:
            raise ApiError("POST change_stream returned 404")
        self.changes.append((uuid, stream_id))
        self.take_off_slate(uuid)


def build(watching: int = 1) -> tuple[Recovery, FakeApi, list[str]]:
    api = FakeApi()
    lines: list[str] = []
    recovery = Recovery(api, SLATE_URL, lambda: watching, lines.append)
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
    api.chain[WEB3] = [OWN_SLATE[WEB3]]
    api.put_on_slate(WEB3)
    run(recovery, 0, 120)
    assert api.changes == []
    assert lines[-1] == f"Channel {WEB3} has no stream besides the fallback"


def test_the_first_source_skips_the_channels_own_fallback_wherever_it_sits():
    recovery, api, _ = build()
    api.chain[WEB3] = [OWN_SLATE[WEB3], 22930]
    api.put_on_slate(WEB3)
    run(recovery, 0, 120)
    assert api.changes == [(WEB3, 22930)]


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


def test_only_channels_playing_the_fallback_url_are_reported(captured):
    captured(
        {
            "channels": [
                {"channel_id": WEB3, "channel_name": "DAZN Web 3", "stream_id": 22924,
                 "url": SLATE_URL, "client_count": 2},
                {"channel_id": CALCIO, "channel_name": "Sky Calcio", "stream_id": 1289,
                 "url": "http://provider/live/u/p/1289.ts", "client_count": 1},
                {"channel_id": "uuid-bench", "channel_name": "Bench", "stream_id": 23660,
                 "url": f"{SLATE_URL}?bench=a", "client_count": 1},
            ]
        }
    )
    found = Dispatcharr("http://dispatcharr", "key").channels_on(SLATE_URL)
    assert found == [SlatedChannel(uuid=WEB3, name="DAZN Web 3", clients=2, stream_id=22924)]


def test_chains_keep_each_channels_stream_order(captured):
    captured(
        [
            {"uuid": WEB3, "streams": [2262, 2263, 2264, 22924]},
            {"uuid": "uuid-vetrina", "streams": [22929, 22930]},
            {"uuid": "", "streams": [1]},
        ]
    )
    chains = Dispatcharr("http://dispatcharr", "key").chains()
    assert chains == {WEB3: [2262, 2263, 2264, 22924], "uuid-vetrina": [22929, 22930]}


def test_changing_stream_posts_the_stream_id_with_the_key(captured):
    fake = captured({"message": "ok"})
    Dispatcharr("http://dispatcharr/", "secret").change_stream(WEB3, 2262)
    request = fake.requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == f"http://dispatcharr/proxy/ts/change_stream/{WEB3}"
    assert json.loads(request.data) == {"stream_id": 2262}
    assert request.get_header("X-api-key") == "secret"


def test_the_server_sends_channels_back_only_with_a_key():
    broadcaster = Broadcaster(["ffmpeg"])
    try:
        assert build_recovery(SLATE_URL, "", broadcaster) is None
        assert build_recovery(SLATE_URL, "key", broadcaster) is not None
    finally:
        broadcaster.close()


def test_the_server_watches_the_url_the_plugin_attaches():
    assert slate_url("127.0.0.1", 9721, "/slate.ts") == SLATE_URL
