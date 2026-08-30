import sys
import textwrap

import pytest

from webapp.core.loader import load_handlers
from webapp.handlers.bash import BashMetricHandler


def test_discovers_bash_handler():
    registry = load_handlers("webapp.handlers")
    assert registry["bash"] is BashMetricHandler


def test_unknown_package_raises():
    with pytest.raises(ModuleNotFoundError):
        load_handlers("webapp.does_not_exist")


@pytest.fixture
def fake_package(tmp_path):
    """Create an importable fake handler package, cleaned up after use."""
    created = []

    def make(modules: dict[str, str]) -> str:
        pkg = tmp_path / "fakehandlers"
        pkg.mkdir(exist_ok=True)
        (pkg / "__init__.py").write_text("")
        for name, src in modules.items():
            (pkg / f"{name}.py").write_text(textwrap.dedent(src))
            created.append(f"fakehandlers.{name}")
        created.append("fakehandlers")
        sys.path.insert(0, str(tmp_path))
        return "fakehandlers"

    yield make

    sys.path.remove(str(tmp_path))
    for mod in created:
        sys.modules.pop(mod, None)


def test_module_without_handler_is_skipped(fake_package, caplog):
    package = fake_package({"empty_mod": "X = 1\n"})
    with caplog.at_level("WARNING"):
        registry = load_handlers(package)
    assert registry == {}
    assert "no MetricHandler subclass" in caplog.text


def test_module_with_two_handlers_raises(fake_package):
    package = fake_package(
        {
            "two": """
                from webapp.core.base import MetricHandler

                class FirstHandler(MetricHandler):
                    async def execute(self, metrics):
                        return {}

                class SecondHandler(MetricHandler):
                    async def execute(self, metrics):
                        return {}
            """
        }
    )
    with pytest.raises(ValueError, match="multiple handlers"):
        load_handlers(package)
