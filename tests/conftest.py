import pytest
import os
from pathlib import Path

@pytest.fixture(autouse=True)
def mock_config_dir(tmp_path, monkeypatch):
    """Automatically mock the configuration directory for all tests in the project."""
    config_dir = tmp_path / "libris_config"
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LIBRIS_CONFIG_DIR", str(config_dir))
    return config_dir
