from __future__ import annotations

import re

import pytest

from modal_devin import ConfigurationError, WorkerSettings
from modal_devin._config import WorkerConfig


def test_worker_config_derives_safe_bounded_modal_names():
    config = WorkerConfig(
        name="Production / Platform ✨ " + "x" * 100,
        outpost_id="outpost_env-demo",
    )

    for value in (
        config.app_name,
        config.snapshot_store_name,
        config.image_build_app_name,
    ):
        assert len(value) < 64
        assert re.fullmatch(r"[a-z0-9._-]+", value)
    assert config.acceptor_id.startswith("modal-")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"name": "", "outpost_id": "outpost"}, "name"),
        ({"name": "worker", "outpost_id": ""}, "outpost_id"),
        ({"name": "worker", "outpost_id": "outpost", "api_url": "file:///tmp/x"}, "api_url"),
        (
            {"name": "worker", "outpost_id": "outpost", "api_url": "https://u:p@example.com"},
            "credentials",
        ),
    ],
)
def test_worker_config_rejects_invalid_identity(kwargs, message):
    with pytest.raises(ConfigurationError, match=message):
        WorkerConfig(**kwargs)


def test_worker_settings_read_typed_environment(monkeypatch):
    monkeypatch.setenv("MODAL_DEVIN_SCHEDULER_INTERVAL_SECONDS", "17")
    monkeypatch.setenv("MODAL_DEVIN_API_TIMEOUT_SECONDS", "4.5")
    monkeypatch.setenv("MODAL_DEVIN_SNAPSHOT_TTL_SECONDS", "none")
    monkeypatch.setenv("MODAL_DEVIN_SNAPSHOT_TIMEOUT_SECONDS", "75")
    monkeypatch.setenv("MODAL_DEVIN_SIDECAR_READY_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("MODAL_DEVIN_LOG_LEVEL", "debug")

    settings = WorkerSettings.from_env()

    assert settings.scheduler_interval_seconds == 17
    assert settings.api_timeout_seconds == 4.5
    assert settings.snapshot_ttl_seconds is None
    assert settings.snapshot_timeout_seconds == 75
    assert settings.sidecar_ready_timeout_seconds == 12
    assert settings.log_level == "debug"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"scheduler_interval_seconds": 0},
        {"session_timeout_seconds": -1},
        {"snapshot_ttl_seconds": 0},
        {"status_attempts": 0},
        {"status_retry_delay_seconds": -1},
        {"sidecar_ready_timeout_seconds": 0},
        {"snapshot_timeout_seconds": 0},
        {"log_level": "LOUD"},
    ],
)
def test_worker_settings_reject_invalid_values(kwargs):
    with pytest.raises(ConfigurationError):
        WorkerSettings(**kwargs)


def test_invalid_environment_value_names_the_variable(monkeypatch):
    monkeypatch.setenv("MODAL_DEVIN_SESSION_TIMEOUT_SECONDS", "later")

    with pytest.raises(ConfigurationError, match="MODAL_DEVIN_SESSION_TIMEOUT_SECONDS"):
        WorkerSettings.from_env()
