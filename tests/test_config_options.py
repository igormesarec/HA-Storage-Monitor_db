"""Conservative option defaults and config-entry reload semantics."""
import json
from ha_storage_monitor.const import resolved_options


def test_recorder_writer_diagnostics_default_disabled():
    assert resolved_options({}, {})['enable_recorder_writer_diagnostics'] is False


def test_options_override_entry_data_on_reload():
    data={'enable_recorder_writer_diagnostics':False,'low_disk_gib':15}
    enabled=resolved_options(data,{'enable_recorder_writer_diagnostics':True})
    assert enabled['enable_recorder_writer_diagnostics'] is True
    disabled=resolved_options(data,{'enable_recorder_writer_diagnostics':False})
    assert disabled['enable_recorder_writer_diagnostics'] is False


def test_dashboard_contains_explicit_disabled_state():
    from pathlib import Path
    card=(Path(__file__).parents[1]/'custom_components'/'ha_storage_monitor'/
          'frontend'/'storage-monitor-card.js').read_text(encoding='utf-8')
    assert 'Recorder writer diagnostics disabled' in card


def test_config_translations_expose_writer_diagnostics_option():
    from pathlib import Path
    root=Path(__file__).parents[1]/'custom_components'/'ha_storage_monitor'
    for path in (root/'strings.json',root/'translations'/'en.json',
                 root/'translations'/'sl.json'):
        data=json.loads(path.read_text(encoding='utf-8'))
        assert 'enable_recorder_writer_diagnostics' in (
            data['config']['step']['user']['data'])
        assert 'enable_recorder_writer_diagnostics' in (
            data['options']['step']['init']['data'])
