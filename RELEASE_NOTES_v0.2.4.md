# Home Assistant Storage Monitor 0.2.4-beta.1

This focused release adds explicit retirement for previously monitored local
SQLite sources. An administrator can retire or reactivate a `database:*` source
from the Settings tab through admin-only WebSocket commands.

Retirement changes only Storage Monitor's own lifecycle metadata. It preserves
all historical snapshots and original timestamps, closes existing growth and
missing-source alert state, dismisses only the associated Storage Monitor
persistent notifications, and excludes the source from current lists, active
growth evaluation, missing-source checks and the **Other local SQLite total**.
Historical charts keep the source and label it **Retired / Upokojeno**.

A retired source that reappears remains excluded and is marked as detected
again. It is never silently reintroduced. Explicit reactivation resumes hourly
metadata sampling and begins a fresh eight-elapsed-day growth-learning window,
so pre-retirement evidence cannot be mixed into a new alarm baseline.

No production database is opened for writing, modified, renamed or deleted.
Recorder writer diagnostics remain opt-in and off by default; Recorder and
InfluxDB query behavior is unchanged.
