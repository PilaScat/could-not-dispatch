from __future__ import annotations

TS_PACKET_SIZE = 188
SYNC_BYTE = 0x47
PAT_PID = 0x0000


def find_alignment(data: bytes) -> int:
    limit = len(data) - TS_PACKET_SIZE
    for offset in range(min(len(data), TS_PACKET_SIZE)):
        if data[offset] != SYNC_BYTE:
            continue
        if offset > limit or data[offset + TS_PACKET_SIZE] == SYNC_BYTE:
            return offset
    return -1


def packet_pid(packet: bytes) -> int:
    return ((packet[1] & 0x1F) << 8) | packet[2]


def starts_random_access(packet: bytes) -> bool:
    if len(packet) < 6 or packet[0] != SYNC_BYTE:
        return False
    adaptation = (packet[3] >> 4) & 0x03
    if adaptation not in (2, 3):
        return False
    if packet[4] == 0:
        return False
    return bool(packet[5] & 0x40)


def entry_points(data: bytes) -> list[int]:
    start = find_alignment(data)
    if start < 0:
        return []

    points: list[int] = []
    table = -1
    offset = start
    while offset + TS_PACKET_SIZE <= len(data):
        packet = data[offset : offset + TS_PACKET_SIZE]
        if packet_pid(packet) == PAT_PID:
            if table < 0:
                table = offset
        elif starts_random_access(packet):
            points.append(table if table >= 0 else offset)
            table = -1
        offset += TS_PACKET_SIZE
    return points


def head_start(data: bytes, wanted: int) -> bytes:
    points = entry_points(data)
    if not points:
        return b""
    for offset in points:
        if len(data) - offset <= wanted:
            return data[offset:]
    return data[points[-1] :]
