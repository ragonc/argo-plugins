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
python3 scripts/garmin_pull.py --days 30            # everything, last 30 days -> ./garmin-data
python3 scripts/garmin_pull.py --days 365 --only sleep,hrv,summary
python3 scripts/garmin_pull.py --from 2026-01-01 --to 2026-06-30 --only activities --tcx
```

| flag | meaning |
|---|---|
| `--only a,b` | any of `sleep`, `hrv`, `summary`, `vo2max`, `activities` (default all) |
| `--days N` / `--from` `--to` | date range (default last 30 days) |
| `--out DIR` | output folder (default `./garmin-data`) |
| `--format` | `csv`, `sqlite`, `json` in any combination (JSON always kept) |
| `--tcx` | also save each workout's TCX file |
| `--refresh` | re-pull days already on disk |

## What you get

```
garmin-data/
  raw/2026-05-01.json ...   one shaped JSON per day, also the resume checkpoint
  raw/activities.json
  sleep.csv  hrv.csv  summary.csv  vo2max.csv  activities.csv
  garmin.db                 SQLite, one table per category, keyed on date / activity_id
  tcx/<activity_id>.tcx     with --tcx
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

This came out of running a personal daily Garmin pull for months. The parts that
bit us and are now built in: cache the session or get throttled; stop at the first
429 instead of retrying into a longer ban; checkpoint per day so a stop costs
nothing; never fill a gap with a guess; keep the password in one readable file.
