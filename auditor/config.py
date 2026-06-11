"""Load and validate rules.yaml; load secrets from .env."""

import os
from pathlib import Path

import yaml

from auditor.models import Rules


def load_rules(path: str | Path = "rules.yaml") -> Rules:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Rules.model_validate(raw or {})


def load_dotenv(path: str | Path = ".env") -> list[str]:
    """Minimal KEY=VALUE loader. The project .env is the source of truth:
    its values OVERRIDE shell exports (user decision, 2026-06-11 -- a stale
    placeholder export in a shell profile kept masking the real key).

    Returns the keys where a differing shell export was overridden, so
    callers can mention it."""
    path = Path(path)
    overridden: list[str] = []
    if not path.exists():
        return overridden
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if not value:
            continue
        if key in os.environ and os.environ[key] != value:
            overridden.append(key)
        os.environ[key] = value
    return overridden
