import logging
from typing import ClassVar

from webapp.core.base import MetricHandler
from webapp.core.builtin import (
    collect,
    is_known_command,
    parse_handler_cmd,
)
from webapp.core.model import Metric, SampleValue

log = logging.getLogger(__name__)


class BuiltinMetricHandler(MetricHandler):
    """Handler that reports runtime and OS state via core.builtin.

    The config is a JSON array of metric definitions like any other
    handler, but `cmd` names a builtin provider (see
    webapp.core.builtin) rather than a shell command. verify() checks
    each cmd against the builtin registry; execute() delegates to the
    shared collectors in webapp.core.builtin.
    """

    required_fields: ClassVar[list[str]] = ["cmd"]

    def __init__(self, config_path: str) -> None:
        super().__init__(config_path)
        self._runtime = None

    def bind_runtime(self, runtime) -> None:
        """Receive the Runtime so providers can read its state."""
        self._runtime = runtime

    async def verify(self, metrics: list[Metric]) -> None:
        await super().verify(metrics)
        known = self._known_handlers()
        errors: list[str] = []
        for m in metrics:
            if not is_known_command(m.cmd):
                errors.append(
                    f"metric {m.name!r}: unknown builtin command {m.cmd!r}"
                )
                continue
            parsed = parse_handler_cmd(m.cmd)
            if parsed is not None and known is not None:
                handler, _ = parsed
                if handler not in known:
                    errors.append(
                        f"metric {m.name!r}: no such handler {handler!r}"
                    )
            if m.value_type in ("histogram", "summary"):
                errors.append(
                    f"metric {m.name!r}: builtin commands require a scalar "
                    f"value_type, got {m.value_type!r}"
                )
        if errors:
            msg = f"Invalid config {self.config_path}:\n" + "\n".join(
                f"  - {e}" for e in errors
            )
            log.error(msg)
            raise ValueError(msg)

    async def execute(self, metrics: list[Metric]) -> dict[str, SampleValue]:
        return await collect(metrics, self.support.runner, self._runtime)

    def _known_handlers(self) -> set[str] | None:
        """Class names of active handlers, or None if runtime not bound."""
        if self._runtime is None:
            return None
        handlers = getattr(self._runtime, "handlers", None)
        if handlers is None:
            return None
        return {type(h).__name__ for h in handlers}
