#!/usr/bin/env python3
"""garmin_client.py -- log in to Garmin Connect and read your own wellness and
activity data. Standard library only: nothing to pip install, and every line
that touches your password is in this one file where you can read it.

Hosts contacted, and only these:
  - sso.garmin.com          login form, credential POST, two-factor code
  - connectapi.garmin.com   token exchange and every data endpoint
No other host is ever contacted.

How login works (Garmin's own protocol, the same one the Connect phone app
uses): SSO login -> OAuth1 token -> OAuth2 bearer token. After the first
successful login the tokens are cached in ~/.garmin-pull/session.json
(chmod 600) and reused, refreshed in place when the access token expires.
That matters for two reasons: you only type a two-factor code once in a
long while, and repeated full logins are exactly what triggers Garmin's
HTTP 429 rate limiting.

Credentials, in order of precedence:
  1. GARMIN_USERNAME + GARMIN_PASSWORD in the environment (both required)
  2. ~/.garmin-pull/credentials.toml  ->  username = "..." / password = "..."
     (written for you by garmin_setup.py, chmod 600)
Neither the password nor any token is ever printed, logged, or written to
any output file.

The static OAuth1 consumer key/secret below identifies "the Garmin Connect
app" to Garmin's SSO. It is not tied to any account and is the same value
every open-source implementation of this flow carries; it is hardcoded so
this file never has to fetch anything from a non-Garmin host.

Library use:
    from garmin_client import open_session, fetch_all, save_session
    session = open_session()                 # cached session, or login (prompts for 2FA)
    day = fetch_all(session, "2026-01-31")   # dict: sleep / hrv / daily_summary / vo2max
    save_session(session)

Shaping functions (shape_sleep, shape_hrv, ...) are pure and defensive:
a field Garmin stops sending shows up as None, never as a crash.
"""
from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import hmac
import http.cookiejar
import json
import os
import secrets
import sys
import time
import tomllib  # Python 3.11+
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

HOME_DIR = Path(os.environ.get("GARMIN_PULL_HOME", Path.home() / ".garmin-pull"))
CREDENTIALS_PATH = HOME_DIR / "credentials.toml"
SESSION_CACHE_PATH = Path(os.environ.get("GARMIN_PULL_SESSION_CACHE", HOME_DIR / "session.json"))
SESSION_CACHE_VERSION = 1

SSO_HOST = "sso.garmin.com"
CONNECTAPI_HOST = "connectapi.garmin.com"
MOBILE_INTEGRATION_SERVICE_URL = "https://mobile.integration.garmin.com/gcm/android"
GARMIN_OAUTH_CONSUMER_KEY = "fc3e99d2-118c-44b8-8ae3-03370dde24c0"
GARMIN_OAUTH_CONSUMER_SECRET = "E08WAR897WEy2knn7aFBrvegVAf0AFdWBBF"
CLIENT_ID = "GCM_ANDROID_DARK"
OAUTH_USER_AGENT = "com.garmin.android.apps.connectmobile"
SSO_PAGE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)
FETCH_TIMEOUT_SECONDS = 20
SSO_STATUS_SUCCESSFUL = "SUCCESSFUL"
SSO_STATUS_MFA_REQUIRED = "MFA_REQUIRED"


class GarminConnectError(Exception):
    """Base class for everything this module raises."""


class GarminConfigError(GarminConnectError):
    """Credentials missing or malformed."""


class GarminAuthError(GarminConnectError):
    """Login, two-factor, or token exchange failed."""


class GarminMFARequired(GarminAuthError):
    """SSO returned MFA_REQUIRED and no code was supplied. Pass `state` back
    into GarminSession.submit_mfa(state, code) to finish the login."""

    def __init__(self, state: dict, mfa_method: str):
        self.state = state
        self.mfa_method = mfa_method
        super().__init__(
            f"Garmin account requires a two-factor code (method: {mfa_method}); "
            f"call submit_mfa() with the code to continue"
        )


class GarminAPIError(GarminConnectError):
    """A connectapi.garmin.com call failed after a successful login."""


# --- credentials ---------------------------------------------------------

def load_credentials(path: Path = CREDENTIALS_PATH) -> tuple[str, str]:
    """(username, password) from the environment if both are set, else from
    the credentials file. Never returns a half pair."""
    env_user, env_pass = os.environ.get("GARMIN_USERNAME"), os.environ.get("GARMIN_PASSWORD")
    if env_user and env_pass:
        return env_user, env_pass
    if not path.is_file():
        raise GarminConfigError(
            f"no credentials: set GARMIN_USERNAME and GARMIN_PASSWORD, or run garmin_setup.py "
            f"to create {path}"
        )
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    username, password = data.get("username"), data.get("password")
    if not username or not password:
        raise GarminConfigError(f"{path} must contain username = \"...\" and password = \"...\"")
    return str(username), str(password)


