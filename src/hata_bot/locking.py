from __future__ import annotations

import os
from pathlib import Path
from typing import TextIO

from hata_bot.exceptions import SingleInstanceError

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class SingleInstanceLock:
    def __init__(self, lock_file: Path) -> None:
        self.lock_file = lock_file
        self.handle: TextIO | None = None

    def __enter__(self) -> "SingleInstanceLock":
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.lock_file.open("a+", encoding="utf-8")
        self.handle.seek(0)
        self.handle.truncate(0)
        self.handle.write(str(os.getpid()))
        self.handle.flush()

        try:
            if os.name == "nt":
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise SingleInstanceError(f"Another HataBot run is already active: {self.lock_file}") from exc

        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if not self.handle:
            return
        try:
            if os.name == "nt":
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None

