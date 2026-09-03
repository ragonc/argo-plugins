# garmin-pull

Get your own Garmin Connect data out of the app and into files: sleep, HRV, resting
heart rate, stress, body battery, steps, VO2max and every workout, as daily summaries
or as every single data point the watch recorded during the day. One CSV per
category, one SQLite database, and the raw JSON. Pick only the categories and the
date range you want.

Pure Python standard library, 3.11 or newer. Nothing to install. Talks only to
`sso.garmin.com` and `connectapi.garmin.com`.

## Install as a Claude Code plugin

```
/plugin marketplace add ragonc/argo-plugins
/plugin install garmin-pull@argo-plugins
```

Then just ask Claude: "get my Garmin data for the last 3 months, sleep and HRV only".
The `garmin-pull` skill walks you through the one-time login and runs the pull.

## Use it without Claude

```
python3 scripts/garmin_setup.py                     # once: email, password (hidden), 2FA code if asked
python3 scripts/garmin_pull.py                      # last 30 days, daily summaries -> ./garmin-data
python3 scripts/garmin_pull.py --last 1y --what sleep,hrv,summary
python3 scripts/garmin_pull.py --since 2026-01-01 --until 2026-06-30 --what activities --workout-files
python3 scripts/garmin_pull.py --full-day --last 7d # every data point of each day, see below
python3 scripts/garmin_pull.py --all-history        # from your first Garmin activity to today
```

| flag | meaning |
|---|---|
| `--what LIST` | `all` (default: the five daily summaries) or any of `sleep`, `hrv`, `summary`, `vo2max`, `activities`, `detail`, comma-separated |
| `--full-day` | every data point the watch stored each day: all summaries + `detail` + workout files |
| `--last PERIOD` | `30d` (default), `12w`, `6m`, `2y` ... |
| `--since DATE` `--until DATE` | an explicit range, `YYYY-MM-DD` |
| `--all-history` | from your first Garmin activity to today |
| `--workout-files` | also save each workout's TCX file (per-second heart rate, pace, GPS) |
| `--out FOLDER` | where to write (default `./garmin-data`) |
| `--format` | `csv`, `sqlite` or both (default both; raw JSON is always written) |
| `--refresh` | re-pull days already on disk |
| `--yes` | skip the confirmation for long pulls |

Before a pull the script prints the range, the categories, and roughly how long it
will take. Anything over about 15 minutes asks for a yes first (`--yes` skips that).

### Every data point of a day

The five summaries are one row per day. `--full-day` (or `--what detail`) adds what
the watch actually recorded through the day: heart rate every two minutes, stress
and body battery every three, steps per quarter hour, breathing rate, SpO2, HRV
readings through the night, sleep stages and movement, plus that day's training
readiness, hydration, training status, endurance score and fitness age. It is about
14 requests per day, so four times the cost of the summaries; a day comes out at
roughly 4,000 timeline rows.

Two extra files appear: `timeline.csv` (date, series, time_gmt, value, value2, one row
per measurement) and `snapshots.csv` (date, metric, value). Both are tables in
`garmin.db` too. The untouched Garmin responses are kept under `raw/detail/<day>/`,
one JSON per endpoint, so anything the flattening does not cover is still there.

The flattening adapts rather than assuming. Garmin ships a descriptor list next to
every array saying which column is the timestamp and what the others are; the
flattener reads those, so a reordered or extended array still lands in the right
columns. Any list with a time field becomes a series, any scalar becomes a snapshot
metric, and unknown fields get a generated name instead of being dropped. A shipped
`schema_baseline.json` records the field names seen when this was built; every pull
compares and the report says what Garmin added or removed, so you hear about a
change from the report rather than from an empty column.

### After every pull: the report

The last thing printed, also saved as `report.txt`: days complete, days with errors,
days Garmin had no data for, timeline series with row counts, files written, and any
schema drift. If the run was cut short by rate limiting it says so and what to do.

