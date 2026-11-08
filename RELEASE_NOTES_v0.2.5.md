# Home Assistant Storage Monitor 0.2.5-beta.1

This release makes growth-baseline state auditable without querying Recorder,
InfluxDB, or any monitored SQLite database. The existing Growth Evaluation
table now reports verified UTC-day and six-hour-checkpoint coverage, missing
required measurements, the actual last observed shrink and byte values, the
earliest possible clean baseline time, and a specific reason when comparison
is unavailable. English and Slovenian presentation are included.

The investigation confirmed that a genuine shrink intentionally places a
source in **Maintenance** for the rolling eight-day window. Missing, cached,
stale and unavailable observations are not treated as sizes and cannot create
a false shrink. A valid latest 24-hour delta remains visible during Maintenance
when both endpoints exist; the invalid prior-seven-day denominator remains
unavailable. Zero/negative baselines remain Learning, and the strict greater-
than-5-percent threshold is unchanged.

The InfluxDB evaluator can collect all 33 genuine six-hour checkpoints during
eight clean ordinary days. Spring DST necessarily skips the five-elapsed-hour
00→06 opportunity, and either DST phase shift requires a new clean exact-UTC
window; cached values never fill the gap. A startup at 18:34 safely defers the
midnight probe and can resume at 06:00.

One confirmed defect was corrected: probe-health scheduling compared a saved
06:00:10 attempt with a theoretical 12:00:00 checkpoint and displayed 18:00 as
next eligible. The real scheduler runs at `:00:10`, so 12:00:10 satisfies the
unchanged strict six-elapsed-hour limit. The diagnostic now uses that exact
callback second. No extra Supervisor scan is introduced.

Source retirement remains isolated from active-source evidence. Retired
sources stay excluded, while explicit reactivation continues to require a
fresh eight-day learning window and now exposes that reason and earliest
possible checkpoint in the dashboard.
