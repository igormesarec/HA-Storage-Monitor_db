"""Packaging checks that do not require a Home Assistant installation."""
import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_manifest_declares_runtime_components_used_during_setup():
    manifest = json.loads((ROOT / "custom_components" / "ha_storage_monitor" /
                           "manifest.json").read_text(encoding="utf-8"))
    assert {"http", "websocket_api"}.issubset(manifest["dependencies"])
    assert manifest["config_flow"] is True
    assert manifest["requirements"] == []
    assert manifest["version"] == "0.1.2-beta.2"


def test_hacs_metadata_targets_supported_core_release():
    metadata = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    minimum = tuple(int(part) for part in metadata["homeassistant"].split("."))
    assert minimum <= (2026, 10, 4)
