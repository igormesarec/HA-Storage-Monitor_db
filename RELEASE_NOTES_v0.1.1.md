# v0.1.1-beta.1 | Development update (not deployed)

## Verified in this repository review

The GitHub `main` branch has the existing custom integration and tests, but the uploaded `tests.yml` is at the repository root. GitHub Actions only runs workflow files under `.github/workflows/`. Upload the included `.github` directory with a method that preserves dot-directories (GitHub browser uploads can flatten folder structure). Remove the root `tests.yml` later as a separate, explicitly approved cleanup of the repository, not production HA.

## Changes

1. Correct GitHub Actions workflow location and limit token permissions to read-only.
2. Close all own snapshot SQLite connections after each transaction.
3. Explicitly close read-only Recorder diagnostic SQLite connections.
4. Protect a verified hourly sample from later cached/unavailable duplicate writes.
5. Add regression tests for connection cleanup and snapshot idempotency.
6. Generic SQLite discovery now treats an empty successful scan as measured zero, and refuses to report partial totals when discovery is truncated or inaccessible.

## Installation gate

Do **not** install or restart Home Assistant yet. HA Core WebSocket/static path compatibility and protected InfluxDB Supervisor API access still need a real HA OS test. Existing Recorder SQL sensor and all production databases stay unchanged.

## HACS icon caveat

Root `icon.png` is a bundled brand asset, not guaranteed automatic HACS catalog branding. Home Assistant brand icons typically need registration in the Home Assistant brands repository; a local integration can separately reference its own asset in documentation.
