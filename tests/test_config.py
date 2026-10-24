import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from headway.config import DEFAULT_CONFIG_PATH, golemio_token, load_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / DEFAULT_CONFIG_PATH


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate tests from HEADWAY_* and GOLEMIO_TOKEN variables of the caller."""
    for name in list(os.environ):
        if name.upper().startswith("HEADWAY_") or name.upper() == "GOLEMIO_TOKEN":
            monkeypatch.delenv(name)


def test_default_config_loads() -> None:
    settings = load_settings(DEFAULT_CONFIG)
    assert settings.collect.interval_s > 0
    assert settings.time.local_tz == "Europe/Prague"


def test_env_overrides_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEADWAY_COLLECT__INTERVAL_S", "45")
    settings = load_settings(DEFAULT_CONFIG)
    assert settings.collect.interval_s == 45
    # Values not overridden still come from the file.
    assert settings.time.local_tz == "Europe/Prague"


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text(
        DEFAULT_CONFIG.read_text(encoding="utf-8") + '\n[surprise]\nkey = "x"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_settings(bad)


def test_token_in_toml_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "token.toml"
    bad.write_text(
        DEFAULT_CONFIG.read_text(encoding="utf-8").replace(
            "[collect]", '[collect]\ntoken = "not-a-real-token"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_settings(bad)


def test_token_read_only_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert golemio_token() is None
    monkeypatch.setenv("GOLEMIO_TOKEN", "dummy-value-for-test")
    token = golemio_token()
    assert token is not None
    assert token.get_secret_value() == "dummy-value-for-test"
    assert "dummy-value-for-test" not in repr(token)
