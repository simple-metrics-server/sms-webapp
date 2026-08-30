import asyncio
import json
import os
from abc import ABC, abstractmethod
from typing import ClassVar

from webapp.core.helpers import HandlerSupport
from webapp.core.model import Metric, SampleValue


class MetricHandler(ABC):
    """Interface for metric handlers.

    A handler owns a config source and moves through four phases:
    read() parses the source into metric definitions, verify() validates
    them, execute() collects the metric values and finalize() renders
    them into Prometheus exposition format.

    The runtime runs all four phases per scrape cycle, so config changes
    are picked up on the next cycle. Phases are stateless: data flows
    through parameters and return values, the handler instance only
    holds its config path and helpers.

    read(), verify() and finalize() have concrete default
    implementations built on HandlerSupport; a handler declares its
    required command fields in required_fields and only implements
    execute(). Override any phase if a handler needs custom behavior.
    """

    required_fields: ClassVar[list[str]] = []

    def __init__(self, config_path: str) -> None:
        self.config_path = config_path
        self.support = HandlerSupport()

    async def read(self) -> list[Metric]:
        """Parse the JSON config file into metric definitions."""
        return await asyncio.to_thread(self._parse)

    def _parse(self) -> list[Metric]:
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"Config not found: {self.config_path}")
        with open(self.config_path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if not isinstance(obj, list):
            raise TypeError(f"Config must be a JSON array: {self.config_path}")
        try:
            return [Metric(**m) for m in obj]
        except TypeError as e:
            raise ValueError(f"Invalid metric definition: {e}") from e

    async def verify(self, metrics: list[Metric]) -> None:
        """Validate the parsed config. Raise on any problem."""
        self.support.verify_metrics(
            metrics, self.required_fields, self.config_path
        )

    @abstractmethod
    async def execute(self, metrics: list[Metric]) -> dict[str, SampleValue]:
        """Collect all metric values. Raise on any failure."""

    async def finalize(
        self,
        metrics: list[Metric],
        results: dict[str, SampleValue],
    ) -> str:
        """Render collected values into Prometheus exposition format."""
        missing = [m.name for m in metrics if m.name not in results]
        if missing:
            raise ValueError(f"No results for metrics: {missing}")
        return self.support.render_exposition(metrics, results)