def save_credentials(username: str, password: str, path: Path = CREDENTIALS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f'username = {json.dumps(username)}\npassword = {json.dumps(password)}\n'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(body)
    os.chmod(path, 0o600)


# --- OAuth 1.0a request signing (RFC 5849) --------------------------------
#
# Only two requests in the whole flow need real OAuth1 signing: the
# "preauthorized" token GET and the "exchange" token POST below. Every
# wellness-data call after that is a plain Bearer-token GET (the OAuth2
# access token), which needs no signing at all.

def _percent_encode(value: object) -> str:
    """RFC 3986 unreserved-character percent-encoding, per RFC 5849 3.6.
    Letters, digits and '_.-~' are left alone; everything else (including
    '/') is encoded -- this is stricter than urllib's default `quote`,
    which leaves '/' unencoded unless told not to."""
    return urllib.parse.quote(str(value), safe="")


def _oauth1_signature_base_string(
    method: str,
    base_url: str,
    params: list[tuple[str, str]],
) -> str:
    """Build the OAuth1 signature base string per RFC 5849 3.4.1.

    `params` must already include every query-string param, every
    x-www-form-urlencoded body param (if the request has one), and every
    oauth_* param except oauth_signature itself."""
    encoded_pairs = sorted(
        (_percent_encode(k), _percent_encode(v)) for k, v in params
    )
    normalized = "&".join(f"{k}={v}" for k, v in encoded_pairs)
    return "&".join(
        [method.upper(), _percent_encode(base_url), _percent_encode(normalized)]
    )


def _oauth1_sign(base_string: str, consumer_secret: str, token_secret: str = "") -> str:
    """HMAC-SHA1 sign the base string per RFC 5849 3.4.2, base64-encoded."""
    signing_key = f"{_percent_encode(consumer_secret)}&{_percent_encode(token_secret)}"
    digest = hmac.new(
        signing_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def build_oauth1_authorization_header(
    method: str,
    url: str,
    consumer_key: str,
    consumer_secret: str,
    *,
    token: str | None = None,
    token_secret: str | None = None,
    body_params: dict[str, str] | None = None,
    realm: str | None = None,
) -> str:
    """Build a complete `Authorization: OAuth ...` header value for one
    request, including query-string params already on `url` and any
    x-www-form-urlencoded `body_params` in the signature base string (both
    are required inputs to the signature by RFC 5849, even though only the
    oauth_* params end up in this header)."""
    split = urllib.parse.urlsplit(url)
    base_url = urllib.parse.urlunsplit((split.scheme, split.netloc, split.path, "", ""))
    query_params = urllib.parse.parse_qsl(split.query, keep_blank_values=True)

    oauth_params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_version": "1.0",
    }
    if token:
        oauth_params["oauth_token"] = token

    all_params = list(query_params) + list(oauth_params.items())
    if body_params:
        all_params += list(body_params.items())

    base_string = _oauth1_signature_base_string(method, base_url, all_params)
    oauth_params["oauth_signature"] = _oauth1_sign(
        base_string, consumer_secret, token_secret or ""
    )

    header_params = list(oauth_params.items())
    if realm is not None:
        header_params.insert(0, ("realm", realm))
    parts = [f'{_percent_encode(k)}="{_percent_encode(v)}"' for k, v in header_params]
    return "OAuth " + ", ".join(parts)


# --- tokens ------------------------------------------------------------

@dataclasses.dataclass
class OAuth1Token:
    oauth_token: str
    oauth_token_secret: str
    mfa_token: str | None = None


@dataclasses.dataclass
class OAuth2Token:
    access_token: str
    token_type: str
    refresh_token: str
    expires_at: int  # unix seconds
    refresh_token_expires_at: int  # unix seconds
    scope: str = ""

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    @property
    def refresh_expired(self) -> bool:
        return time.time() >= self.refresh_token_expires_at

    def authorization_header(self) -> str:
        return f"{self.token_type.title()} {self.access_token}"


def _oauth2_token_from_response(raw: dict) -> OAuth2Token:
    now = int(time.time())
    return OAuth2Token(
        access_token=raw["access_token"],
        token_type=raw.get("token_type", "Bearer"),
        refresh_token=raw["refresh_token"],
        expires_at=now + int(raw["expires_in"]),
        refresh_token_expires_at=now + int(raw["refresh_token_expires_in"]),
        scope=raw.get("scope", ""),
    )


# --- session caching (pure I/O helpers -- no network) -----------------------
#
# See "Session caching" in the module docstring for the reasoning. Two
# small, independently-testable functions: save_session() writes exactly
# what's needed to skip a future SSO login, load_session() reconstructs a
# GarminSession from that file (or returns None if there's nothing usable).
# Neither ever touches the network -- that's what makes them testable
# against synthetic data without a real Garmin account.

def _session_cache_dict(session: "GarminSession") -> dict:
    """Shape a logged-in session's tokens for JSON serialization. Raises
    GarminAuthError (not a silent partial write) if the session has no
    tokens yet -- caching a not-yet-logged-in session is a caller bug, not
    a state worth persisting."""
    if session.oauth1_token is None or session.oauth2_token is None:
        raise GarminAuthError("cannot cache a session with no tokens -- call login() first")
    o1, o2 = session.oauth1_token, session.oauth2_token
    return {
        "cache_version": SESSION_CACHE_VERSION,
        "saved_at": int(time.time()),
        # Garmin Connect's internal "userName" (see GarminSession._garmin_username),
        # not the login email -- caching it here saves one extra connectapi
        # call (socialProfile) on every restored session.
        "garmin_username": session._username,
        "oauth1": {
            "oauth_token": o1.oauth_token,
            "oauth_token_secret": o1.oauth_token_secret,
            # oauth1.mfa_token is deliberately NOT cached: it is a one-shot
            # value tied to the login ticket that produced it, not something
            # a later OAuth1->OAuth2 refresh call needs or should resend
            # (refresh_oauth2() calls _exchange_oauth1_for_oauth2 with
            # login=False, which only attaches mfa_token when the OAuth1Token
            # object being used has one -- a freshly-reconstructed one from
            # cache correctly has none, and refresh proceeds without it).
        },
        "oauth2": {
            "access_token": o2.access_token,
            "token_type": o2.token_type,
            "refresh_token": o2.refresh_token,
            "expires_at": o2.expires_at,
            "refresh_token_expires_at": o2.refresh_token_expires_at,
            "scope": o2.scope,
        },
    }


def save_session(session: "GarminSession", path: Path = SESSION_CACHE_PATH) -> None:
    """Write `session`'s OAuth1 + OAuth2 tokens to `path` so a later call
    can skip the SSO/MFA login entirely (see load_session()). Overwrites
    any existing cache file at that path -- the whole point is that it
    reflects the most recent successful login/refresh. chmod 600 after
    writing, same as every other credential-adjacent output in this
    project. Raises GarminAuthError if `session` has never logged in;
    propagates any OSError from the write/chmod itself (caller decides
    whether a caching failure should be fatal -- main() below treats it as
    a warning, not a hard failure, since the fetch already succeeded by the
    time this is called)."""
    data = _session_cache_dict(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def load_session(path: Path = SESSION_CACHE_PATH) -> "GarminSession | None":
    """Return a GarminSession restored from a previous save_session() call,
    or None if there's nothing usable to restore. Returns None (never
    raises) for every one of: no file at `path`; unreadable/corrupt JSON;
    an unrecognized `cache_version`; a missing expected field; OR a refresh
    token that has itself expired (`refresh_token_expires_at` in the past --
    Garmin's hard stop past which only a brand-new SSO login can recover).
    In every case the cache file itself is left on disk untouched, never
    deleted here -- this project never deletes without being asked, and a
    stale/corrupt cache is harmless to leave in place since the next
    save_session() call overwrites it.

    Deliberately does NOT check whether the OAuth2 *access* token itself
    (`expires_at`) has expired -- a cached session in that state is exactly
    the case this caching exists for: GarminSession.connectapi()
    already refreshes an expired access token lazily and automatically
    (pre-existing behavior, unchanged by this function) using the OAuth1
    token restored here, which is a single lightweight request, not a full
    SSO/MFA round trip."""
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("cache_version") != SESSION_CACHE_VERSION:
            return None
        o1_raw = raw["oauth1"]
        o2_raw = raw["oauth2"]
        oauth1_token = OAuth1Token(
            oauth_token=o1_raw["oauth_token"],
            oauth_token_secret=o1_raw["oauth_token_secret"],
        )
        oauth2_token = OAuth2Token(
            access_token=o2_raw["access_token"],
            token_type=o2_raw["token_type"],
            refresh_token=o2_raw["refresh_token"],
            expires_at=o2_raw["expires_at"],
            refresh_token_expires_at=o2_raw["refresh_token_expires_at"],
            scope=o2_raw.get("scope", ""),
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return None

    if oauth2_token.refresh_expired:
        return None

    session = GarminSession()
    session.oauth1_token = oauth1_token
    session.oauth2_token = oauth2_token
    session._username = raw.get("garmin_username")
    return session


# --- HTTP plumbing (stdlib only) -------------------------------------------

class GarminSession:
    """One logged-in (or not-yet-logged-in) session against Garmin Connect.

    Uses a single http.cookiejar-backed opener for the whole session, the
    same way a browser or the reference clients do -- Garmin's SSO relies on
    cookies set during the sign-in page load surviving through the login
    POST and the token exchange."""

    def __init__(self):
        self._cookiejar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookiejar)
        )
        self.oauth1_token: OAuth1Token | None = None
        self.oauth2_token: OAuth2Token | None = None
        self._username: str | None = None  # Garmin Connect "userName", not the login email

    # -- low-level request helper --

    def _request(
        self,
        method: str,
        host: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        json_body: dict | None = None,
        form_body: dict[str, str] | None = None,
    ) -> tuple[int, dict, bytes]:
        url = f"https://{host}{path}"
        if params:
            url += ("&" if "?" in path else "?") + urllib.parse.urlencode(params)

        data = None
        req_headers = dict(headers or {})
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        elif form_body is not None:
            data = urllib.parse.urlencode(form_body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

        request = urllib.request.Request(url, data=data, method=method, headers=req_headers)
        try:
            with self._opener.open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
                body = response.read()
                return response.status, dict(response.headers), body
        except urllib.error.HTTPError as exc:
            body = exc.read()
            return exc.code, dict(exc.headers or {}), body
        except urllib.error.URLError as exc:
            raise GarminConnectError(f"could not reach {host}{path}: {exc}") from exc

    # -- login flow --

    def login(self, username: str, password: str, *, mfa_code: str | None = None) -> None:
        """Log in with username/password. Raises GarminMFARequired if the
        account has two-factor auth enabled and no mfa_code was given --
        catch it, prompt the user, and call submit_mfa(exc.state, code)."""
        login_params = {
            "clientId": CLIENT_ID,
            "locale": "en-US",
            "service": MOBILE_INTEGRATION_SERVICE_URL,
        }

        # Load the sign-in page first -- sets CSRF/session cookies the
        # login POST below depends on, same as a real browser would.
        self._request(
            "GET",
            SSO_HOST,
            "/mobile/sso/en/sign-in",
            params={"clientId": CLIENT_ID},
            headers={"User-Agent": SSO_PAGE_USER_AGENT},
        )

        status, _headers, body = self._request(
            "POST",
            SSO_HOST,
            "/mobile/api/login",
            params=login_params,
            headers={"User-Agent": SSO_PAGE_USER_AGENT},
            json_body={
                "username": username,
                "password": password,
                "rememberMe": False,
                "captchaToken": "",
            },
        )
        resp = self._parse_json(status, body, context="login")
        resp_type = resp.get("responseStatus", {}).get("type")

        if resp_type == SSO_STATUS_MFA_REQUIRED:
            mfa_info = resp.get("customerMfaInfo") or {}
            mfa_method = mfa_info.get("mfaLastMethodUsed") or "email"
            state = {"login_params": login_params}
            if mfa_code is None:
                raise GarminMFARequired(state, mfa_method)
            ticket = self._submit_mfa_code(login_params, mfa_code, mfa_method)
            self._complete_login(ticket)
            return

        if resp_type != SSO_STATUS_SUCCESSFUL:
            message = resp.get("responseStatus", {}).get("message", "")
            raise GarminAuthError(
                f"Garmin login failed: {resp_type or 'unknown response'} {message}".strip()
            )

        ticket = resp["serviceTicketId"]
        self._complete_login(ticket)

    def submit_mfa(self, state: dict, mfa_code: str, mfa_method: str = "email") -> None:
        """Finish a login that raised GarminMFARequired."""
        ticket = self._submit_mfa_code(state["login_params"], mfa_code, mfa_method)
        self._complete_login(ticket)

    def _submit_mfa_code(self, login_params: dict, mfa_code: str, mfa_method: str) -> str:
        status, _headers, body = self._request(
            "POST",
            SSO_HOST,
            "/mobile/api/mfa/verifyCode",
            params=login_params,
            headers={"User-Agent": SSO_PAGE_USER_AGENT},
            json_body={
                "mfaMethod": mfa_method,
                "mfaVerificationCode": mfa_code,
                "rememberMyBrowser": False,
                "reconsentList": [],
                "mfaSetup": False,
            },
        )
        resp = self._parse_json(status, body, context="MFA verification")
        resp_type = resp.get("responseStatus", {}).get("type")
        if resp_type != SSO_STATUS_SUCCESSFUL:
            message = resp.get("responseStatus", {}).get("message", "")
            raise GarminAuthError(f"MFA verification failed: {resp_type} {message}".strip())
        return resp["serviceTicketId"]

    def _complete_login(self, ticket: str) -> None:
        # Best-effort: sets a Cloudflare load-balancer pinning cookie.
        # Never fatal if it fails -- the token exchange below works without it.
        try:
            self._request(
                "GET",
                SSO_HOST,
                "/portal/sso/embed",
                headers={"User-Agent": SSO_PAGE_USER_AGENT},
            )
        except GarminConnectError:
            pass

        self.oauth1_token = self._get_oauth1_token(ticket)
        self.oauth2_token = self._exchange_oauth1_for_oauth2(self.oauth1_token, login=True)

    def _get_oauth1_token(self, ticket: str) -> OAuth1Token:
        path = "/oauth-service/oauth/preauthorized"
        params = {
            "ticket": ticket,
            "login-url": MOBILE_INTEGRATION_SERVICE_URL,
            "accepts-mfa-tokens": "true",
        }
        url = f"https://{CONNECTAPI_HOST}{path}?{urllib.parse.urlencode(params)}"
        auth_header = build_oauth1_authorization_header(
            "GET", url, GARMIN_OAUTH_CONSUMER_KEY, GARMIN_OAUTH_CONSUMER_SECRET
        )
        status, _headers, body = self._request(
            "GET",
            CONNECTAPI_HOST,
            path,
            params=params,
            headers={"User-Agent": OAUTH_USER_AGENT, "Authorization": auth_header},
        )
        if status != 200:
            raise GarminAuthError(
                f"OAuth1 preauthorized token request failed: HTTP {status} {body[:300]!r}"
            )
        parsed = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode("utf-8")).items()}
        if "oauth_token" not in parsed or "oauth_token_secret" not in parsed:
            raise GarminAuthError(f"OAuth1 preauthorized response missing token fields: {parsed}")
        return OAuth1Token(
            oauth_token=parsed["oauth_token"],
            oauth_token_secret=parsed["oauth_token_secret"],
            mfa_token=parsed.get("mfa_token"),
        )

    def _exchange_oauth1_for_oauth2(self, oauth1: OAuth1Token, *, login: bool = False) -> OAuth2Token:
        path = "/oauth-service/oauth/exchange/user/2.0"
        url = f"https://{CONNECTAPI_HOST}{path}"
        body_params: dict[str, str] = {}
        if login:
            body_params["audience"] = "GARMIN_CONNECT_MOBILE_ANDROID_DI"
        if oauth1.mfa_token:
            body_params["mfa_token"] = oauth1.mfa_token

        auth_header = build_oauth1_authorization_header(
            "POST",
            url,
            GARMIN_OAUTH_CONSUMER_KEY,
            GARMIN_OAUTH_CONSUMER_SECRET,
            token=oauth1.oauth_token,
            token_secret=oauth1.oauth_token_secret,
            body_params=body_params,
        )
        status, _headers, body = self._request(
            "POST",
            CONNECTAPI_HOST,
            path,
            headers={"User-Agent": OAUTH_USER_AGENT, "Authorization": auth_header},
            form_body=body_params,
        )
        if status != 200:
            raise GarminAuthError(
                f"OAuth1->OAuth2 token exchange failed: HTTP {status} {body[:300]!r}"
            )
        return _oauth2_token_from_response(self._parse_json(status, body, context="token exchange"))

    def refresh_oauth2(self) -> None:
        if not self.oauth1_token:
            raise GarminAuthError("cannot refresh: not logged in (no OAuth1 token)")
        self.oauth2_token = self._exchange_oauth1_for_oauth2(self.oauth1_token, login=False)

    @staticmethod
    def _parse_json(status: int, body: bytes, *, context: str) -> dict:
        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GarminAuthError(
                f"{context}: HTTP {status}, non-JSON response ({exc})"
            ) from exc

    # -- authenticated data calls --

    def connectapi(self, path: str, *, params: dict[str, str] | None = None) -> dict | list | None:
        """GET a connectapi.garmin.com JSON endpoint with the current
        Bearer token, refreshing it first if expired. This is the only
        method every fetch_* helper below goes through."""
        if not self.oauth2_token:
            raise GarminAuthError("not logged in -- call login() first")
        if self.oauth2_token.expired:
            self.refresh_oauth2()

        status, _headers, body = self._request(
            "GET",
            CONNECTAPI_HOST,
            path,
            params=params,
            headers={"Authorization": self.oauth2_token.authorization_header()},
        )
        if status == 204:
            return None
        if status != 200:
            raise GarminAPIError(f"connectapi {path} failed: HTTP {status} {body[:300]!r}")
        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GarminAPIError(f"connectapi {path}: non-JSON response ({exc})") from exc

    def _garmin_username(self) -> str:
        """Garmin Connect's internal 'userName' (not the login email), used
        as a path segment by the sleep endpoint. Cached per session."""
        if self._username:
            return self._username
        profile = self.connectapi("/userprofile-service/socialProfile")
        if not isinstance(profile, dict) or "userName" not in profile:
            raise GarminAPIError(f"unexpected /userprofile-service/socialProfile response: {profile}")
        self._username = profile["userName"]
        return self._username

    def fetch_sleep(self, day: str) -> dict | None:
        username = self._garmin_username()
        path = f"/wellness-service/wellness/dailySleepData/{urllib.parse.quote(username, safe='')}"
        return self.connectapi(path, params={"date": day, "nonSleepBufferMinutes": "60"})

    def fetch_hrv(self, day: str) -> dict | None:
        return self.connectapi(f"/hrv-service/hrv/{day}")

    def fetch_daily_summary(self, day: str) -> dict | None:
        return self.connectapi("/usersummary-service/usersummary/daily/", params={"calendarDate": day})

    def fetch_vo2max(self, day: str) -> dict | list | None:
        return self.connectapi(f"/metrics-service/metrics/maxmet/daily/{day}/{day}")

    # -- activities (added 2026-09-03, same session/auth machinery as the
    # wellness fetch_* methods above -- see module docstring "Sources
    # consulted" for garth's Activity.list()/Activity.get() path strings and
    # python-garminconnect's download-service paths, both re-checked that
    # day specifically for this addition) --

    def fetch_activities(self, *, start: int = 0, limit: int = 20) -> list:
        """One page of the activity list, newest first. Raw Garmin Connect
        activity summaries -- see shape_activity() for the normalized form.
        Path/params match garth's Activity.list() (activitylist-service,
        start/limit paging) and python-garminconnect's get_activities()."""
        result = self.connectapi(
            "/activitylist-service/activities/search/activities",
            params={"start": str(start), "limit": str(limit)},
        )
        return result if isinstance(result, list) else []

    def fetch_activities_since(
        self, since_date: str, *, page_size: int = 20, max_pages: int = 25
    ) -> list:
        """Page backward through the activity list (newest first, Garmin's
        own order -- never re-sorted here) until an activity's local start
        date is older than `since_date` (YYYY-MM-DD, inclusive), a page
        comes back shorter than `page_size` (no more activities), or
        `max_pages` is hit. The endpoint has no date-range filter of its
        own (confirmed against both reference sources in the module
        docstring -- only start/limit paging), so this is the only way to
        bound a pull by date. `max_pages` is a safety valve, not an
        expected limit: 25 * 20 = 500 activities is far more than a daily
        or weekly catch-up pull should ever need, so hitting it means
        something is wrong (e.g. `since_date` far in the past) rather than
        that this many new activities genuinely exist -- the run stops
        there instead of paging forever."""
        collected: list = []
        start = 0
        for _ in range(max_pages):
            page = self.fetch_activities(start=start, limit=page_size)
            if not page:
                break
            stop = False
            for entry in page:
                day = (entry.get("startTimeLocal") or "")[:10]
                if day and day < since_date:
                    stop = True
                    break
                collected.append(entry)
            if stop or len(page) < page_size:
                break
            start += page_size
        return collected

    def fetch_activity_tcx(self, activity_id: int | str) -> bytes:
        """Raw TCX (XML) bytes for one activity from Garmin's export
        endpoint. Not JSON, so this bypasses connectapi() (which assumes a
        JSON body) and talks to the same host/bearer-token auth directly.
        Path matches python-garminconnect's
        garmin_connect_tcx_download = "/download-service/export/tcx/activity".
        Raises GarminAPIError on anything but HTTP 200."""
        if not self.oauth2_token:
            raise GarminAuthError("not logged in -- call login() first")
        if self.oauth2_token.expired:
            self.refresh_oauth2()
        path = f"/download-service/export/tcx/activity/{urllib.parse.quote(str(activity_id), safe='')}"
        status, _headers, body = self._request(
            "GET",
            CONNECTAPI_HOST,
            path,
            headers={"Authorization": self.oauth2_token.authorization_header()},
        )
        if status != 200:
            raise GarminAPIError(f"connectapi {path} failed: HTTP {status} {body[:300]!r}")
        return body


