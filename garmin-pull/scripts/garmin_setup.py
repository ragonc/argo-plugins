#!/usr/bin/env python3
"""garmin_setup.py -- one-time setup, run it in your own terminal:

    python3 garmin_setup.py

It asks for your Garmin Connect email and password (typed hidden, never echoed,
never shown to Claude), logs in once (asking for a two-factor code if your
account has one), and caches the session in ~/.garmin-pull/session.json with
owner-only permissions. That session lasts about a year, so the password is
not kept: when it finally expires, run this again.

    python3 garmin_setup.py --keep-password   # also store the password (0600) so
                                              # a future re-login needs no typing;
                                              # only useful for unattended machines
    python3 garmin_setup.py --check           # just test the cached session / credentials
    python3 garmin_setup.py --forget          # delete stored credentials and session

Nothing is sent anywhere except sso.garmin.com and connectapi.garmin.com.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from garmin_client import (  # noqa: E402
    CREDENTIALS_PATH,
    SESSION_CACHE_PATH,
    GarminConnectError,
    load_credentials,
    open_session,
    save_credentials,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--forget", action="store_true")
    parser.add_argument("--keep-password", action="store_true",
                        help="store the password too (owner-readable file); default is session only")
    args = parser.parse_args()

    if args.forget:
        for path in (CREDENTIALS_PATH, SESSION_CACHE_PATH):
            if path.exists():
                path.unlink()
                print(f"removed {path}")
        return 0

    credentials = None
    if not args.check:
        if not sys.stdin.isatty():
            print("run this in a terminal: it needs to ask for your password privately", file=sys.stderr)
            return 2
        print("Garmin Connect login (used once, on this computer; the password is "
              + ("stored owner-readable only" if args.keep_password else "not stored") + ")")
        username = input("email: ").strip()
        password = getpass.getpass("password (hidden): ")
        if not username or not password:
            print("both are needed", file=sys.stderr)
            return 2
        credentials = (username, password)
        if args.keep_password:
            save_credentials(username, password)
            print(f"saved -> {CREDENTIALS_PATH}")
        if SESSION_CACHE_PATH.exists():
            SESSION_CACHE_PATH.unlink()
    else:
        if not SESSION_CACHE_PATH.exists():
            try:
                load_credentials()
            except GarminConnectError as exc:
                print(f"not set up: {exc}", file=sys.stderr)
                return 1

    try:
        session = open_session(interactive=True, credentials=credentials)
        profile = session.connectapi("/userprofile-service/socialProfile")
        name = (profile or {}).get("displayName") or (profile or {}).get("userName") or "?"
    except GarminConnectError as exc:
        print(f"login failed: {exc}", file=sys.stderr)
        return 1
    print(f"logged in as {name}; session cached at {SESSION_CACHE_PATH} (good for about a year)")
    print("next: python3 garmin_pull.py --last 30d")
    return 0


if __name__ == "__main__":
    sys.exit(main())
