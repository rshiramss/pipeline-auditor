"""Load and validate rules.yaml."""

from pathlib import Path

import yaml

from auditor.models import Rules


def load_rules(path: str | Path = "rules.yaml") -> Rules:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Rules.model_validate(raw or {})
