from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from backend.app.assets import path_to_asset_id, resolve_asset_path, safe_component


@given(st.text(min_size=1, max_size=200))
def test_safe_component_never_creates_path(value: str) -> None:
    result = safe_component(value)
    assert result
    assert "/" not in result
    assert "\\" not in result
    assert result not in {".", ".."}


def test_asset_id_round_trip(tmp_path: Path) -> None:
    export_dir = tmp_path / "exports"
    target = export_dir / "project" / "panels" / "one.png"
    target.parent.mkdir(parents=True)
    target.touch()
    asset_id = path_to_asset_id(target, export_dir)
    assert asset_id == "project/panels/one.png"
    assert resolve_asset_path(asset_id, export_dir) == target.resolve()


def test_resolve_asset_path_rejects_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        resolve_asset_path("../outside.png", tmp_path / "exports")
