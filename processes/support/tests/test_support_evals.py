from pathlib import Path
from typing import Any

import yaml

from mappd.data.cases import read_cases

EVALS = Path(__file__).parents[1] / "evals"


def resolve(data: dict[str, Any], dotted: str) -> object:
    value: object = data
    for part in dotted.split("."):
        assert isinstance(value, dict), dotted
        value = value[part]
    return value


def test_every_metric_field_exists_in_every_dev_case() -> None:
    config = yaml.safe_load((EVALS / "config.yaml").read_text())
    cases = read_cases(EVALS / "dev.jsonl")
    assert cases
    for metric in config["metrics"]:
        if "field" in metric:
            for case in cases:
                resolve(case.model_dump(), metric["field"])
