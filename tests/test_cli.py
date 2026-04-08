import typer
from typer.testing import CliRunner
from libris.cli import app
import os
from pathlib import Path
import yaml
import pytest

runner = CliRunner()

def test_config_vault_path(tmp_path):
    # Test getting current vault path (should not fail)
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "Current vault path:" in result.output

    # Test setting vault path
    vault_path = tmp_path / "my_vault"
    result = runner.invoke(app, ["config", "--vault", str(vault_path)])
    assert result.exit_code == 0
    assert f"Vault path set to: {vault_path.resolve()}" in result.output
    assert vault_path.exists()

    # Test setting API key
    result = runner.invoke(app, ["config", "--api-key", "my-secret-key"])
    assert result.exit_code == 0
    assert "API key set successfully." in result.output
    
    # Test getting current config
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "API key: *********-key" in result.output

def test_cleanup_command(tmp_path):
    # Mock vault path
    vault_path = tmp_path / "my_vault"
    vault_path.mkdir()
    
    # Create a legacy file
    legacy_file = vault_path / "Legacy.md"
    legacy_file.write_text("---\ntitle: Legacy\nstatus: To Read\ngoogle_books_id: 123\n---\n")
    
    # Run cleanup via CLI
    # We need to ensure the config uses this vault path
    from libris.config import set_config
    set_config("vault_path", str(vault_path))
    
    result = runner.invoke(app, ["cleanup"])
    assert result.exit_code == 0
    assert "Updated: Legacy.md" in result.output
    assert "Finished. Updated 1 books." in result.output
    
    # Verify file content
    content = legacy_file.read_text()
    assert "tags: Book" in content
    
    # Run again
    result = runner.invoke(app, ["cleanup"])
    assert result.exit_code == 0
    assert "All books are already up to date." in result.output


def test_list_command_timing_flag(tmp_path):
    vault_path = tmp_path / "my_vault"
    vault_path.mkdir()

    # Valid book note (should be listed)
    book_file = vault_path / "Book.md"
    book_file.write_text("---\nstatus: To Read\ngoogle_books_id: 123\n---\n", encoding="utf-8")

    from libris.config import set_config
    set_config("vault_path", str(vault_path))

    result = runner.invoke(app, ["list", "--timing"])
    assert result.exit_code == 0
    assert "- Book.md [To Read]" in result.output
    assert "Scan time:" in result.output