# --- shaping (pure functions -- no I/O, fully unit-testable) --------------
#
# Field names below come from the reference sources in the module docstring
# (garth's typed data models are the most current/authoritative source
# found). None of this has been checked against a live response -- there is
# no account to check it against yet. Every accessor here is defensive
# (dict.get, returns None on anything missing) specifically because of that:
# a shape mismatch on first real use should show up as a None field to fill
# in, not a crash.

def shape_sleep(raw: dict | None) -> dict:
    """Sleep stages, efficiency, awakenings from a dailySleepData response."""
    if not raw:
        return {}
    dto = raw.get("dailySleepDTO") or {}
    deep = dto.get("deepSleepSeconds")
    light = dto.get("lightSleepSeconds")
    rem = dto.get("remSleepSeconds")
    awake = dto.get("awakeSleepSeconds")
    tracked_parts = [v for v in (deep, light, rem, awake) if v is not None]
    total_sleep = sum(v for v in (deep, light, rem) if v is not None) if any(
        v is not None for v in (deep, light, rem)
    ) else None
    total_tracked = sum(tracked_parts) if tracked_parts else None
    efficiency_pct = (
        round(100 * total_sleep / total_tracked, 1)
        if total_sleep is not None and total_tracked
        else None
    )
    scores = dto.get("sleepScores") or {}
    overall = (scores.get("overall") or {}).get("value")
    # Sleep Need -- CONFIRMED against a real live dailySleepData response on
    # 2026-08-26 (see state/activity-log.md that date's health-dashboard
    # entry), unlike the rest of this function's field names, which per this
    # module's own docstring were sourced from garth's models and never
    # checked against a live account. The real raw shape is
    # dailySleepDTO.sleepNeed = {"baseline": <minutes>, "actual": <minutes>,
    # "feedback": "DECREASED"|"NO_CHANGE"|..., "trainingFeedback": ...,
    # "sleepHistoryAdjustment": ..., "hrvAdjustment": ..., "napAdjustment": ...,
    # ...}. `baseline` is Garmin's default target for this person; `actual` is
    # that day's personalized adjusted target (what the Garmin Connect app
    # itself displays as "Sleep Need") -- NOT how long they actually slept
    # that night (total_sleep_seconds above is the actual-slept figure).
    # `dailySleepDTO.nextSleepNeed` carries the same shape for the *next*
    # night's forecast -- deliberately not extracted here, this function is
    # about the night just described by `raw`.
    sleep_need = dto.get("sleepNeed") or {}
    return {
        "calendar_date": dto.get("calendarDate"),
        "total_sleep_seconds": total_sleep,
        "deep_sleep_seconds": deep,
        "light_sleep_seconds": light,
        "rem_sleep_seconds": rem,
        "awake_sleep_seconds": awake,
        "efficiency_pct": efficiency_pct,
        "awakenings_count": dto.get("awakeCount"),
        "overall_sleep_score": overall,
        "avg_spo2": dto.get("averageSpO2Value"),
        "lowest_spo2": dto.get("lowestSpO2Value"),
        "avg_respiration": dto.get("averageRespirationValue"),
        "avg_sleep_stress": dto.get("avgSleepStress"),
        "sleep_need_baseline_minutes": sleep_need.get("baseline"),
        "sleep_need_actual_minutes": sleep_need.get("actual"),
        "sleep_need_feedback": sleep_need.get("feedback"),
    }


