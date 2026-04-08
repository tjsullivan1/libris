import yaml
import os
from pathlib import Path
from typing import Optional

def get_config_dir() -> Path:
    env_config_dir = os.environ.get("LIBRIS_CONFIG_DIR")
    if env_config_dir:
        return Path(env_config_dir).expanduser().resolve()
    return Path.home() / ".config" / "libris"

def get_config_file() -> Path:
    return get_config_dir() / "config.yaml"

def get_config() -> dict:
    config_file = get_config_file()
    if not config_file.exists():
        return {}
    with open(config_file, "r") as f:
        return yaml.safe_load(f) or {}

def set_config(key: str, value: str):
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    config = get_config()
    config[key] = value
    with open(get_config_file(), "w") as f:
        yaml.dump(config, f)

def get_vault_path() -> Path:
    config = get_config()
    path_str = config.get("vault_path")
    if not path_str:
        return Path(".").resolve()
    return Path(path_str).expanduser().resolve()

def get_api_key() -> Optional[str]:
    config = get_config()
    return config.get("google_books_api_key")
