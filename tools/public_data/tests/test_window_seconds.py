from __future__ import annotations

import csv
from pathlib import Path

from tools.public_data.experiment import ExperimentConfig


def test_rule_window_seconds_defaults_to_60() -> None:
    assert ExperimentConfig.rule_window_seconds == 60.0


def test_rule_window_seconds_is_configurable() -> None:
    config = ExperimentConfig(rule_window_seconds=5.0)
    assert config.rule_window_seconds == 5.0


def test_extractor_emits_window_seconds_column() -> None:
    """The rule baseline must not guess a 60 s window for 5 s MCCD clips."""
    from tools.public_data.extract_classroom_features import BASE_COLUMNS

    assert "window_seconds" in BASE_COLUMNS
    # It must come after label so existing column order stays stable for readers
    assert BASE_COLUMNS.index("window_seconds") > BASE_COLUMNS.index("label")
