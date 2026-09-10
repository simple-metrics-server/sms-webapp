import asyncio
import time

import pytest

from webapp.core.helpers import HandlerSupport
from webapp.core.model import (
    DistributionSample,
    LabeledSample,
    Metric,
    MultiLabeledSample,
)


def metric(**kw):
    base = {
        "name": "test_metric",
        "help_text": "A test metric",
        "value_type": "gauge",
        "cmd": "echo 42",
        "timeout": 5,
    }
    base.update(kw)
    return Metric(**base)


# --- verify_metrics ---


def test_verify_accepts_valid_metrics():
    HandlerSupport().verify_metrics([metric()], ["cmd"], "test")


@pytest.mark.parametrize("name", ["1bad", "has-dash", "has space", ""])
def test_verify_rejects_bad_names(name):
    with pytest.raises(ValueError, match="invalid metric name"):
        HandlerSupport().verify_metrics([metric(name=name)], ["cmd"], "test")


def test_verify_rejects_bad_type():
    with pytest.raises(ValueError, match="invalid value_type"):
        HandlerSupport().verify_metrics(
            [metric(value_type="nope")], ["cmd"], "test"
        )


def test_verify_rejects_duplicate_names():
    with pytest.raises(ValueError, match="duplicate series"):
        HandlerSupport().verify_metrics([metric(), metric()], ["cmd"], "test")


def test_verify_rejects_missing_and_unparseable_required_field():
    with pytest.raises(ValueError) as excinfo:
        HandlerSupport().verify_metrics(
            [metric(name="m_empty", cmd=""), metric(name="m_bad", cmd="'x")],
            ["cmd"],
            "test",
        )
    assert "m_empty" in str(excinfo.value)
    assert "m_bad" in str(excinfo.value)


def test_verify_rejects_nonpositive_timeout():
    with pytest.raises(ValueError, match="timeout"):
        HandlerSupport().verify_metrics([metric(timeout=0)], ["cmd"], "test")


def test_verify_rejects_nonnumeric_timeout():
    with pytest.raises(ValueError, match="timeout must be a number"):
        HandlerSupport().verify_metrics([metric(timeout="5")], ["cmd"], "test")


def test_verify_collects_all_errors():
    with pytest.raises(ValueError) as excinfo:
        HandlerSupport().verify_metrics(
            [metric(name="1bad", value_type="nope", cmd="", timeout=0)],
            ["cmd"],
            "test",
        )
    msg = str(excinfo.value)
    assert "invalid metric name" in msg
    assert "invalid value_type" in msg
    assert "empty or missing" in msg
    assert "timeout" in msg


def test_verify_skips_required_fields_when_not_declared():
    HandlerSupport().verify_metrics([metric(cmd="")], [], "test")


# --- run_cmds ---


def test_run_cmds_returns_canonical_values():
    out = asyncio.run(
        HandlerSupport().run_cmds(
            [metric(cmd="echo 1"), metric(name="m2", cmd="echo 2.5")]
        )
    )
    assert out == {"test_metric": "1.0", "m2": "2.5"}


@pytest.mark.parametrize("cmd", ["echo abc", "printf '1\\n2\\n'"])
def test_run_cmds_rejects_bad_output(cmd):
    with pytest.raises(ExceptionGroup):
        asyncio.run(HandlerSupport().run_cmds([metric(cmd=cmd)]))


def test_run_cmds_raises_on_command_failure():
    with pytest.raises(ExceptionGroup) as excinfo:
        asyncio.run(HandlerSupport().run_cmds([metric(cmd="false")]))
    assert any("exited with 1" in str(e) for e in excinfo.value.exceptions)


def test_run_cmds_runs_in_parallel():
    metrics = [metric(name=f"m{i}", cmd="sleep 0.3") for i in range(3)]
    metrics.append(metric(name="m_fast", cmd="echo 7"))
    start = time.monotonic()
    with pytest.raises(ExceptionGroup):
        asyncio.run(HandlerSupport().run_cmds(metrics))
    elapsed = time.monotonic() - start
    assert elapsed < 0.8, f"not parallel: took {elapsed:.2f}s"