def shape_hrv(raw: dict | None) -> dict:
    """Last-night HRV, weekly avg, personal baseline range, status."""
    if not raw:
        return {}
    summary = raw.get("hrvSummary") or {}
    baseline = summary.get("baseline") or {}
    return {
        "calendar_date": summary.get("calendarDate"),
        "status": summary.get("status"),
        "last_night_avg_ms": summary.get("lastNightAvg"),
        "weekly_avg_ms": summary.get("weeklyAvg"),
        "baseline_low_ms": baseline.get("balancedLow"),
        "baseline_high_ms": baseline.get("balancedUpper"),
    }


def shape_daily_summary(raw: dict | None) -> dict:
    """Resting HR, stress mean, SpO2 mean, steps -- one call covers all
    four (Garmin's per-user daily wellness rollup)."""
    if not raw:
        return {}
    return {
        "calendar_date": raw.get("calendarDate"),
        "resting_hr": raw.get("restingHeartRate"),
        "resting_hr_7d_avg": raw.get("lastSevenDaysAvgRestingHeartRate"),
        "avg_stress": raw.get("averageStressLevel"),
        "max_stress": raw.get("maxStressLevel"),
        "steps": raw.get("totalSteps"),
        "avg_spo2": raw.get("averageSpO2"),
        "lowest_spo2": raw.get("lowestSpO2"),
    }


