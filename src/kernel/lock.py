from __future__ import annotations

import fcntl
from contextlib import contextmanager


class StateLockMixin:
    @contextmanager
    def _state_lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / "state.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
