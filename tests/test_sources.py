from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from could_not_dispatch.sources import Source, answers, first_worth_trying

TS_PACKET = b"\x47" + bytes(187)


class Provider(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/live.ts":
            assert self.headers["User-Agent"] == "VLC/3.0"
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.end_headers()
            self.wfile.write(TS_PACKET * 4)
        elif self.path == "/moved.ts":
            self.send_response(302)
            self.send_header("Location", "/live.ts")
            self.end_headers()
        elif self.path == "/empty.ts":
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_error(404)

    def log_message(self, *_: object) -> None:
        return None


@pytest.fixture
def provider() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def silent() -> Iterator[str]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}/live.ts"
    finally:
        listener.close()


def refused_url() -> str:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}/live.ts"


def test_a_stream_sending_data_answers(provider):
    assert answers(f"{provider}/live.ts", "VLC/3.0", 2)


def test_a_redirect_to_a_stream_sending_data_answers(provider):
    assert answers(f"{provider}/moved.ts", "VLC/3.0", 2)


def test_an_http_error_does_not_answer(provider):
    assert not answers(f"{provider}/gone.ts", "VLC/3.0", 2)


def test_an_empty_body_does_not_answer(provider):
    assert not answers(f"{provider}/empty.ts", "VLC/3.0", 2)


def test_a_refused_connection_does_not_answer():
    assert not answers(refused_url(), "VLC/3.0", 2)


def test_a_silent_source_does_not_answer_within_the_timeout(silent):
    assert not answers(silent, "VLC/3.0", 0.5)


def checker(answering: set[str]) -> tuple[list[str], object]:
    checked: list[str] = []

    def check(url: str, user_agent: str, timeout: float) -> bool:
        checked.append(url)
        return url in answering

    return checked, check


def test_the_first_answering_stream_is_worth_trying():
    checked, check = checker({"http://p/2.ts"})
    sources = [Source(1, "http://p/1.ts"), Source(2, "http://p/2.ts"), Source(3, "http://p/3.ts")]
    assert first_worth_trying(sources, 8, check) == 2
    assert checked == ["http://p/1.ts", "http://p/2.ts"]


def test_with_no_stream_answering_none_is_worth_trying():
    _, check = checker(set())
    assert first_worth_trying([Source(1, "http://p/1.ts")], 8, check) is None


def test_a_stream_dispatcharr_cannot_build_a_url_for_is_skipped():
    checked, check = checker({"http://p/2.ts"})
    assert first_worth_trying([Source(1, None), Source(2, "http://p/2.ts")], 8, check) == 2
    assert checked == ["http://p/2.ts"]


def test_a_stream_on_a_full_account_is_left_to_dispatcharr_unchecked():
    checked, check = checker(set())
    assert first_worth_trying([Source(1, None, full=True)], 8, check) == 1
    assert checked == []


def test_a_stream_that_is_not_http_is_left_to_dispatcharr_unchecked():
    checked, check = checker(set())
    assert first_worth_trying([Source(1, "udp://239.0.0.1:1234")], 8, check) == 1
    assert checked == []


def test_streams_past_the_budget_are_left_to_dispatcharr_unchecked():
    moments = iter([0.0, 0.0, 9.0, 18.0])
    checked, check = checker(set())
    sources = [Source(stream, f"http://p/{stream}.ts") for stream in (1, 2, 3)]
    assert first_worth_trying(sources, 8, check, clock=lambda: next(moments), budget=25) == 3
    assert checked == ["http://p/1.ts", "http://p/2.ts"]
