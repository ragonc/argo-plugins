# garmin-pull

Get your own Garmin Connect data out of the app and into files: sleep, HRV, resting
heart rate, stress, body battery, steps, VO2max and every workout, as daily summaries
or as every single data point the watch recorded during the day. One CSV per
category, one SQLite database, and the raw JSON. Pick only the categories and the
date range you want.

Pure Python standard library, 3.11 or newer. Nothing to install. Talks only to
`sso.garmin.com` and `connectapi.garmin.com`. No Python 3.11 yet? `brew install
python@3.11` on macOS, the installer from python.org on Windows (tick "Add to
PATH"), your package manager on Linux; `python3 --version` tells you what you have.

## Install as a Claude Code plugin

```
/plugin marketplace add ragonc/argo-plugins
/plugin install garmin-pull@argo-plugins
```

Then just ask Claude: "get my Garmin data for the last 3 months, sleep and HRV only".
The `garmin-pull` skill walks you through the one-time login and runs the pull.

## Use it without Claude

```
python3 scripts/garmin_setup.py                     # once: email, password (hidden, not stored), 2FA code if asked
python3 scripts/garmin_pull.py                      # last 30 days, daily summaries -> ./garmin-data
python3 scripts/garmin_pull.py --last 1y --what sleep,hrv,summary
python3 scripts/garmin_pull.py --since 2026-01-01 --until 2026-06-30 --what activities --workout-files
python3 scripts/garmin_pull.py --full-day --last 7d # every data point of each day, see below
python3 scripts/garmin_pull.py --all-history        # from your first Garmin activity to today
```

| flag | meaning |
|---|---|
| `--what LIST` | `summaries` (default: the five daily ones) or any of `sleep`, `hrv`, `summary`, `vo2max`, `activities`, `detail`, comma-separated |
| `--full-day` | every data point the watch stored each day: summaries + `detail` + workout files; with `--what`, adds `detail` and workout files to your list |
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
columns. Any list with a time field and one clear value column becomes a series,
any scalar becomes a snapshot metric, and unknown fields get a generated name
instead of being dropped. When a list has several numeric columns and nothing says
which is the value, it is left out and named in the report rather than guessed: a
plausible wrong number is worse than a gap, and the raw JSON still has it. Two
watches on one account stay apart: the primary device keeps the plain metric names,
the other gets its device id in the name. A shipped `schema_baseline.json` records
the field names seen when this was built; every pull compares and the report says
what Garmin added or removed, so you hear about a change from the report rather
than from an empty column.

Flattening runs one day at a time straight into `garmin.db` and the CSVs, so a
multi-year `--full-day` pull does not need the whole history in memory.

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

The webhook must be `https://`; it is written into the timer unit or launch agent
with owner-only permissions, since anyone holding the URL can post as you.

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

- The setup script asks for your password once, in your terminal, and does not
  store it. What it keeps is the session: `~/.garmin-pull/session.json`, created
  owner-readable only and written atomically. The access token in it lasts an hour
  and is re-minted from a longer-lived token that Garmin keeps valid for about a
  year, so one login and one two-factor code cover roughly a year of pulls. When it
  finally expires, run the setup again. Garmin sees one login, not one per run;
  repeated logins are what triggers their HTTP 429 throttling.
- `--keep-password` stores the password too, in `~/.garmin-pull/credentials.toml`
  (owner-readable only), so that eventual re-login needs no typing. Only worth it on
  an unattended machine. `GARMIN_USERNAME` and `GARMIN_PASSWORD` in the environment
  do the same for automation; they are only read when no usable session exists, and
  an account with two-factor auth still needs a terminal for the code.
- On Windows the permission bits are not enforced; the files live in your user
  profile, which other accounts on the machine cannot read by default.
- `python3 scripts/garmin_setup.py --forget` deletes both files.

## Rate limits

The pull pauses one second between days. At an HTTP 429 it reads Garmin's
Retry-After: up to two minutes it waits that long and tries once more; anything
longer, or no value, and it stops cleanly and prints how long to wait. Rerun the
same command later: days already on disk are skipped, so it continues where it
stopped. An endpoint Garmin refuses outright for a day (a 404, say) is recorded as
"no data" and not asked again; a server error is retried on the next run. A full
year is about 1,500 requests and takes roughly ten minutes when Garmin is happy.

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
nothing; never fill a gap with a guess; keep the password out of files unless asked.
