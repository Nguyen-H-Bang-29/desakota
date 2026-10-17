"""Tests for the data-tree layout."""

from pathlib import Path

import pytest

from desakota.paths import DATA_DIR_ENV_VAR, PROJECT_ROOT, DataLayout


def test_defaults_to_data_dir_beside_the_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    monkeypatch.delenv(DATA_DIR_ENV_VAR, raising=False)

    # Act
    layout = DataLayout.from_env()

    # Assert
    assert layout.root == PROJECT_ROOT / 'data'


def test_env_var_overrides_the_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Arrange
    monkeypatch.setenv(DATA_DIR_ENV_VAR, str(tmp_path))

    # Act
    layout = DataLayout.from_env()

    # Assert
    assert layout.root == tmp_path.resolve()
    assert layout.raw == tmp_path.resolve() / 'raw'


def test_ensure_creates_every_stage(tmp_path: Path) -> None:
    # Arrange
    layout = DataLayout(root=tmp_path / 'tree')

    # Act
    returned = layout.ensure()

    # Assert
    assert returned is layout
    assert layout.raw.is_dir()
    assert layout.interim.is_dir()
    assert layout.processed.is_dir()


def test_ensure_is_idempotent(tmp_path: Path) -> None:
    # Arrange
    layout = DataLayout(root=tmp_path / 'tree').ensure()

    # Act / Assert — a second call must not raise
    layout.ensure()
    assert layout.processed.is_dir()