def shape_vo2max(raw: dict | list | None) -> dict:
    """VO2max for the day, if Garmin published an update. The maxmet
    endpoint returns a list (often one entry); each entry's 'generic' block
    is the running VO2max, per python-garminconnect's use of this same
    endpoint -- least-verified shaping in this module, flagged in the
    module docstring."""
    if not raw:
        return {}
    entries = raw if isinstance(raw, list) else [raw]
    for entry in entries:
        generic = (entry or {}).get("generic") or {}
        vo2max = generic.get("vo2MaxPreciseValue", generic.get("vo2MaxValue"))
        if vo2max is not None:
            return {"calendar_date": generic.get("calendarDate"), "vo2max": vo2max}
    return {}


def shape_activity(raw: dict | None) -> dict:
    """Normalize one raw activity-list entry (see fetch_activities /
    fetch_activities_since) to a flat dict. Field names sourced from
    garth's Activity/Summary dataclasses (camelCase JSON keys behind their
    snake_case aliases) and python-garminconnect's field usage -- same
    "sourced from reference, not yet confirmed against a live account"
    caveat as this module's other shape_* functions carried before
    2026-08-26 (see module docstring); update this comment once checked
    against a real response, same discipline as shape_sleep's sleep_need
    fields above. Every accessor is defensive (dict.get) for the same
    reason: a field-name mismatch on first real use should show up as a
    None value to fill in, not a crash."""
    if not raw:
        return {}
    activity_type = raw.get("activityType") or {}
    return {
        "activity_id": raw.get("activityId"),
        "activity_name": raw.get("activityName"),
        "activity_type": activity_type.get("typeKey"),
        "start_time_local": raw.get("startTimeLocal"),
        "start_time_gmt": raw.get("startTimeGMT"),
        "distance_m": raw.get("distance"),
        "duration_s": raw.get("duration"),
        "elapsed_duration_s": raw.get("elapsedDuration"),
        "moving_duration_s": raw.get("movingDuration"),
        "elevation_gain_m": raw.get("elevationGain"),
        "elevation_loss_m": raw.get("elevationLoss"),
        "average_speed_mps": raw.get("averageSpeed"),
        "max_speed_mps": raw.get("maxSpeed"),
        "calories": raw.get("calories"),
        "average_hr": raw.get("averageHR"),
        "max_hr": raw.get("maxHR"),
        "steps": raw.get("steps"),
        "average_power": raw.get("avgPower"),
        "max_power": raw.get("maxPower"),
        "aerobic_training_effect": raw.get("aerobicTrainingEffect"),
        "anaerobic_training_effect": raw.get("anaerobicTrainingEffect"),
        "vo2max_value": raw.get("vO2MaxValue"),
    }


