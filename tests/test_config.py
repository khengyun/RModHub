"""`app.config.Settings`: environment aliases and the "empty string means unset" rule.

Every test builds `Settings(_env_file=None)` under a controlled environment so the
developer's `.env` and shell cannot leak in.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from app.config import DEFAULT_IP_HASH_SECRET, DEFAULT_SAMPLE_DIR, Settings

_SIGNAL_VARS = (
    "RMODHUB_DATABASE_URL",
    "DATABASE_URL",
    "RMODHUB_CELERY_BROKER_URL",
    "CELERY_BROKER_URL",
    "RMODHUB_UPLOAD_DIR",
    "RMODHUB_SAMPLE_DIR",
    "RMODHUB_IP_HASH_SECRET",
    "RMODHUB_MAX_POD5_GB",
    "MAX_POD5_GB",
    "RMODHUB_MAX_BAM_GB",
    "RMODHUB_CORS_ORIGINS",
)


@pytest.fixture
def env(monkeypatch):
    for name in _SIGNAL_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_defaults(env):
    s = _settings()
    assert s.database_url is None and s.signal_enabled is False
    assert s.celery_broker_url is None
    assert s.upload_dir == Path("/data/uploads") and s.sample_dir == DEFAULT_SAMPLE_DIR
    assert s.ip_hash_secret_is_default is True
    assert s.max_pod5_gb == 5 and s.max_bam_gb == 5


@pytest.mark.parametrize(
    "name",
    [
        "RMODHUB_UPLOAD_DIR",
        "RMODHUB_SAMPLE_DIR",
        "RMODHUB_IP_HASH_SECRET",
        "RMODHUB_MAX_POD5_GB",
        "MAX_POD5_GB",
        "RMODHUB_MAX_BAM_GB",
        "RMODHUB_DATABASE_URL",
        "DATABASE_URL",
        "CELERY_BROKER_URL",
        "RMODHUB_CORS_ORIGINS",
    ],
)
@pytest.mark.parametrize("value", ["", "  "])
def test_empty_env_value_means_unset(env, name, value):
    """`VAR=` in .env / compose is "use the default", never the cwd, an empty key or a crash."""
    env.setenv(name, value)
    s = _settings()
    defaults = Settings(_env_file=None)
    assert s.model_dump() == defaults.model_dump()
    assert s.upload_dir != Path(".") and s.sample_dir != Path(".")
    assert s.ip_hash_secret.get_secret_value() == DEFAULT_IP_HASH_SECRET
    assert s.ip_hash_secret_is_default is True


def test_aliases_and_precedence(env):
    env.setenv("MAX_POD5_GB", "2")
    env.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    env.setenv("CELERY_BROKER_URL", "redis://:pw@redis:6379/0")
    s = _settings()
    assert s.max_pod5_gb == 2.0 and s.signal_enabled is True
    assert isinstance(s.celery_broker_url, SecretStr)
    assert s.celery_broker_url.get_secret_value() == "redis://:pw@redis:6379/0"

    env.setenv("RMODHUB_MAX_POD5_GB", "3")
    env.setenv("RMODHUB_DATABASE_URL", "sqlite+pysqlite:///other.db")
    s = _settings()
    assert s.max_pod5_gb == 3.0  # the prefixed name wins
    assert s.database_url.get_secret_value() == "sqlite+pysqlite:///other.db"


def test_for_log_redacts_every_secret(env):
    s = _settings(
        database_url="postgresql+psycopg://u:DBPW@h/db",
        celery_broker_url="redis://:BROKERPW@h:6379/0",
        ip_hash_secret="HMACPW",
    )
    dumped = s.for_log()
    assert dumped["database_url"] == "***"
    assert dumped["celery_broker_url"] == "***"
    assert dumped["ip_hash_secret"] == "***"
    assert dumped["signal_enabled"] is True
    text = repr(dumped)
    for secret in ("DBPW", "BROKERPW", "HMACPW"):
        assert secret not in text
    assert _settings().for_log()["celery_broker_url"] is None
