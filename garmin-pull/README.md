# garmin-pull

Get your own Garmin Connect data out of the app and into files: sleep, HRV, resting
heart rate, stress, body battery, steps, VO2max and every workout. One CSV per
category, one SQLite database, and the raw JSON per day. Pick only the categories and
the date range you want.

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
python3 scripts/garmin_pull.py                      # last 30 days, everything -> ./garmin-data
python3 scripts/garmin_pull.py --last 1y --what sleep,hrv,summary
python3 scripts/garmin_pull.py --since 2026-01-01 --until 2026-06-30 --what activities --workout-files
python3 scripts/garmin_pull.py --everything         # the full drop, see below
```

| flag | meaning |
|---|---|
| `--what LIST` | `all` (default) or any of `sleep`, `hrv`, `summary`, `vo2max`, `activities`, comma-separated |
| `--last PERIOD` | `30d` (default), `12w`, `6m`, `2y` ... |
| `--since DATE` `--until DATE` | an explicit range, `YYYY-MM-DD` |
| `--everything` | full drop: all categories plus workout files, from your first Garmin activity to today |
| `--workout-files` | also save each workout's TCX file (per-second heart rate, pace, GPS) |
| `--out FOLDER` | where to write (default `./garmin-data`) |
| `--format` | `csv`, `sqlite` or both (default both; raw JSON is always written) |
| `--refresh` | re-pull days already on disk |

Before a pull the script prints the range, the categories, and roughly how long it
will take. Anything over about 15 minutes asks for a yes first (`--yes` skips that).

### The full drop

`--everything` first pages through your activity list to find your oldest workout,
then pulls every category from that day to today, plus the TCX file of every
workout. For a watch worn for five years that is around 7,000 requests and well over
an hour, and Garmin will almost certainly rate-limit you partway. That is fine and
expected: the pull stops cleanly, and rerunning the same command later continues
from the last day on disk. Two or three sessions usually get everything.

## What you get

```
garmin-data/
  raw/2026-05-01.json ...   one shaped JSON per day, also the resume checkpoint
  raw/activities.json
  sleep.csv  hrv.csv  summary.csv  vo2max.csv  activities.csv
  garmin.db                 SQLite, one table per category, keyed on date / activity_id
  tcx/<activity_id>.tcx     with --workout-files or --everything
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
