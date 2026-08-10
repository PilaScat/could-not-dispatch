from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeProcess:
    def __init__(self, chunks: tuple[bytes, ...] = (), keep_open: bool = True) -> None:
        read_fd, write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self.stderr = io.BytesIO(b"")
        self._writer = os.fdopen(write_fd, "wb", buffering=0)
        self._returncode: int | None = None
        for chunk in chunks:
            self.feed(chunk)
        if not keep_open:
            self.close_writer()

    def feed(self, data: bytes) -> None:
        with contextlib.suppress(OSError, ValueError):
            self._writer.write(data)

    def close_writer(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            self._writer.close()

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self._returncode = -15
        self.close_writer()

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout: float | None = None) -> int | None:
        return self._returncode
