#!/usr/bin/env python3
"""garmin_canary.py -- a weekly "does it still work?" check you run on your own machine.

Garmin changes its login flow and response shapes now and then, and when that
happens every tool of this kind breaks at once. This check notices before you
need the data: it restores your cached session, fetches yesterday's summaries and
every detail endpoint, flattens them, and compares the field names with the
plugin's baseline.

    python3 garmin_canary.py                 # run once, print the verdict
    python3 garmin_canary.py --webhook URL   # also POST the verdict when something is wrong
    python3 garmin_canary.py --install       # schedule it weekly on this machine (Linux / macOS)
    python3 garmin_canary.py --uninstall

Exit codes: 0 healthy, 2 Garmin changed something (pull still works), 1 broken.
The webhook gets a JSON body {"content": "..."} -- what Discord and most chat
webhooks accept -- and only when the exit code is not 0. Set it once with the
environment variable GARMIN_PULL_WEBHOOK instead of the flag if you prefer.
Nothing is sent anywhere except Garmin and, on failure, your own webhook.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import garmin_detail as gd  # noqa: E402
from garmin_client import SESSION_CACHE_PATH, GarminConnectError, load_session, save_session  # noqa: E402
from garmin_pull import fetch_day  # noqa: E402

HEALTHY, CHANGED, BROKEN = 0, 2, 1
LOG_PATH = Path(os.environ.get("GARMIN_PULL_HOME", Path.home() / ".garmin-pull")) / "canary.log"


def assess(summary: dict, status: dict[str, str], timeline: list[dict], drift: dict) -> tuple[int, str]:
    """Turn what the run saw into (exit code, one-line verdict)."""
    failed = [k for k, v in status.items() if str(v).startswith("error")]
    if failed:
        return BROKEN, f"detail endpoints failing: {', '.join(failed)} ({status[failed[0]]})"
    empty = [k for k in ("sleep", "hrv", "summary") if not summary.get(k)]
    series = {r["series"] for r in timeline}
    if len(series) < 12 or len(timeline) < 500:
        return BROKEN, (f"flattening looks thin: {len(timeline)} rows in {len(series)} series "
                        f"(expected about 20 series and a few thousand rows)")
    if drift:
        parts = []
        for endpoint, change in drift.items():
            if endpoint == "unmapped":
                parts.append(f"{len(change)} list(s) not flattened")
                continue
            if change.get("added"):
                parts.append(f"{endpoint} +{','.join(change['added'][:4])}")
            if change.get("missing"):
                parts.append(f"{endpoint} -{','.join(change['missing'][:4])}")
        return CHANGED, "Garmin changed response fields, pull still works: " + "; ".join(parts)
    note = f", no data for {', '.join(empty)} yesterday (watch not worn?)" if empty else ""
    return HEALTHY, f"healthy: {len(timeline)} measurements in {len(series)} series{note}"


def run() -> tuple[int, str]:
    session = load_session(SESSION_CACHE_PATH)
    if session is None:
        return BROKEN, f"cached session at {SESSION_CACHE_PATH} is not usable; run garmin_setup.py in a terminal"
    day = (date.today() - timedelta(days=1)).isoformat()
    scratch = Path(tempfile.mkdtemp(prefix="garmin-canary-"))
    try:
        try:
            summary = fetch_day(session, day, {"sleep", "hrv", "summary", "vo2max"})
        except GarminConnectError as exc:
            return BROKEN, f"summary fetch for {day} failed: {str(exc)[:200]}"
        try:
            status = gd.fetch_detail(session, day, scratch / day, pause=0.3)
        except GarminConnectError as exc:
            return BROKEN, f"detail fetch for {day} failed: {str(exc)[:200]}"
        raw = gd.load_detail_day(scratch / day)
        timeline = gd.flatten_timeline(day, raw)
        drift = gd.schema_drift(raw)
        code, verdict = assess(summary, status, timeline, drift)
        return code, f"{day}: {verdict}"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        try:
            save_session(session, SESSION_CACHE_PATH)
        except OSError:
            pass


def valid_webhook(url: str | None) -> str | None:
    """Only https URLs: the verdict text names your Garmin data shapes and
    the machine, and must not travel in clear."""
    if url is None:
        return None
    if not url.lower().startswith("https://"):
        raise SystemExit(f"--webhook must be an https:// URL, not {url[:40]!r}")
    return url


def post_webhook(url: str, text: str) -> bool:
    try:
        body = json.dumps({"content": f"garmin-pull canary: {text}"[:1900]}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20):
            return True
    except Exception as exc:  # noqa: BLE001
        print(f"webhook failed: {type(exc).__name__}", file=sys.stderr)
        return False


def append_log(code: int, text: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M')} exit={code} {text}\n")
    except OSError:
        pass


# --- scheduling on the user's own machine --------------------------------------------

def _python() -> str:
    return sys.executable or "python3"


def _write_private(path: Path, text: str) -> None:
    """Created 0600 and renamed into place: the unit/plist may carry the
    webhook URL, which is a secret (anyone holding it can post as you)."""
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def install(webhook: str | None) -> int:
    script = Path(__file__).resolve()
    system = platform.system()
    env_line = f"Environment=GARMIN_PULL_WEBHOOK={webhook}\n" if webhook else ""
    if system == "Linux" and shutil.which("systemctl"):
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        _write_private(unit_dir / "garmin-pull-canary.service",
            "[Unit]\nDescription=garmin-pull weekly check\n\n[Service]\nType=oneshot\n"
            f"{env_line}ExecStart={_python()} {script}\n")
        (unit_dir / "garmin-pull-canary.timer").write_text(
            "[Unit]\nDescription=garmin-pull weekly check\n\n[Timer]\nOnCalendar=Mon *-*-* 08:15:00\n"
            "Persistent=true\nRandomizedDelaySec=10min\n\n[Install]\nWantedBy=timers.target\n")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        result = subprocess.run(["systemctl", "--user", "enable", "--now", "garmin-pull-canary.timer"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            print(f"could not enable the timer: {result.stderr.strip()}")
            return 1
        print("scheduled: every Monday 08:15 via systemd user timer garmin-pull-canary.timer")
        print("check with: systemctl --user list-timers garmin-pull-canary.timer")
        return 0
    if system == "Darwin":
        agents = Path.home() / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        plist = agents / "com.garmin-pull.canary.plist"
        env_xml = (f"<key>EnvironmentVariables</key><dict><key>GARMIN_PULL_WEBHOOK</key>"
                   f"<string>{webhook}</string></dict>" if webhook else "")
        _write_private(plist, f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.garmin-pull.canary</string>
<key>ProgramArguments</key><array><string>{_python()}</string><string>{script}</string></array>
<key>StartCalendarInterval</key><dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>15</integer></dict>
{env_xml}
<key>StandardOutPath</key><string>{LOG_PATH.with_name('canary.out')}</string>
<key>StandardErrorPath</key><string>{LOG_PATH.with_name('canary.err')}</string>
</dict></plist>
""")
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        result = subprocess.run(["launchctl", "load", str(plist)], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"could not load the launch agent: {result.stderr.strip()}")
            return 1
        print(f"scheduled: every Monday 08:15 via launchd ({plist})")
        return 0
    print("automatic scheduling is set up for Linux (systemd) and macOS (launchd).")
    print("On Windows, open Task Scheduler and create a weekly task running:")
    print(f'  "{_python()}" "{script}"')
    if webhook:
        print(f"  with the environment variable GARMIN_PULL_WEBHOOK={webhook}")
    return 1


