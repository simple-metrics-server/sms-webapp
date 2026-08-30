import asyncio
import subprocess
import time

import pytest

from webapp.core.process import ProcessRunner


def count_sleep_processes() -> int:
    out = subprocess.run(
        ["pgrep", "-f", "sleep 5"], capture_output=True, text=True, check=False
    )
    return len([l for l in out.stdout.strip().splitlines() if l])


def test_run_returns_stdout():
    out = asyncio.run(ProcessRunner().run(["echo", "hello"], timeout=5))
    assert out == "hello\n"


def test_run_rejects_empty_command():
    with pytest.raises(ValueError, match="empty command"):
        asyncio.run(ProcessRunner().run([], timeout=5))


def test_run_raises_on_nonzero_exit():
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(ProcessRunner().run(["false"], timeout=5))
    assert "exited with 1" in str(excinfo.value)


def test_run_includes_stderr_in_error():
    with pytest.raises(RuntimeError, match="some error"):
        asyncio.run(
            ProcessRunner().run(
                ["sh", "-c", "echo some error >&2; exit 3"], timeout=5
            )
        )


def test_run_caps_stderr_in_error():
    cmd = ["sh", "-c", "head -c 2000 /dev/zero | tr '\\0' 'x' >&2; exit 1"]
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(ProcessRunner().run(cmd, timeout=5))
    assert "x" * 500 in str(excinfo.value)
    assert "x" * 501 not in str(excinfo.value)


def test_run_raises_on_timeout_and_reaps_child():
    before = count_sleep_processes()
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        asyncio.run(ProcessRunner().run(["sleep", "5"], timeout=1))
    assert time.monotonic() - start < 4
    time.sleep(0.1)
    assert count_sleep_processes() == before


def test_run_reaps_child_on_cancellation():
    before = count_sleep_processes()

    async def main():
        task = asyncio.create_task(
            ProcessRunner().run(["sleep", "5"], timeout=30)
        )
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    time.sleep(0.1)
    assert count_sleep_processes() == before


def test_run_kills_grandchildren_on_timeout():
    # the shell spawns a child sleep; killing only the direct child
    # would leave the grandchild running
    cmd = ["sh", "-c", "sleep 5 & wait"]
    before = count_sleep_processes()
    with pytest.raises(RuntimeError, match="timed out"):
        asyncio.run(ProcessRunner().run(cmd, timeout=1))
    time.sleep(0.1)
    assert count_sleep_processes() == before


def test_run_many_collects_results():
    cmds = {
        "a": (["echo", "1"], 5),
        "b": (["echo", "2"], 5),
    }
    out = asyncio.run(ProcessRunner().run_many(cmds))
    assert out == {"a": "1\n", "b": "2\n"}


def test_run_many_runs_in_parallel():
    cmds = {f"m{i}": (["sleep", "0.3"], 5) for i in range(3)}
    cmds["fast"] = (["echo", "ok"], 5)
    start = time.monotonic()
    out = asyncio.run(ProcessRunner().run_many(cmds))
    elapsed = time.monotonic() - start
    # 3 x 0.3s sequential would take >= 0.9s
    assert elapsed < 0.8, f"not parallel: took {elapsed:.2f}s"
    assert out["fast"] == "ok\n"


def test_run_many_raises_group_with_all_failures():
    cmds = {
        "bad1": (["false"], 5),
        "good": (["echo", "ok"], 5),
        "bad2": (["sh", "-c", "exit 2"], 5),
    }
    with pytest.raises(ExceptionGroup) as excinfo:
        asyncio.run(ProcessRunner().run_many(cmds))
    assert len(excinfo.value.exceptions) == 2
