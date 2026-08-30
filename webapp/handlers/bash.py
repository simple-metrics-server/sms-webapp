from typing import ClassVar

from webapp.core.base import MetricHandler
from webapp.core.model import Metric, SampleValue


class BashMetricHandler(MetricHandler):
    """Handler that collects metrics by executing shell commands.

    The config source is a JSON file containing an array of metric
    objects. Each command is executed without a shell and must print a
    single numeric value to stdout.
    """

    required_fields: ClassVar[list[str]] = ["cmd"]

    async def execute(self, metrics: list[Metric]) -> dict[str, SampleValue]:
        return await self.support.run_cmds(metrics)
