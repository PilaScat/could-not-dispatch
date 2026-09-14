from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from could_not_dispatch import recovery as recovery_module
from could_not_dispatch.recovery import (
    ApiError,
    Catalogue,
    Dispatcharr,
    Recovery,
    SlatedChannel,
    Status,
)
from could_not_dispatch.server import Broadcaster, build_recovery, slate_url

SLATE_URL = "http://127.0.0.1:9721/slate.ts"
WEB3 = "uuid-web3"
CALCIO = "uuid-calcio"
OWN_SLATE = {WEB3: 22924, CALCIO: 22931}
PROVIDER, PROVIDER_PROFILE, CUSTOM, CUSTOM_PROFILE = 2, 2, 1, 1


class FakeApi:
    def __init__(self) -> None:
        self.slated: dict[str, SlatedChannel] = {}
        self.chain: dict[str, list[int]] = {
            WEB3: [2262, 2263, OWN_SLATE[WEB3]],
            CALCIO: [1289, OWN_SLATE[CALCIO]],
        }
        self.accounts = {2262: PROVIDER, 2263: PROVIDER, 1289: PROVIDER,
                         OWN_SLATE[WEB3]: CUSTOM, OWN_SLATE[CALCIO]: CUSTOM}
        self.limits = {PROVIDER: {PROVIDER_PROFILE: 3}, CUSTOM: {CUSTOM_PROFILE: 0}}
        self.busy = 0
        self.changes: list[tuple[str, int]] = []
        self.status_calls = 0
        self.status_fails = False
        self.change_fails = False
        self.capacity_refusals = 0

    def put_on_slate(self, uuid: str, clients: int = 1) -> None:
        self.slated[uuid] = SlatedChannel(
            uuid=uuid, name=f"Channel {uuid}", clients=clients, stream_id=OWN_SLATE[uuid]
        )

    def take_off_slate(self, uuid: str) -> None:
        self.slated.pop(uuid, None)

    def status(self, url: str) -> Status:
        assert url == SLATE_URL
        self.status_calls += 1
        if self.status_fails:
            raise ApiError("GET /proxy/ts/status failed: timed out")
        return Status(slated=list(self.slated.values()), usage={PROVIDER_PROFILE: self.busy})

    def catalogue(self) -> Catalogue:
        return Catalogue(
            chains={uuid: list(streams) for uuid, streams in self.chain.items()},
            accounts=dict(self.accounts),
            limits={account: dict(profiles) for account, profiles in self.limits.items()},
        )

    def change_stream(self, uuid: str, stream_id: int) -> None:
        if self.capacity_refusals:
            self.capacity_refusals -= 1
            raise ApiError(
                "POST change_stream returned 503 No profiles available with connection capacity"
            )
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


def run(recovery: Recovery, start: float, end: float, step: float = 10.0) -> None:
    for now in seconds(start, end, step):
        recovery.tick(now)


def test_a_channel_on_the_fallback_goes_back_to_its_first_stream_after_two_minutes():
    recovery, api, lines = build()
    api.put_on_slate(WEB3)
    run(recovery, 0, 110)
    assert api.changes == []
    recovery.tick(120)
    assert api.changes == [(WEB3, 2262)]
    assert lines[-1] == f"sent Channel {WEB3} back to stream 2262 after 120s on the fallback"


def test_nothing_is_sent_back_while_nobody_watches_the_fallback():
    recovery, api, _ = build(watching=0)
    api.put_on_slate(WEB3)
    run(recovery, 0, 600)
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
    api.accounts[22930] = PROVIDER
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


def test_a_channel_that_found_the_provider_full_goes_back_as_soon_as_a_connection_frees_up():
    recovery, api, lines = build()
    api.busy = 3
    recovery.tick(0)
    api.busy = 2
    api.put_on_slate(WEB3)
    recovery.tick(2)
    assert api.changes == [(WEB3, 2262)]
    assert lines == [
        f"Channel {WEB3} is on the fallback while the provider is full; "
        f"it goes back as soon as a connection frees up",
        f"sent Channel {WEB3} back to stream 2262 after 0s on the fallback "
        f"as a connection freed up",
    ]


def test_while_the_provider_stays_full_nothing_is_tried_and_the_check_runs_every_two_seconds():
    recovery, api, _ = build()
    api.busy = 3
    api.put_on_slate(WEB3)
    run(recovery, 0, 600, step=2.0)
    assert api.changes == []
    assert recovery.interval == 2.0
    api.busy = 2
    recovery.tick(602)
    assert api.changes == [(WEB3, 2262)]
    assert recovery.interval == 10.0


def test_a_full_provider_seen_long_before_the_fallback_keeps_the_two_minute_wait():
    recovery, api, _ = build()
    api.busy = 3
    recovery.tick(0)
    api.busy = 1
    run(recovery, 10, 30)
    api.put_on_slate(WEB3)
    run(recovery, 40, 150)
    assert api.changes == []
    recovery.tick(160)
    assert api.changes == [(WEB3, 2262)]


