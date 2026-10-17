from __future__ import annotations

import fcntl
import functools
from contextlib import contextmanager
from typing import Any, Callable


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


def state_transaction(method: Callable) -> Callable:
    @functools.wraps(method)
    def transaction(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._state_lock():
            return method(self, *args, **kwargs)

    return transaction