def uninstall() -> int:
    system = platform.system()
    if system == "Linux" and shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "disable", "--now", "garmin-pull-canary.timer"], capture_output=True)
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        for name in ("garmin-pull-canary.timer", "garmin-pull-canary.service"):
            path = unit_dir / name
            if path.exists():
                path.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        print("removed the systemd user timer")
        return 0
    if system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.garmin-pull.canary.plist"
        if plist.exists():
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
            plist.unlink()
        print("removed the launch agent")
        return 0
    print("nothing to remove automatically on this platform; delete the scheduled task by hand")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--webhook", default=os.environ.get("GARMIN_PULL_WEBHOOK"), metavar="URL",
                        help="POST the verdict here when it is not healthy (Discord/Slack-style JSON)")
    parser.add_argument("--install", action="store_true", help="schedule weekly on this machine")
    parser.add_argument("--uninstall", action="store_true", help="remove the schedule")
    args = parser.parse_args()
    args.webhook = valid_webhook(args.webhook)
    if args.uninstall:
        return uninstall()
    if args.install:
        return install(args.webhook)
    code, text = run()
    print(text)
    append_log(code, text)
    if code != HEALTHY and args.webhook:
        post_webhook(args.webhook, text)
    return code


if __name__ == "__main__":
    sys.exit(main())