# --- parse_value ---


@pytest.mark.parametrize(
    "raw,expected",
    [("42\n", "42.0"), ("3.5", "3.5"), ("-1", "-1.0"), ("1e3", "1000.0")],
)
def test_parse_value_canonicalizes(raw, expected):
    assert HandlerSupport().parse_value("m", raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "  ", "abc", "abc def", "nan", "inf", "-inf", "1_0", "infinity"]
)
def test_parse_value_rejects_invalid(raw):
    with pytest.raises(ValueError):
        HandlerSupport().parse_value("m", raw)


# --- render_exposition ---


def test_render_exposition_format():
    metrics = [
        metric(
            name="m_a", help_text="Help A", value_type="counter", cmd="echo 1"
        ),
        metric(name="m_b", help_text="Help B", cmd="echo 2"),
    ]
    results = {"m_a": "1.0", "m_b": "2.0"}
    out = HandlerSupport().render_exposition(metrics, results)
    assert out == (
        "# HELP m_a Help A\n"
        "# TYPE m_a counter\n"
        "m_a 1.0\n"
        "\n"
        "# HELP m_b Help B\n"
        "# TYPE m_b gauge\n"
        "m_b 2.0\n"
    )


def test_render_exposition_escapes_help():
    out = HandlerSupport().render_exposition(
        [metric(help_text="back\\slash")], {"test_metric": "1.0"}
    )
    assert "# HELP test_metric back\\\\slash" in out


def test_render_exposition_empty():
    assert HandlerSupport().render_exposition([], {}) == ""


# --- verify_metrics: histograms and summaries ---


def hist(**kw):
    base = {
        "name": "test_hist",
        "help_text": "A histogram",
        "value_type": "histogram",
        "buckets": [0.1, 0.5, 1.0],
        "cmd": "echo 1",
        "timeout": 5,
    }
    base.update(kw)
    return Metric(**base)


def summ(**kw):
    base = {
        "name": "test_summary",
        "help_text": "A summary",
        "value_type": "summary",
        "quantiles": [0.5, 0.99],
        "cmd": "echo 1",
        "timeout": 5,
    }
    base.update(kw)
    return Metric(**base)


def test_verify_accepts_histogram():
    HandlerSupport().verify_metrics([hist()], ["cmd"], "test")


def test_verify_accepts_summary():
    HandlerSupport().verify_metrics([summ()], ["cmd"], "test")


def test_verify_rejects_histogram_without_buckets():
    with pytest.raises(ValueError, match="buckets"):
        HandlerSupport().verify_metrics([hist(buckets=None)], ["cmd"], "test")


def test_verify_rejects_non_ascending_buckets():
    with pytest.raises(ValueError, match="strictly ascending"):
        HandlerSupport().verify_metrics(
            [hist(buckets=[1.0, 0.5])], ["cmd"], "test"
        )


def test_verify_rejects_nonfinite_buckets():
    with pytest.raises(ValueError, match="finite"):
        HandlerSupport().verify_metrics(
            [hist(buckets=[0.1, float("inf")])], ["cmd"], "test"
        )


def test_verify_rejects_buckets_on_non_histogram():
    with pytest.raises(
        ValueError, match="only valid for value_type 'histogram'"
    ):
        HandlerSupport().verify_metrics(
            [metric(buckets=[0.1])], ["cmd"], "test"
        )


def test_verify_rejects_quantiles_out_of_range():
    with pytest.raises(ValueError, match="must be > 0.0"):
        HandlerSupport().verify_metrics(
            [summ(quantiles=[0.0, 0.5])], ["cmd"], "test"
        )
    with pytest.raises(ValueError, match="must be < 1.0"):
        HandlerSupport().verify_metrics(
            [summ(quantiles=[0.5, 1.0])], ["cmd"], "test"
        )


