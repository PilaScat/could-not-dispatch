from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .constants import EPG_RETRY_SECONDS, IN_USE_EMPTY_TEXT, IN_USE_SEPARATOR
from .recovery import ApiError, InUse, Program


def _same(first: str, second: str) -> bool:
    return " ".join(first.split()).casefold() == " ".join(second.split()).casefold()


def line(name: str, program: Program | None) -> str:
    if program is None or _same(name, program.title):
        return name
    return f"{name}{IN_USE_SEPARATOR}{program.title}"


class InUseFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._written: str | None = None

    def write(self, lines: Sequence[str]) -> bool:
        text = "\n".join(lines) or IN_USE_EMPTY_TEXT
        if text == self._written:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_name(f"{self.path.name}.part")
        partial.write_text(text, encoding="utf-8")
        os.replace(partial, self.path)
        self._written = text
        return True


@dataclass(frozen=True)
class _Known:
    program: Program | None
    until: float


class InUseCaption:
    def __init__(
        self,
        file: InUseFile,
        programs: Callable[[Sequence[str]], dict[str, Program]] | None,
        log: Callable[[str], None],
        clock: Callable[[], float] = time.time,
        retry_seconds: float = EPG_RETRY_SECONDS,
    ) -> None:
        self._file = file
        self._programs = programs
        self._log = log
        self._clock = clock
        self._retry_seconds = retry_seconds
        self._known: dict[str, _Known] = {}
        self._failed_at: float | None = None
        self._file.write([])

    def update(self, channels: Sequence[InUse]) -> None:
        now = self._clock()
        uuids = [channel.uuid for channel in channels]
        self._known = {uuid: known for uuid, known in self._known.items() if uuid in uuids}
        self._look_up(uuids, now)
        self._file.write(
            [line(channel.name, self._program(channel.uuid, now)) for channel in channels]
        )

    def _program(self, uuid: str, now: float) -> Program | None:
        known = self._known.get(uuid)
        return known.program if known is not None and now < known.until else None

    def _look_up(self, uuids: list[str], now: float) -> None:
        missing = [
            uuid for uuid in uuids if uuid not in self._known or now >= self._known[uuid].until
        ]
        if self._programs is None or not missing:
            return
        if self._failed_at is not None and now - self._failed_at < self._retry_seconds:
            return
        try:
            found = self._programs(missing)
        except ApiError as error:
            if self._failed_at is None:
                self._log(f"the programme guide is unavailable, showing names only: {error}")
            self._failed_at = now
            return
        self._failed_at = None
        for uuid in missing:
            program = found.get(uuid)
            if program is not None and program.ends_at > now:
                self._known[uuid] = _Known(program, program.ends_at)
            else:
                self._known[uuid] = _Known(None, now + self._retry_seconds)
