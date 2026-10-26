"""One instance, configurable conservative alarms."""
from homeassistant import config_entries
import voluptuous as vol
from .const import DOMAIN, DEFAULT_OPTIONS, resolved_options

def setup_schema(defaults=None):
    values={**DEFAULT_OPTIONS,**(defaults or {})}
    return vol.Schema({
        vol.Optional("low_disk_gib", default=values["low_disk_gib"]): vol.All(vol.Coerce(int), vol.Range(min=1,max=1024)),
        vol.Optional("min_excess_mib", default=values["min_excess_mib"]): vol.All(vol.Coerce(int), vol.Range(min=0,max=102400)),
        vol.Optional("retention_days", default=values["retention_days"]): vol.In((365,730,36500)),
        vol.Optional("repeat_notifications", default=values["repeat_notifications"]): bool,
        vol.Optional("enable_recorder_writer_diagnostics", default=values["enable_recorder_writer_diagnostics"]): bool,
    })

class StorageMonitorFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION=1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id("independent_storage_monitor")
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Storage Monitor",data=user_input)
        return self.async_show_form(step_id="user",data_schema=setup_schema())

    @staticmethod
    def async_get_options_flow(config_entry):
        return StorageMonitorOptions()

class StorageMonitorOptions(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="",data=user_input)
        current=resolved_options(self.config_entry.data,self.config_entry.options)
        return self.async_show_form(step_id="init",data_schema=setup_schema(current))