def fetch_all(session: GarminSession, day: str) -> dict:
    """Fetch and shape every wellness metric for one day. Raises
    GarminAPIError/GarminAuthError on any failure -- never returns a
    partial result silently; the caller sees exactly what failed."""
    return {
        "date": day,
        "sleep": shape_sleep(session.fetch_sleep(day)),
        "hrv": shape_hrv(session.fetch_hrv(day)),
        "daily_summary": shape_daily_summary(session.fetch_daily_summary(day)),
        "vo2max": shape_vo2max(session.fetch_vo2max(day)),
        # Skin temperature deviation has NO confirmed connectapi endpoint --
        # neither garth nor python-garminconnect (the two references
        # consulted) expose one. Not guessed at; see module docstring.
        "skin_temp_deviation": None,
    }




def open_session(*, mfa_code: str | None = None, use_cache: bool = True,
                 cache_path: Path = SESSION_CACHE_PATH, interactive: bool | None = None,
                 log=None) -> GarminSession:
    """The one call scripts should use: cached session if usable, else a
    fresh login. Two-factor: uses `mfa_code` if given, otherwise prompts on
    the terminal when interactive, otherwise raises GarminMFARequired with a
    clear message. Saves the (possibly refreshed) session on success."""
    log = log or (lambda msg: print(msg, file=sys.stderr))
    if interactive is None:
        interactive = sys.stdin.isatty()
    if use_cache:
        session = load_session(cache_path)
        if session is not None:
            log(f"garmin: reusing cached session ({cache_path})")
            return session
    username, password = load_credentials()
    session = GarminSession()
    try:
        session.login(username, password, mfa_code=mfa_code)
    except GarminMFARequired as exc:
        if not interactive:
            raise GarminAuthError(
                f"two-factor code needed ({exc.mfa_method}) but no terminal to ask on -- "
                f"run garmin_setup.py once interactively, or pass --mfa-code"
            ) from exc
        code = input(f"Garmin two-factor code ({exc.mfa_method}): ").strip()
        session.submit_mfa(exc.state, code, exc.mfa_method)
    if use_cache:
        try:
            save_session(session, cache_path)
            log(f"garmin: session cached -> {cache_path}")
        except OSError as exc:
            log(f"garmin: warning, could not cache session: {exc}")
    return session
