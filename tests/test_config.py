"""load_dotenv: .env is the source of truth and overrides shell exports."""

import os

from auditor.config import load_dotenv


def test_env_file_overrides_shell_export(tmp_path, monkeypatch):
    monkeypatch.setenv("PA_TEST_KEY", "stale-shell-placeholder")
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nPA_TEST_KEY=real-value\nPA_EMPTY=\n")

    overridden = load_dotenv(env_file)

    assert os.environ["PA_TEST_KEY"] == "real-value"
    assert overridden == ["PA_TEST_KEY"]
    assert "PA_EMPTY" not in os.environ  # empty values never clobber


def test_missing_file_is_a_noop(tmp_path):
    assert load_dotenv(tmp_path / "absent.env") == []
