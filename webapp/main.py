import asyncio
import logging
import os
import sys
from pathlib import Path

from quart import Quart, Response

from webapp.core import Runtime

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 9555
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_DIR = DATA_DIR / "config"
CACHE_PATH = DATA_DIR / "metrics.cache"
SCRAPE_INTERVAL = float(os.environ.get("SCRAPE_INTERVAL", "60"))
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = Quart(__name__)
runtime = Runtime(config_dir=str(CONFIG_DIR), cache_path=str(CACHE_PATH))


@app.before_request
async def count_request() -> None:
    """Count every incoming HTTP request (builtin.runtime.requests)."""
    runtime.request_count += 1


@app.before_serving
async def startup() -> None:
    """Run the first scrape, then start the background scrape loop."""
    await runtime.scrape_once()
    asyncio.create_task(runtime.serve(SCRAPE_INTERVAL))


@app.get("/metrics")
async def metrics() -> Response:
    """Serve the latest scrape result in Prometheus exposition format."""
    try:
        out = await runtime.render()
    except FileNotFoundError:
        return Response(
            "no metrics available yet\n",
            status=503,
            content_type=CONTENT_TYPE,
        )
    return Response(out, content_type=CONTENT_TYPE)


def run_app(
    host: str,
    port: int,
    certfile: str | None = None,
    keyfile: str | None = None,
) -> None:
    """Run Quart; monkeypatched in tests, called by the CLI."""
    app.run(
        host=host,
        port=port,
        certfile=certfile,
        keyfile=keyfile,
        use_reloader=False,
    )


if __name__ == "__main__":
    # Alias this module so cli's `from webapp.main import run_app`
    # does not execute webapp/main.py a second time.
    sys.modules.setdefault("webapp.main", sys.modules[__name__])
    from webapp.cli import main

    main()
