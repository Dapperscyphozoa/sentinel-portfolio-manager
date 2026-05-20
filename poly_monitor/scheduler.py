"""Trivial in-process scheduler for poly-monitor routines.

Each registered task runs in its own thread on a fixed interval. Errors
are caught + logged; one routine crashing doesn't kill the others.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable


log = logging.getLogger("poly_monitor.scheduler")


class Scheduler:
    def __init__(self) -> None:
        self._tasks: list[threading.Thread] = []
        self._stop = threading.Event()

    def every(self, interval_s: int, name: str, fn: Callable[[], None]) -> None:
        def runner() -> None:
            # Stagger initial fire so routines don't all hit the DB at t=0
            time.sleep(random.uniform(0, min(interval_s, 30)))
            while not self._stop.is_set():
                try:
                    fn()
                except Exception as e:
                    log.exception(f"{name} failed: {e}")
                self._stop.wait(interval_s)

        t = threading.Thread(target=runner, name=f"sched-{name}", daemon=True)
        t.start()
        self._tasks.append(t)
        log.info(f"scheduled {name} every {interval_s}s")

    def run_forever(self) -> None:
        try:
            while not self._stop.is_set():
                time.sleep(60)
        except KeyboardInterrupt:
            self._stop.set()
