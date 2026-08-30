import asyncio
import logging
import os
from pathlib import Path

from webapp.core.base import MetricHandler
from webapp.core.helpers import series_names
from webapp.core.loader import load_handlers
from webapp.core.model import Metric
from webapp.core.timing import HandlerTimings, ScrapeTimings, measure

log = logging.getLogger(__name__)


class Runtime:
    """Orchestrates dynamically discovered metric handlers.

    Handler types are discovered in the handlers package (one handler
    class per module, registry key = module name). A handler is only
    activated if a matching config <name>.json exists in config_dir.

    scrape_once() runs one scrape cycle over all handlers and writes
    the exposition atomically to the cache file; on any failure the
    previous cache file is kept. On success it returns and stores the
    timings of the cycle (last_timings) and increments scrape_count.
    serve() repeats scrape cycles on a fixed interval; a cycle never
    runs longer than the interval. render() returns the content of the
    cache file. request_count is left to the HTTP layer to increment
    (see webapp.main).
    """

    def __init__(
        self,
        config_dir: str,
        cache_path: str,
        handlers_package: str = "webapp.handlers",
    ) -> None:
        registry = load_handlers(handlers_package)
        self.cache_path = Path(cache_path)
        self.last_timings: ScrapeTimings | None = None
        self.request_count = 0
        self.scrape_count = 0
        self.handlers: list[MetricHandler] = []
        for name, htype in sorted(registry.items()):
            cfg = Path(config_dir) / f"{name}.json"
            if cfg.exists():
                log.info("activating handler %r with %s", name, cfg)
                handler = htype(str(cfg))
                binder = getattr(handler, "bind_runtime", None)
                if callable(binder):
                    binder(self)
                self.handlers.append(handler)
            else:
                log.info("handler %r has no config %s, skipping", name, cfg)

    async def scrape_once(self) -> ScrapeTimings:
        """Run one scrape cycle, write the cache file atomically.

        Raises ExceptionGroup if any handler failed; the previous cache
        file and last_timings are kept in that case. On success returns
        the timings of the cycle.
        """
        timings = ScrapeTimings()
        with measure(lambda d: setattr(timings, "total", d)):
            prepared = await self._read_and_verify_all()
            self._check_duplicate_names({h: ms for h, ms, _ in prepared})
            timings.per_handler = {type(h).__name__: t for h, _, t in prepared}

            async def collect(
                handler: MetricHandler, ms: list[Metric], t: HandlerTimings
            ) -> str:
                with measure(lambda d: setattr(t, "execute", d)):
                    results = await handler.execute(ms)
                with measure(lambda d: setattr(t, "finalize", d)):
                    return await handler.finalize(ms, results)

            results = await asyncio.gather(
                *(collect(h, ms, t) for h, ms, t in prepared),
                return_exceptions=True,
            )
            errors = [r for r in results if isinstance(r, Exception)]
            if errors:
                raise ExceptionGroup("scrape failed", errors)
            blocks = [r.strip() for r in results if isinstance(r, str)]
            out = "\n\n".join(b for b in blocks if b)
            if out:
                out += "\n"
            await asyncio.to_thread(self._write_cache, out)
        self.last_timings = timings
        self.scrape_count += 1
        return timings

    async def _read_and_verify_all(
        self,
    ) -> list[tuple[MetricHandler, list[Metric], HandlerTimings]]:
        async def prepare(
            handler: MetricHandler,
        ) -> tuple[MetricHandler, list[Metric], HandlerTimings]:
            t = HandlerTimings()
            with measure(lambda d: setattr(t, "read", d)):
                metrics = await handler.read()
            with measure(lambda d: setattr(t, "verify", d)):
                await handler.verify(metrics)
            return handler, metrics, t

        results = await asyncio.gather(
            *(prepare(h) for h in self.handlers), return_exceptions=True
        )
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            raise ExceptionGroup("config verification failed", errors)
        return [
            (h, ms, t) for h, ms, t in results if isinstance(h, MetricHandler)
        ]

    def _check_duplicate_names(
        self, metrics: dict[MetricHandler, list[Metric]]
    ) -> None:
        seen: dict[str, str] = {}
        errors: list[str] = []
        for handler, ms in metrics.items():
            for m in ms:
                owner = type(handler).__name__
                for s in series_names(m):
                    if s in seen:
                        errors.append(
                            f"series {s!r} exported by both {seen[s]} and {owner}"
                        )
                    else:
                        seen[s] = owner
        if errors:
            msg = "Duplicate metric names across handlers:\n" + "\n".join(
                f"  - {e}" for e in errors
            )
            log.error(msg)
            raise ValueError(msg)

    def _write_cache(self, content: str) -> None:
        tmp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, self.cache_path)

    async def serve(self, interval: float) -> None:
        """Repeat scrape cycles on a fixed interval forever."""
        while True:
            try:
                await asyncio.wait_for(self.scrape_once(), timeout=interval)
            except ExceptionGroup as eg:
                log.error("scrape cycle failed: %s", eg)
            except TimeoutError:
                log.error("scrape cycle exceeded interval of %ss", interval)
            await asyncio.sleep(interval)

    async def render(self) -> str:
        """Return the content of the cache file."""
        return await asyncio.to_thread(
            self.cache_path.read_text, encoding="utf-8"
        )
