# Home Assistant Storage Monitor 0.2.2-beta.1

This focused feature release makes the Top Writers entity ranking selectable as
Top 20 (default), Top 50, Top 100 or Top 500.

The ranking is computed only from completed six-hour writer summaries already
stored in Storage Monitor's own SQLite. An admin-only WebSocket request validates
the selected limit and returns a fixed page of at most 50 entity rows. Top 100
and Top 500 therefore never transfer all ranked entities in one entity page.
Sorting remains descending by measured record count with entity ID as the
deterministic tie-breaker. Integration groupings cover the selected Top N and
are labeled as partial totals, not complete integration counts.

Changing the selector does not open Recorder, perform a retroactive query or
change the collector schedule, time window, four-second budget, 500,000-row cap,
5,000-entity cap, or 256-KiB serialized collection cap. Recorder diagnostics
remain opt-in and disabled by default. Incomplete and learning intervals remain
explicit and are never extrapolated.

No Recorder, InfluxDB, WAL/SHM, SI Grid Monitor, alert, storage-history or
Supervisor measurement behavior is changed by this release.

Pagination bounds entity transfer and browser rendering, but computing a page
still reads the retained completed writer summaries from the monitor's own
SQLite and aggregates the selected ranking server-side. In an offline synthetic
run with 32 windows and 600 entities, 100 Top-500 summaries averaged 2.497 ms
(3.032 ms maximum) and a 50-entity JSON page before integration grouping was
8,741 bytes. These figures are development-machine measurements, not HA OS
performance guarantees; registry mapping and SQLite JSON decoding add overhead.
