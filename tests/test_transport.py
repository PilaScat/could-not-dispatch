from __future__ import annotations

from could_not_dispatch.transport import (
    TS_PACKET_SIZE,
    entry_points,
    find_alignment,
    head_start,
    packet_pid,
    starts_random_access,
)


def packet(pid: int = 0x0100, random_access: bool = False, payload: int = 0x11) -> bytes:
    header = bytearray(b"\x47\x00\x00\x00")
    header[1] = (pid >> 8) & 0x1F
    header[2] = pid & 0xFF
    if random_access:
        header[3] = 0x30
        body = bytearray([1, 0x40])
    else:
        header[3] = 0x10
        body = bytearray()
    out = bytes(header) + bytes(body)
    return out + bytes([payload]) * (TS_PACKET_SIZE - len(out))


def test_alignment_is_zero_for_a_clean_stream():
    assert find_alignment(packet() * 3) == 0


def test_alignment_skips_a_partial_leading_packet():
    stream = packet() * 3
    assert find_alignment(stream[57:]) == TS_PACKET_SIZE - 57


def test_alignment_reports_failure_when_there_is_no_sync():
    assert find_alignment(b"\x00" * 400) == -1


def test_packet_pid_is_read_from_the_header():
    assert packet_pid(packet(pid=0x0000)) == 0x0000
    assert packet_pid(packet(pid=0x1FFF)) == 0x1FFF


def test_a_random_access_packet_is_recognised():
    assert starts_random_access(packet(random_access=True)) is True
    assert starts_random_access(packet(random_access=False)) is False


def test_entry_points_are_found_in_order():
    stream = (
        packet(payload=1)
        + packet(random_access=True, payload=2)
        + packet(payload=3)
        + packet(random_access=True, payload=4)
    )
    assert entry_points(stream) == [TS_PACKET_SIZE, TS_PACKET_SIZE * 3]


def test_an_entry_point_includes_the_table_that_precedes_the_picture():
    stream = packet(payload=1) + packet(pid=0x0000) + packet(random_access=True, payload=2)
    assert entry_points(stream) == [TS_PACKET_SIZE]


def test_head_start_hands_over_as_much_as_asked_for():
    stream = b"".join(
        packet(random_access=True, payload=i) if i % 2 == 0 else packet(payload=i)
        for i in range(20)
    )
    wanted = TS_PACKET_SIZE * 9
    primer = head_start(stream, wanted)
    assert 0 < len(primer) <= wanted
    assert len(primer) % TS_PACKET_SIZE == 0
    assert starts_random_access(primer[:TS_PACKET_SIZE])


def test_head_start_falls_back_to_the_last_entry_point_when_all_are_too_big():
    stream = b"".join(
        packet(random_access=True, payload=i) if i == 0 else packet(payload=i)
        for i in range(20)
    )
    primer = head_start(stream, TS_PACKET_SIZE)
    assert len(primer) == TS_PACKET_SIZE * 20


def test_no_head_start_without_an_entry_point():
    assert head_start(packet() * 5, 4096) == b""
    assert head_start(b"", 4096) == b""
    assert head_start(b"\x01\x02", 4096) == b""
