# Home Assistant Storage Monitor 0.2.3-beta.1

This release preserves the configurable Top 20/50/100/500 writer ranking and
its bounded 50-row pagination from 0.2.2-beta.1, while adding one explicitly
authorized read-only database source:

`/config/.storage/home_energy_intelligence/home_energy_intelligence.sqlite3`

Storage Monitor measures the apparent size of the exact SQLite main file and
its optional WAL and SHM sidecars using filesystem metadata only. It does not
open the database, recurse through its directory, follow symlinks, or include
the source in **Other local SQLite total**. Genuine hourly observations use the
existing history, 6h/24h delta, chart, stale-warning and eight-day growth paths.

If Home Energy Intelligence is not installed, its dedicated card reports
**Unavailable**, never zero. After two consecutive unavailable hourly samples,
the normal durable missing-measurement warning applies. That warning means only
that this optional source cannot be measured; it is not evidence of data loss
and is not an instruction to install the optional integration.
