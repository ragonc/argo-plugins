---
name: garmin-pull
description: Pull a person's own Garmin Connect training and wellness data (sleep, HRV, resting heart rate, stress, body battery, steps, VO2max, activities) into CSV, SQLite and JSON files, with a guided first-time setup and the option to pull only some categories or a date range. Use whenever someone wants their Garmin data out of the app and into files they can analyse.
---

# Garmin pull

You are helping someone get their own Garmin data onto their computer. Assume they
have never done this before. Keep every message short, one step at a time, and never
ask for their password: the setup script asks for it privately in their terminal.

All commands below live in `${CLAUDE_PLUGIN_ROOT}/scripts/`. Python 3.11 or newer is
required, nothing to install.

## Step 0: what do they want?

Before any command, settle three things. Infer what you can from their message; ask
the rest in one short message with defaults offered:

1. **Which data.** Offer the five categories in plain words:
   - sleep (stages, score, efficiency, SpO2 during sleep)
   - hrv (heart rate variability, nightly and weekly)
   - summary (resting heart rate, stress, body battery, steps, calories, intensity minutes)
   - vo2max
   - activities (every workout: distance, duration, heart rate, power, training effect)
   Default: all five daily summaries. "Just my sleep and HRV" becomes `--what sleep,hrv`.
   "Everything the watch recorded", "all data points", "the intraday data" means
   `--full-day`: the summaries plus every timestamped measurement of each day (heart
   rate, stress, body battery, steps, breathing, SpO2, HRV, sleep stages) and the day's
   readiness, hydration, training status, endurance and fitness age, plus workout
   files. About four times the cost of the summaries; say so for long ranges.
2. **Which period.** Default: last 30 days (`--last 30d`; also `12w`, `6m`, `2y`, or
   `--since`/`--until` dates). "All my history" means `--all-history`: from their first
   Garmin activity to today. Warn that it is long (often over an hour, in two or three
   sessions because Garmin rate-limits) before starting. A year of summaries takes
   about 10 minutes; say so.
3. **Where.** Default: a `garmin-data` folder in the current directory.

## Step 1: check setup

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/garmin_setup.py --check
```

If it says "not set up" or "login failed", tell them to run this **themselves, in their
own terminal window**, because it asks for their Garmin email and password privately:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/garmin_setup.py
```

Explain in one line what it does: stores the login on their computer only, readable
only by them, logs in once, and may ask for a two-factor code that Garmin emails them.
Wait until they say it worked, then run `--check` again. Do not try to run the setup
for them and do not ask them to paste the password into the chat.

## Step 2: pull

Build one command from step 0, show it, run it. Examples:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/garmin_pull.py --last 30d
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/garmin_pull.py --last 1y --what sleep,hrv,summary
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/garmin_pull.py --since 2026-01-01 --until 2026-06-30 --what activities --workout-files
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/garmin_pull.py --full-day --last 7d
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/garmin_pull.py --all-history --yes
```

Use `--out <folder>` when they named a place. Add `--workout-files` only if they want
the raw per-second workout files. The script prints an estimate before it starts and
asks for confirmation over 15 minutes; pass `--yes` since you cannot answer a prompt,
and tell them the estimate yourself. Run long pulls with a generous timeout.
For `--all-history`, expect it to stop on a rate limit partway: report where it got to
and that rerunning the same command an hour later continues from there.

## Step 3: report

Read the last lines of the script's output and tell them, in a few sentences:
- what was pulled (days and categories) and where the files are
- the three files most people want: the CSVs, `garmin.db`, and `raw/` for JSON; with
  `--full-day`, point at `timeline.csv` (one row per measurement, `series` says which)
- any days that failed and why (the script records them, it never fills gaps)

If the run stopped with a rate-limit message (exit code 3), say plainly that Garmin
throttled the connection, nothing is lost, and rerunning the same command in about an
hour continues from where it stopped. Do not retry in a loop.

## Answering questions afterwards

Once the data is on disk, answer questions from the files, not from Garmin: `garmin.db`
has one table per category with `date` as the key (`activity_id` for activities;
`timeline` is keyed on date, series and time_gmt). One
`sqlite3` or Python query per question is enough. Empty cells mean Garmin did not
provide that metric for that day, usually because the watch was not worn or the
feature is not on their device. Say that rather than guessing.

## Things that go wrong

- **"two-factor code needed but no terminal to ask on"**: the session expired. They
  run the setup script again in their terminal.
- **HTTP 429**: throttled, see above. Repeated full logins cause it, which is why the
  session is cached; never add `--no-cache` to work around a problem.
- **A field is always empty**: Garmin renamed or dropped it. The shaping functions in
  `garmin_client.py` map field names; read the raw JSON for that day to see what is
  actually there before changing anything.
- **Wrong Python**: the scripts need 3.11+. `python3 --version` tells them.

## Never

- never ask for, print, or store the password anywhere except through the setup script
- never invent a value for a missing day or metric
- never contact any host other than sso.garmin.com and connectapi.garmin.com
