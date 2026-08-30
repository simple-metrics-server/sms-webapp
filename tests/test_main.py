import asyncio
import json

import pytest
from quart import Quart, Response

from webapp.core import Runtime
from webapp.main import CONTENT_TYPE, count_request
from webapp.main import runtime as main_runtime


def metric(name, cmd="echo 1"):
    return {
        "name": name,
        "help_text": f"help for {name}",
        "value_type": "gauge",
        "cmd": cmd,
        "timeout": 5,
    }


def make_app(tmp_path, metrics):
    """Build a Quart app wired like main.py, with tmp config and cache."""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "bash.json").write_text(json.dumps(metrics))
    cache = tmp_path / "metrics.cache"
    runtime = Runtime(config_dir=str(cfg_dir), cache_path=str(cache))

    app = Quart(__name__)

    @app.get("/metrics")
    async def metrics_endpoint():
        try:
            out = await runtime.render()
        except FileNotFoundError:
            return Response(
                "no metrics available yet\n",
                status=503,
                content_type=CONTENT_TYPE,
            )
        return Response(out, content_type=CONTENT_TYPE)

    return app, runtime


def test_count_request_increments_runtime_counter():
    before = main_runtime.request_count
    asyncio.run(count_request())
    asyncio.run(count_request())
    assert main_runtime.request_count == before + 2


@pytest.mark.asyncio
async def test_metrics_endpoint_serves_cached_scrape(tmp_path):
    app, runtime = make_app(tmp_path, [metric("m_test")])
    # what before_serving does in main.py
    await runtime.scrape_once()
    async with app.test_client() as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert resp.content_type == CONTENT_TYPE
    body = await resp.get_data(as_text=True)
    assert "m_test 1.0" in body


@pytest.mark.asyncio
async def test_metrics_endpoint_503_before_first_scrape(tmp_path):
    app, _ = make_app(tmp_path, [metric("m_test")])
    async with app.test_client() as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 503
    body = await resp.get_data(as_text=True)
    assert "no metrics available" in body


@pytest.mark.asyncio
async def test_metrics_endpoint_keeps_last_good_on_failure(tmp_path):
    app, runtime = make_app(tmp_path, [metric("m_test")])
    await runtime.scrape_once()
    # break the config; the failed cycle must not touch the cache
    (tmp_path / "cfg" / "bash.json").write_text(
        json.dumps([metric("m_test", cmd="false")])
    )
    with pytest.raises(ExceptionGroup):
        await runtime.scrape_once()
    async with app.test_client() as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "m_test 1.0" in await resp.get_data(as_text=True)