def test_verify_rejects_quantiles_on_non_summary():
    with pytest.raises(ValueError, match="only valid for value_type 'summary'"):
        HandlerSupport().verify_metrics(
            [metric(quantiles=[0.5])], ["cmd"], "test"
        )


def test_verify_rejects_series_collision():
    with pytest.raises(ValueError, match="duplicate series"):
        HandlerSupport().verify_metrics(
            [hist(name="foo"), metric(name="foo_count")], ["cmd"], "test"
        )


# --- parse_distribution ---


def test_parse_distribution_histogram():
    out = HandlerSupport().parse_distribution(
        hist(), "0.1 1\n0.5 3\n1 3\nsum 0.97\ncount 3\n"
    )
    assert out.values == {0.1: "1.0", 0.5: "3.0", 1.0: "3.0"}
    assert out.sum_value == "0.97"
    assert out.count_value == "3.0"


def test_parse_distribution_matches_keys_numerically():
    out = HandlerSupport().parse_distribution(
        hist(), "0.10 1\n0.5 3\n1 3\nsum 0.97\ncount 3\n"
    )
    assert out.values[0.1] == "1.0"


def test_parse_distribution_summary():
    out = HandlerSupport().parse_distribution(
        summ(), "0.5 0.25\n0.99 0.9\nsum 1.2\ncount 3\n"
    )
    assert out.values == {0.5: "0.25", 0.99: "0.9"}
    assert out.sum_value == "1.2"
    assert out.count_value == "3.0"


def test_parse_distribution_rejects_missing_key():
    with pytest.raises(ValueError, match="missing"):
        HandlerSupport().parse_distribution(
            hist(), "0.1 1\n0.5 3\nsum 0.97\ncount 3\n"
        )


def test_parse_distribution_rejects_unknown_key():
    with pytest.raises(ValueError, match="not declared"):
        HandlerSupport().parse_distribution(
            hist(), "0.1 1\n0.5 3\n1 3\n2 4\nsum 0.97\ncount 4\n"
        )


def test_parse_distribution_rejects_duplicate_key():
    with pytest.raises(ValueError, match="duplicate"):
        HandlerSupport().parse_distribution(
            hist(), "0.1 1\n0.1 2\n0.5 3\n1 3\nsum 0.97\ncount 3\n"
        )


def test_parse_distribution_rejects_bad_line():
    with pytest.raises(ValueError, match="expected 'key value'"):
        HandlerSupport().parse_distribution(hist(), "0.1 1 2")


def test_parse_distribution_rejects_nonfinite_value():
    with pytest.raises(ValueError, match="finite"):
        HandlerSupport().parse_distribution(
            hist(), "0.1 nan\n0.5 3\n1 3\nsum 0.97\ncount 3\n"
        )


def test_parse_distribution_rejects_negative_count():
    with pytest.raises(ValueError, match="count must be >= 0"):
        HandlerSupport().parse_distribution(
            hist(), "0.1 1\n0.5 3\n1 3\nsum 0.97\ncount -1\n"
        )


def test_parse_distribution_rejects_non_decreasing_buckets():
    with pytest.raises(ValueError, match="non-decreasing"):
        HandlerSupport().parse_distribution(
            hist(), "0.1 5\n0.5 3\n1 3\nsum 0.97\ncount 5\n"
        )


def test_parse_distribution_rejects_count_below_last_bucket():
    with pytest.raises(ValueError, match="count must be >= last"):
        HandlerSupport().parse_distribution(
            hist(), "0.1 1\n0.5 3\n1 3\nsum 0.97\ncount 2\n"
        )


def test_parse_distribution_rejects_missing_sum():
    with pytest.raises(ValueError, match="missing 'sum'"):
        HandlerSupport().parse_distribution(
            hist(), "0.1 1\n0.5 3\n1 3\ncount 3\n"
        )


# --- render_exposition: histograms and summaries ---


