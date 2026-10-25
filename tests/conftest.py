"""Import the pure-Python modules without installing Home Assistant."""
import pathlib, sys, types
root=pathlib.Path(__file__).parents[1]/"custom_components"/"ha_storage_monitor"
pkg=types.ModuleType("ha_storage_monitor")
pkg.__path__=[str(root)]
sys.modules.setdefault("ha_storage_monitor",pkg)
