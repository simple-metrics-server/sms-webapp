import importlib
import inspect
import logging
import pkgutil
from types import ModuleType

from webapp.core.base import MetricHandler

log = logging.getLogger(__name__)


def load_handlers(package: str) -> dict[str, type[MetricHandler]]:
    """Discover handlers by importing every module in package.

    Each module must define exactly one MetricHandler subclass. The
    registry key is the module name, so dropping e.g. bash.py into the
    handlers folder registers the "bash" handler type; removing the
    file removes the handler.
    """
    pkg = importlib.import_module(package)
    registry: dict[str, type[MetricHandler]] = {}
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{package}.{info.name}")
        htype = _find_handler(module)
        if htype is None:
            log.warning(
                "no MetricHandler subclass in %s, ignoring", module.__name__
            )
            continue
        registry[info.name] = htype
        log.info("registered handler %r from %s", info.name, module.__name__)
    return registry


def _find_handler(module: ModuleType) -> type[MetricHandler] | None:
    found = [
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, MetricHandler)
        and obj is not MetricHandler
        and obj.__module__ == module.__name__
    ]
    if len(found) > 1:
        raise ValueError(
            f"module {module.__name__} defines multiple handlers; expected exactly one"
        )
    return found[0] if found else None