def test_render_exposition_histogram():
    out = HandlerSupport().render_exposition(
        [hist()],
        {
            "test_hist": DistributionSample(
                values={0.1: "1.0", 0.5: "3.0", 1.0: "3.0"},
                sum_value="0.97",
                count_value="3.0",
            )
        },
    )
    assert out == (
        "# HELP test_hist A histogram\n"
        "# TYPE test_hist histogram\n"
        'test_hist_bucket{le="0.1"} 1.0\n'
        'test_hist_bucket{le="0.5"} 3.0\n'
        'test_hist_bucket{le="1.0"} 3.0\n'
        'test_hist_bucket{le="+Inf"} 3.0\n'
        "test_hist_sum 0.97\n"
        "test_hist_count 3.0\n"
    )


def test_render_exposition_summary():
    out = HandlerSupport().render_exposition(
        [summ()],
        {
            "test_summary": DistributionSample(
                values={0.5: "0.25", 0.99: "0.9"},
                sum_value="1.2",
                count_value="3.0",
            )
        },
    )
    assert out == (
        "# HELP test_summary A summary\n"
        "# TYPE test_summary summary\n"
        'test_summary{quantile="0.5"} 0.25\n'
        'test_summary{quantile="0.99"} 0.9\n'
        "test_summary_sum 1.2\n"
        "test_summary_count 3.0\n"
    )


# --- render_exposition: labeled samples ---


def labeled(**kw):
    base = {
        "name": "test_users",
        "help_text": "Sessions per user",
        "value_type": "gauge",
        "cmd": "echo 1",
        "timeout": 5,
    }
    base.update(kw)
    return Metric(**base)


def test_render_exposition_labeled():
    out = HandlerSupport().render_exposition(
        [labeled()],
        {"test_users": LabeledSample("user", {"alice": "2.0", "bob": "1.0"})},
    )
    assert out == (
        "# HELP test_users Sessions per user\n"
        "# TYPE test_users gauge\n"
        'test_users{user="alice"} 2.0\n'
        'test_users{user="bob"} 1.0\n'
    )


def test_render_exposition_labeled_escapes_label_value():
    out = HandlerSupport().render_exposition(
        [labeled()],
        {"test_users": LabeledSample("user", {'a"b\\c': "1.0"})},
    )
    assert 'test_users{user="a\\"b\\\\c"} 1.0' in out


def test_render_exposition_labeled_empty():
    out = HandlerSupport().render_exposition(
        [labeled()], {"test_users": LabeledSample("user", {})}
    )
    assert out == (
        "# HELP test_users Sessions per user\n# TYPE test_users gauge\n\n"
    )


# --- render_exposition: multi-labeled samples ---


def test_render_exposition_multi_labeled():
    out = HandlerSupport().render_exposition(
        [labeled()],
        {
            "test_users": MultiLabeledSample(
                ["status_path", "common_name"],
                {
                    ("a.status", "bob"): "1.0",
                    ("a.status", "alice"): "2.0",
                },
            )
        },
    )
    assert out == (
        "# HELP test_users Sessions per user\n"
        "# TYPE test_users gauge\n"
        'test_users{status_path="a.status",common_name="alice"} 2.0\n'
        'test_users{status_path="a.status",common_name="bob"} 1.0\n'
    )


def test_render_exposition_multi_labeled_escapes_label_values():
    out = HandlerSupport().render_exposition(
        [labeled()],
        {
            "test_users": MultiLabeledSample(
                ["a", "b"], {('x"y', "p\\q"): "1.0"}
            )
        },
    )
    assert 'test_users{a="x\\"y",b="p\\\\q"} 1.0' in out


def test_render_exposition_multi_labeled_empty():
    out = HandlerSupport().render_exposition(
        [labeled()],
        {"test_users": MultiLabeledSample(["user"], {})},
    )
    assert out == (
        "# HELP test_users Sessions per user\n# TYPE test_users gauge\n\n"
    )