def test_a_capacity_refusal_does_not_count_as_a_try():
    recovery, api, lines = build()
    api.busy = 3
    recovery.tick(0)
    api.busy = 2
    api.capacity_refusals = 2
    api.put_on_slate(WEB3)
    run(recovery, 2, 6, step=2.0)
    assert api.changes == [(WEB3, 2262)]
    assert sum("could not send" in line for line in lines) == 2
    assert recovery.interval == 10.0


def test_one_free_connection_takes_back_the_channel_that_has_waited_longest():
    recovery, api, _ = build()
    api.busy = 3
    api.put_on_slate(WEB3)
    recovery.tick(0)
    api.put_on_slate(CALCIO)
    recovery.tick(2)
    api.busy = 2
    recovery.tick(4)
    assert api.changes == [(WEB3, 2262)]


def test_a_channel_back_on_the_fallback_soon_after_a_crowded_return_waits_like_a_failure():
    recovery, api, _ = build()
    api.busy = 3
    recovery.tick(0)
    api.busy = 2
    api.put_on_slate(WEB3)
    recovery.tick(2)
    assert len(api.changes) == 1
    run(recovery, 12, 32)
    api.busy = 3
    api.put_on_slate(WEB3)
    api.busy = 2
    run(recovery, 42, 272)
    assert len(api.changes) == 1
    recovery.tick(282)
    assert len(api.changes) == 2


class Routed:
    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.requests: list[object] = []

    def urlopen(self, request: Any, timeout: float) -> Response:
        self.requests.append(request)
        url = request.full_url
        path = url.split("//", 1)[1].split("/", 1)[1]
        return Response(self.routes["/" + path])


class Response:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


@pytest.fixture
def routed(monkeypatch):
    def install(routes: dict[str, object]) -> Routed:
        fake = Routed(routes)
        monkeypatch.setattr(recovery_module.urllib.request, "urlopen", fake.urlopen)
        return fake

    return install


def test_status_reports_fallback_channels_and_counts_the_rest_per_profile(routed):
    routed(
        {
            "/proxy/ts/status": {
                "channels": [
                    {"channel_id": WEB3, "channel_name": "DAZN Web 3", "stream_id": 22924,
                     "url": SLATE_URL, "client_count": 2, "m3u_profile_id": CUSTOM_PROFILE},
                    {"channel_id": CALCIO, "channel_name": "Sky Calcio", "stream_id": 1289,
                     "url": "http://provider/live/u/p/1289.ts", "client_count": 1,
                     "m3u_profile_id": PROVIDER_PROFILE},
                    {"channel_id": "uuid-bench", "channel_name": "Bench", "stream_id": 23660,
                     "url": f"{SLATE_URL}?bench=a", "client_count": 1,
                     "m3u_profile_id": PROVIDER_PROFILE},
                ]
            }
        }
    )
    status = Dispatcharr("http://dispatcharr", "key").status(SLATE_URL)
    assert status.slated == [
        SlatedChannel(uuid=WEB3, name="DAZN Web 3", clients=2, stream_id=22924)
    ]
    assert status.usage == {PROVIDER_PROFILE: 2}


def test_the_catalogue_follows_pages_and_skips_inactive_accounts_and_profiles(routed):
    routed(
        {
            "/api/channels/channels/?page_size=500": {
                "results": [{"uuid": WEB3, "streams": [2262, 2263, 22924]}],
                "next": "http://dispatcharr/api/channels/channels/?page=2&page_size=500",
            },
            "/api/channels/channels/?page=2&page_size=500": {
                "results": [{"uuid": "", "streams": [1]},
                            {"uuid": "uuid-vetrina", "streams": [22929, 22930]}],
                "next": None,
            },
            "/api/channels/streams/?page_size=9000": [
                {"id": 2262, "m3u_account": PROVIDER}, {"id": 22924, "m3u_account": CUSTOM},
            ],
            "/api/m3u/accounts/": [
                {"id": PROVIDER, "is_active": True, "profiles": [
                    {"id": PROVIDER_PROFILE, "max_streams": 3, "is_active": True},
                    {"id": 9, "max_streams": 2, "is_active": False},
                ]},
                {"id": 5, "is_active": False, "profiles": [{"id": 6, "max_streams": 1}]},
            ],
        }
    )
    catalogue = Dispatcharr("http://dispatcharr", "key").catalogue()
    assert catalogue.chains == {WEB3: [2262, 2263, 22924], "uuid-vetrina": [22929, 22930]}
    assert catalogue.accounts == {2262: PROVIDER, 22924: CUSTOM}
    assert catalogue.limits == {PROVIDER: {PROVIDER_PROFILE: 3}}


def test_changing_stream_posts_the_stream_id_with_the_key(routed):
    fake = routed({f"/proxy/ts/change_stream/{WEB3}": {"message": "ok"}})
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
