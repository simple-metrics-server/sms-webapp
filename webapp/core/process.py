import asyncio
import logging
import os
import signal

log = logging.getLogger(__name__)

MAX_STDERR = 500


class ProcessRunner:
    """Generic async subprocess execution.

    Commands run without a shell, each in its own process group.
    run() returns the decoded stdout of a single command or raises;
    run_many() executes commands concurrently, waits for all of them and
    raises an ExceptionGroup containing every failure if any command
    failed. Timed-out or cancelled commands are killed including their
    whole process group, so no children or grandchildren survive.
    """

    async def run(self, argv: list[str], timeout: float) -> str:
        """Execute one command, return stdout. Raise on any failure."""
        code, stdout, stderr = await self._execute(argv, timeout)
        if code != 0:
            raise RuntimeError(
                f"exited with {code}: {' '.join(argv)!r}: {stderr[:MAX_STDERR]}"
            )
        return stdout

    async def run_status(
        self, argv: list[str], timeout: float
    ) -> tuple[int, str]:
        """Execute one command, return (exit code, stdout).

        Unlike run(), a non-zero exit code is not an error (some probes
        use it to signal "updates available" / "reboot required").
        Timeouts and cancellations still raise.
        """
        code, stdout, _ = await self._execute(argv, timeout)
        return code, stdout

    async def _execute(
        self, argv: list[str], timeout: float
    ) -> tuple[int, str, str]:
        """Run to completion; return (exit code, stdout, stderr)."""
        if not argv:
            raise ValueError("cannot execute an empty command")
        log.debug("executing: %s", argv)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except TimeoutError:
            await self._reap(proc)
            raise RuntimeError(
                f"timed out after {timeout}s: {' '.join(argv)!r}"
            ) from None
        except asyncio.CancelledError:
            await self._reap(proc)
            raise
        return (
            proc.returncode if proc.returncode is not None else -1,
            stdout.decode("utf-8"),
            stderr.decode("utf-8", "replace").strip(),
        )

    async def _reap(self, proc: asyncio.subprocess.Process) -> None:
        """Kill the whole process group and wait for the child to die."""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        await proc.wait()

    async def run_many(
        self, cmds: dict[str, tuple[list[str], float]]
    ) -> dict[str, str]:
        """Execute all commands concurrently, return stdout per key.

        All commands run to completion; if any of them failed, an
        ExceptionGroup with every failure is raised and no results are
        returned.
        """
        results = await asyncio.gather(
            *(self.run(argv, timeout) for argv, timeout in cmds.values()),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            raise ExceptionGroup("process execution failed", errors)
        return dict(zip(cmds.keys(), results))
