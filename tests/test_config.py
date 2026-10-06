"""Tests for configuration lookup, and for refusing to guess a Shelf (#82)."""

import pytest
import yaml

from libris.config import (
    VaultNotConfigured,
    get_api_key,
    get_config_file,
    get_vault_path,
    is_vault_configured,
    set_book_vault_path,
    set_config,
)


def test_get_vault_path_refuses_to_guess_when_nothing_is_configured(
    tmp_path, monkeypatch
):
    # Given no configured Shelf, and a working directory that is not one
    monkeypatch.chdir(tmp_path)

    # When something asks where the Shelf is
    # Then it is told there is none, rather than handed the working directory
    with pytest.raises(VaultNotConfigured):
        get_vault_path()


def test_get_vault_path_returns_the_configured_shelf(tmp_path):
    # Given a configured Shelf
    shelf = tmp_path / "shelf"
    shelf.mkdir()
    set_book_vault_path(shelf)

    # When something asks where it is
    # Then it gets the resolved path
    assert get_vault_path() == shelf.resolve()


def test_get_vault_path_reads_the_legacy_key(tmp_path):
    # Given a config written before book_vault existed
    shelf = tmp_path / "legacy"
    shelf.mkdir()
    get_config_file().write_text(
        yaml.safe_dump({"vault_path": str(shelf.resolve())}), encoding="utf-8"
    )

    # When something asks where the Shelf is
    # Then the legacy key still answers
    assert get_vault_path() == shelf.resolve()


def test_is_vault_configured_reports_the_absence_without_raising(tmp_path):
    # Given no configured Shelf
    # When a caller that must report rather than fail asks
    # Then it gets an answer, not an exception
    assert is_vault_configured() is False

    # And once one is set, it says so
    shelf = tmp_path / "shelf"
    shelf.mkdir()
    set_book_vault_path(shelf)
    assert is_vault_configured() is True


def test_the_api_key_comes_from_the_environment_where_there_is_no_config(
    monkeypatch,
):
    # Given a key in the environment, as the Container App sets it from Key
    # Vault, and none in a config file
    monkeypatch.setenv("LIBRIS_GOOGLE_BOOKS_API_KEY", "from-key-vault")

    # When Google Books asks for the key
    # Then it gets the one the environment holds
    assert get_api_key() == "from-key-vault"


def test_the_environment_key_wins_over_the_config_file(monkeypatch):
    # Given a key in the config file and another in the environment
    set_config("google_books_api_key", "from-config")
    monkeypatch.setenv("LIBRIS_GOOGLE_BOOKS_API_KEY", "from-environment")

    # When Google Books asks for the key
    # Then the environment's is used, as a setting closer to the process
    assert get_api_key() == "from-environment"


def test_the_config_files_key_is_used_when_the_environment_has_none(monkeypatch):
    # Given a key in the config file, and an empty variable in the environment
    set_config("google_books_api_key", "from-config")
    monkeypatch.setenv("LIBRIS_GOOGLE_BOOKS_API_KEY", "")

    # When Google Books asks for the key
    # Then the config file's is used: an empty variable is not a key
    assert get_api_key() == "from-config"
