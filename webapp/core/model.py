from dataclasses import dataclass


@dataclass
class Metric:
    name: str = ""
    help_text: str = ""
    value_type: str = ""
    cmd: str = ""
    timeout: float = 1
    buckets: list[float] | None = None
    quantiles: list[float] | None = None


@dataclass
class DistributionSample:
    """Parsed histogram/summary output.

    values maps the declared bounds/quantiles to their canonical float
    string; sum_value and count_value are the trailing `sum`/`count`
    lines of the command output.
    """

    values: dict[float, str]
    sum_value: str
    count_value: str


@dataclass
class LabeledSample:
    """A scalar family split across one label dimension.

    label_name is the label key; values maps each label value to its
    canonical float string. Renders as `name{label="v"} value` per
    entry (empty values render no samples).
    """

    label_name: str
    values: dict[str, str]


@dataclass
class MultiLabeledSample:
    """A scalar family split across several label dimensions.

    label_names is the ordered tuple of label keys; rows maps a label
    value tuple (in the same order as label_names) to its canonical
    float string. Renders as `name{k1="v1",k2="v2"} value` per row
    (empty rows render no samples).
    """

    label_names: list[str]
    rows: dict[tuple[str, ...], str]


SampleValue = str | DistributionSample | LabeledSample | MultiLabeledSample
"""A value a handler's execute() may produce for one metric."""
