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
        if proc.returncode != 0:
            raise RuntimeError(
                f"exited with {proc.returncode}: {' '.join(argv)!r}: "
                f"{stderr.decode('utf-8', 'replace').strip()[:MAX_STDERR]}"
            )
        return stdout.decode("utf-8")

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
