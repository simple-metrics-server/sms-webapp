import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class HandlerTimings:
    """Duration of each phase of one handler, in seconds."""

    read: float = 0.0
    verify: float = 0.0
    execute: float = 0.0
    finalize: float = 0.0

    @property
    def total(self) -> float:
        return self.read + self.verify + self.execute + self.finalize


@dataclass
class ScrapeTimings:
    """Timings of one scrape cycle: per handler plus total wall time."""

    per_handler: dict[str, HandlerTimings] = field(default_factory=dict)
    total: float = 0.0


@contextmanager
def measure(setter: Callable[[float], None]) -> Generator[None]:
    """Measure the duration of the wrapped block, report it to setter."""
    start = time.perf_counter()
    yield
    setter(time.perf_counter() - start)
