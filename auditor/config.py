"""Load and validate rules.yaml; load secrets from .env."""

import os
from pathlib import Path

import yaml

from auditor.models import Rules


def load_rules(path: str | Path = "rules.yaml") -> Rules:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Rules.model_validate(raw or {})


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal KEY=VALUE loader. Real shell env vars take precedence,
    so an exported key always beats the file."""
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if value and key not in os.environ:
            os.environ[key] = value
