"""One instance, configurable conservative alarms."""
from homeassistant import config_entries
import voluptuous as vol
from .const import DOMAIN

SETUP = vol.Schema({
    vol.Optional("low_disk_gib", default=15): vol.All(vol.Coerce(int), vol.Range(min=1,max=1024)),
    vol.Optional("min_excess_mib", default=0): vol.All(vol.Coerce(int), vol.Range(min=0,max=102400)),
    vol.Optional("retention_days", default=365): vol.In((365,730,36500)),
    vol.Optional("repeat_notifications", default=False): bool,
})

class StorageMonitorFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION=1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id("independent_storage_monitor")
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Storage Monitor",data=user_input)
        return self.async_show_form(step_id="user",data_schema=SETUP)

    @staticmethod
    def async_get_options_flow(config_entry):
        return StorageMonitorOptions()

class StorageMonitorOptions(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="",data=user_input)
        return self.async_show_form(step_id="init",data_schema=SETUP)
