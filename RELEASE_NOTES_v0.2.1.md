# Home Assistant Storage Monitor 0.2.1-beta.1

This focused hotfix corrects false InfluxDB stale alerts without changing the
six-hour Supervisor scan limit, Recorder diagnostics or storage-growth logic.

## Root cause and correction

v0.2.0 correctly changed an Influx cache older than seven hours from `cached`
to `stale`, but then passed those hourly cache rows into the same two-missed-
hour rule used by Recorder and disk measurements. After an 18:34 successful
probe and an intentionally deferred midnight request, the 02:00 and 03:00 rows
could therefore activate an alert even though no eligible Supervisor probe had
failed. The observed production notification is consistent with this code path;
it is not evidence that the InfluxDB service itself failed.

InfluxDB now has a cadence-aware measurement-health policy. Expected deferrals
remain cached/stale with the original timestamp and never count as failures.
Eligible attempt outcomes and consecutive failures are durable in Storage
Monitor's SQLite. Two failed eligible attempts, or an eligible checkpoint that
is still unrecorded one hour later, activates the measurement-health alert. A
successful eligible probe resets the count, clears the durable alert and
dismisses only Storage Monitor's own persistent notification.

Eligible attempts are reserved durably before network I/O. If Home Assistant
is interrupted before the result is saved, the attempt remains `in_progress`
for a two-minute grace period and is then recorded as `interrupted`, distinct
from an HTTP/API failure. Interrupted attempts count toward prolonged
measurement unavailability, cannot bypass the six-elapsed-hour scan minimum,
and never replace the last genuine measurement or its original timestamp.

The alert includes last success, last attempt and outcome, measurement age,
reason and next eligible checkpoint, and explicitly distinguishes measurement
availability from InfluxDB service health. No extra polling is introduced.

## Safety and update

The expensive read-only Supervisor scan remains limited to one attempt per six
elapsed hours, including failures and restarts. Dashboard refreshes read only
the monitor SQLite. Recorder writer diagnostics remain disabled by default.
No production database, retention, index, archive or SI Grid Monitor behavior
is changed.

Download through HACS only after review. Activating changed integration Python
still requires a separately approved controlled Home Assistant restart. Keep
`.storage/ha_storage_monitor_history.sqlite3`; do not delete or recreate it.