### Keeping it healthy: the weekly check

Garmin changes its login flow and response shapes now and then, and when that
happens every tool of this kind breaks at once. Run the check on your own machine
so you hear about it before you need the data:

```
python3 scripts/garmin_canary.py                 # once, prints the verdict
python3 scripts/garmin_canary.py --install       # every Monday 08:15, Linux (systemd) or macOS (launchd)
python3 scripts/garmin_canary.py --install --webhook https://discord.com/api/webhooks/...
python3 scripts/garmin_canary.py --uninstall
```

It restores your cached session, fetches yesterday's summaries and every detail
endpoint, flattens them, and compares field names with the baseline. Exit 0 is
healthy, 2 means Garmin changed something but the pull still works, 1 means broken.
With a webhook it posts only when the verdict is not healthy; without one it just
appends to `~/.garmin-pull/canary.log`. On Windows it prints the command to put in
Task Scheduler. Nothing runs anywhere but your own computer.

### Something not working? The doctor

```
python3 scripts/garmin_doctor.py            # checks, then one small request to Garmin
python3 scripts/garmin_doctor.py --offline  # no network
```

Python version, credentials, cached session and its age, the state of a previous
pull, and one request to prove the session works. Each line says OK, WARN or FAIL
and what to do.

### All of your history

`--all-history` first pages through your activity list to find your oldest workout,
then pulls from that day to today. Combine with `--full-day` for the total drop. For a
watch worn for five years the summaries alone are around 7,000 requests and well
over an hour, and Garmin will almost certainly rate-limit you partway. That is fine
and expected: the pull stops cleanly, and rerunning the same command later continues
from the last day on disk. Two or three sessions usually get everything.

## What you get

```
garmin-data/
  raw/2026-05-01.json ...   one shaped JSON per day, also the resume checkpoint
  raw/activities.json
  raw/detail/<day>/*.json   untouched responses, one per endpoint (detail / --full-day)
  sleep.csv  hrv.csv  summary.csv  vo2max.csv  activities.csv
  timeline.csv  snapshots.csv   every measurement / every daily metric (detail)
  garmin.db                 SQLite, one table per CSV, keyed on date / activity_id
  tcx/<activity_id>.tcx     with --workout-files or --full-day
```

Empty cells mean Garmin did not report that metric for that day. Nothing is
interpolated or guessed.

## How the login works, and why it is safe to read

Garmin has no public API for personal data. This uses the same login flow as the
Garmin Connect phone app: SSO login, then an OAuth1 token, then an OAuth2 bearer token.
All of it is in `scripts/garmin_client.py`, written against the documented protocol
with the standard library, so you can read every line that touches your password.

- Credentials are stored in `~/.garmin-pull/credentials.toml`, owner-readable only.
  Or set `GARMIN_USERNAME` and `GARMIN_PASSWORD` in the environment instead.
- After the first login the tokens are cached in `~/.garmin-pull/session.json` and
  refreshed in place. You type a two-factor code rarely, and Garmin sees one login,
  not one per run. Repeated logins are what triggers their HTTP 429 throttling.
- `python3 scripts/garmin_setup.py --forget` deletes both files.

## Rate limits

The pull pauses one second between days and stops cleanly at the first HTTP 429.
Rerun the same command an hour later: days already on disk are skipped, so it
continues where it stopped. A full year is about 1,500 requests and takes roughly
ten minutes when Garmin is happy.

## Tests

```
python3 -m unittest discover -s tests
```

The OAuth1 signing is checked against RFC 5849's own worked example, and the shaping
functions against captured response shapes, so neither test needs a Garmin account.

## Lessons baked in

This came out of building a personal daily Garmin pull and living with it for a
few weeks. The parts that bit us and are now built in: cache the session or get throttled; stop at the first
429 instead of retrying into a longer ban; checkpoint per day so a stop costs
nothing; never fill a gap with a guess; keep the password in one readable file.
