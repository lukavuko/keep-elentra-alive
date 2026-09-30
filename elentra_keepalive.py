#!/usr/bin/env python3
"""
elentra_keepalive.py — keep a UBC Elentra session from idling out.

You log in ONCE in your browser (CWL + MFA). This script borrows that session
and pokes a cheap authenticated page every ~15 minutes so the server never marks
you idle. It never sees, stores, or transmits your credentials.

A companion Brave extension (in ./brave-extension) watches your Elentra
cookies and POSTs them to this script on 127.0.0.1 whenever you log in, log out,
or if they rotate. This script runs a tiny local listener to receive
them. You just:
    1. Load ./brave-extension once at brave://extensions (Developer mode -> "Load unpacked")
    2. Run:  python elentra_keepalive.py

Start it any time — logged in or out. While no valid session is available
it re-checks every 10s. If the Elentra session dies but your SSO session is
still valid, it asks the extension to silently re-login by loading the Elentra
login URL in a background tab (override with ELENTRA_LOGIN_URL / --login-url).

Usage:
    python elentra_keepalive.py
    python elentra_keepalive.py --listen-port 8765

Requires: requests
"""

from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import logging
import os
import random
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import requests
except ImportError:
    req_file = Path(__file__).resolve().parent / "requirements.txt"
    print(f"Missing dependencies — installing from {req_file} ...", file=sys.stderr)
    subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req_file)], check=True)
    import requests

BASE = "https://ubc.elentra.cloud"
PING_URL = f"{BASE}/dashboard"

IDP_HOST = "authentication.ubc.ca"

DEFAULT_INTERVAL = 900  # keepalive ping cadence once the session is alive
MIN_INTERVAL = 60  # refuse anything faster; avoids hammering Elentra
JITTER = 180  # max jitter (s), clamped to a quarter of the interval
TIMEOUT = 25

ACQUIRE_INTERVAL = 10  # re-check cadence while waiting for a valid session

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _default_cookie_path() -> Path:
    # Keep this out of the project directory — it may live in a synced folder
    # (OneDrive, Dropbox, ...) and this file holds live session cookie values.
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "elentra-keepalive" / "cookies.json"


DEFAULT_COOKIE_PATH = _default_cookie_path()

# Local listener the Brave extension pushes cookies to. Bound to loopback only.
# Keep these two in sync with brave-extension/background.js.
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8765

# Light shared gate. Real safety comes from loopback binding + validating every
# received cookie set against Elentra before adopting it. Change in both files
# together if you like.
PUSH_TOKEN = "elentra-keepalive-local"

# The cookie whose presence means "there is an Elentra app session to keep warm".
SESSION_COOKIE = "entrada-me"

log = logging.getLogger("keepalive")
_stop = False


class _ColorFormatter(logging.Formatter):
    """Colors console log lines by level. Green/cyan read as "good", yellow as
    "waiting/retrying", red as "actually wrong" — matching this script's own
    acquire/alive/error states."""

    COLORS = {
        logging.DEBUG: "\033[2;37m",  # dim gray
        logging.INFO: "\033[32m",  # green
        logging.WARNING: "\033[33m",  # yellow
        logging.ERROR: "\033[31m",  # red
        logging.CRITICAL: "\033[1;31m",  # bold red
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        color = self.COLORS.get(record.levelno)
        return f"{color}{text}{self.RESET}" if color else text


def _enable_windows_ansi() -> bool:
    """Turn on ANSI escape processing in the Windows console. Returns False (and
    leaves color off) if that fails, e.g. output isn't a real console."""
    try:
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = ctypes.windll.kernel32.GetStdHandle(-12)  # STD_ERROR_HANDLE
        mode = ctypes.c_uint32()
        if not ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except Exception:  # noqa: BLE001 — color is a nicety, never worth crashing over
        return False


def _setup_logging(log_file: Path | None) -> None:
    fmt = "%(asctime)s  %(levelname)-7s %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    if log_file:
        # Plain text in the log file — no point coloring something you'll grep/tail.
        logging.basicConfig(level=logging.INFO, format=fmt, datefmt=datefmt, filename=str(log_file))
        return

    use_color = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None
    if use_color and os.name == "nt":
        use_color = _enable_windows_ansi()

    handler = logging.StreamHandler()
    handler.setFormatter(_ColorFormatter(fmt, datefmt) if use_color else logging.Formatter(fmt, datefmt))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def next_delay(interval: int) -> int:
    """Keepalive delay with jitter, clamped so a short --interval can't go negative."""
    jitter = min(JITTER, interval // 4)
    return max(30, interval + random.randint(-jitter, jitter))


def _prevent_sleep() -> None:
    """Ask Windows not to sleep while this process runs."""
    if os.name == "nt":
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        log.info("Sleep prevention is active (SetThreadExecutionState). Remember to close me  or i will keep the system awake.")


_sigint_count = 0


def _handle_signal(signum, _frame):
    global _stop, _sigint_count
    _sigint_count += 1
    if _sigint_count >= 2:
        log.warning("Second signal — forcing immediate exit.")
        os._exit(1)
    _stop = True
    _store.wake()  # break any wait immediately
    log.info("Signal %s received — shutting down after current cycle (press again to force quit).", signum)


# --------------------------------------------------------------------------- #
# Shared store: the listener thread writes the latest pushed cookies here; the
# main loop reads them. `wake()` interrupts the loop's sleep so a fresh login is
# picked up within moments instead of at the next scheduled tick.
# --------------------------------------------------------------------------- #
class CookieStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._cookies: list[dict] | None = None  # list of {name,value,domain,path,...}

    def set(self, cookies: list[dict]) -> None:
        with self._lock:
            changed = cookies_from_objects(cookies) != cookies_from_objects(self._cookies or [])
            self._cookies = cookies
        # The extension re-pushes on a 1-minute alarm (and on every service-worker
        # respawn) even when nothing changed. Only wake the loop early for an
        # actual change (new login, rotation) — otherwise a heartbeat push would
        # cut the keepalive interval down to the push cadence.
        if changed:
            self._event.set()

    def get(self) -> list[dict] | None:
        with self._lock:
            return None if self._cookies is None else list(self._cookies)

    def wake(self) -> None:
        self._event.set()

    def wait(self, timeout: float) -> None:
        """Sleep up to `timeout`s, returning early if new cookies arrive/stop."""
        self._event.wait(timeout)
        self._event.clear()


_store = CookieStore()


def cookies_from_objects(objs: list[dict]) -> dict[str, str]:
    """Flatten a list of cookie objects to a name->value dict (last wins)."""
    out: dict[str, str] = {}
    for c in objs:
        name, value = c.get("name"), c.get("value")
        if name is not None and value is not None:
            out[name] = value
    return out


def build_jar(objs: list[dict]) -> requests.cookies.RequestsCookieJar:
    """Build a domain-scoped cookie jar so each cookie is sent to the right host
    (Elentra app cookies to elentra.cloud, SSO cookies to the IdP on redirect)."""
    jar = requests.cookies.RequestsCookieJar()
    for c in objs:
        name, value = c.get("name"), c.get("value")
        if name is None or value is None:
            continue
        domain = (c.get("domain") or "").lstrip(".")
        path = c.get("path") or "/"
        if domain:
            jar.set(name, value, domain=domain, path=path)
        else:
            jar.set(name, value)
    return jar


# --------------------------------------------------------------------------- #
# Local push listener
# --------------------------------------------------------------------------- #
MAX_PUSH_BYTES = 1 << 20  # 1 MiB is far more than any cookie set needs


class _PushHandler(BaseHTTPRequestHandler):
    def _reject_browser_origin(self) -> bool:
        """The extension fetches us via its host_permissions grant, which carries no
        Origin header (or a chrome-extension:// one). A same-machine web page always
        sends its https:// origin — refuse those so an open tab can't poke us."""
        origin = self.headers.get("Origin")
        if origin and not origin.startswith("chrome-extension://"):
            self.send_response(403)
            self.end_headers()
            return True
        return False

    def do_GET(self):  # noqa: N802 — cheap health check for the extension
        if self._reject_browser_origin():
            return
        if self.path.rstrip("/") == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        if self._reject_browser_origin():
            return
        if self.path.rstrip("/") != "/cookies":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= MAX_PUSH_BYTES:
                raise ValueError(f"bad Content-Length: {length}")
            payload = json.loads(self.rfile.read(length))
        except ValueError:  # covers json.JSONDecodeError too
            self.send_response(400)
            self.end_headers()
            return

        if PUSH_TOKEN and payload.get("token") != PUSH_TOKEN:
            self.send_response(403)
            self.end_headers()
            return

        cookies = payload.get("cookies")
        if not isinstance(cookies, list) or not all(isinstance(c, dict) for c in cookies):
            self.send_response(400)
            self.end_headers()
            return

        _store.set(cookies)
        names = ", ".join(sorted({c.get("name", "?") for c in cookies})) or "(none)"
        log.debug("Push received (%s): %d cookie(s): %s", payload.get("reason", "?"), len(cookies), names)

        self.send_response(200)
        self.end_headers()

    def log_message(self, fmt, *args):  # silence default stderr access log
        return


def start_listener(host: str, port: int) -> ThreadingHTTPServer:
    try:
        server = ThreadingHTTPServer((host, port), _PushHandler)
    except OSError as exc:
        sys.exit(
            f"Can't listen on {host}:{port} — {exc}\n"
            "Is another copy of this script already running? Close it, or pass "
            "--listen-port NNNN (and update PORT in brave-extension/background.js to match)."
        )
    t = threading.Thread(target=server.serve_forever, name="cookie-listener", daemon=True)
    t.start()
    log.info("Listening for cookie pushes on http://%s:%d  (load brave-extension to feed it).", host, port)
    return server


def load_cookie_objects(path: Path) -> list[dict]:
    """Load persisted cookie objects (our own warm-start format), keeping
    domain/path so a rebuilt jar is properly scoped."""
    if not path.exists():
        sys.exit(f"Cookie file not found: {path}")
    data = json.loads(path.read_text())
    if isinstance(data, list):
        return [c for c in data if "name" in c and "value" in c]
    return [{"name": n, "value": v} for n, v in dict(data).items()]


def lock_down(path: Path) -> None:
    """Best-effort permission lockdown on a freshly written cookie file."""
    if os.name == "nt":
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.warning(
                "Couldn't restrict permissions on %s: %s",
                path,
                result.stdout.strip() or result.stderr.strip(),
            )
    else:
        path.chmod(0o600)


# --------------------------------------------------------------------------- #
# Session checks / persistence
# --------------------------------------------------------------------------- #
def check_expected_cookies(cookies: dict[str, str]) -> None:
    """Warn if either half of the dual session is missing."""
    if SESSION_COOKIE not in cookies:
        log.warning("No '%s' cookie — the Elentra app session is missing.", SESSION_COOKIE)
    # if not any(k.startswith("SimpleSAML") for k in cookies):
    #     log.warning(
    #         "No SimpleSAML* cookie — the SSO session is missing, so you'll get "
    #         "bounced to login as soon as Elentra's session rotates."
    #     )


def session_is_alive(resp: requests.Response) -> bool:
    if resp.status_code in (401, 403) or resp.status_code >= 500:
        return False
    if any(IDP_HOST in r.url for r in resp.history) or IDP_HOST in resp.url:
        return False
    if "/login" in resp.url.lower():
        return False

    body = resp.text[:4000].lower()
    if "samlrequest" in body or 'name="j_username"' in body:
        return False
    return True


def persist(objs: list[dict], path: Path | None) -> None:
    """Persist cookie objects (with domain/path) so a warm start can rebuild a
    properly-scoped jar instead of falling back to domainless cookies."""
    if path is None:
        return
    slim = [{k: c[k] for k in ("name", "value", "domain", "path") if k in c} for c in objs]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(slim, indent=2))
    lock_down(path)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": os.environ.get(
                "KEEPALIVE_UA",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    return session


# --------------------------------------------------------------------------- #
# Loops
# --------------------------------------------------------------------------- #
def run_auto_loop(args) -> int:
    """Listener-fed loop: acquire (10s) until a pushed cookie validates, then
    keepalive (interval). Falls back to acquire if the session dies. Never exits
    on a dead session — it waits for the next good push."""
    server = start_listener(LISTEN_HOST, args.listen_port)

    # Warm start: if a previously-persisted cookie file exists, seed it so an
    # already-logged-in user is validated immediately before the extension's first push.
    if args.cookie_file and args.cookie_file.exists():
        try:
            seed = load_cookie_objects(args.cookie_file)
            _store.set(seed)
            log.info("Warm-started from %s (%d cookie(s)); will re-validate.", args.cookie_file, len(seed))
        except Exception as exc:  # noqa: BLE001 — best effort
            log.debug("Warm start skipped: %s", exc)

    session = make_session()
    _prevent_sleep()

    state = "acquiring"
    consecutive_errors = 0
    acquire_ticks = 0
    log.warning("Waiting for a valid Elentra session from Brave (re-checking every %ds)...", ACQUIRE_INTERVAL)

    # Warm start seeds the store, which marks the event "changed" — drain it so
    # we don't fire an extra ping the instant the loop starts.
    _store.wait(0)

    while not _stop:
        objs = _store.get()
        flat = cookies_from_objects(objs) if objs else {}

        if SESSION_COOKIE not in flat:
            # Nothing to validate yet (logged out, or extension not loaded).
            if state == "alive":
                log.warning("Elentra session gone — re-acquiring (checking every %ds).", ACQUIRE_INTERVAL)
                state = "acquiring"
                acquire_ticks = 0
            acquire_ticks += 1
            if acquire_ticks == 1 or acquire_ticks % 6 == 0:  # throttle: ~every 60s
                log.warning("No '%s' cookie yet — log in to Elentra in Brave. Still waiting...", SESSION_COOKIE)
            _store.wait(ACQUIRE_INTERVAL)
            continue

        session.cookies = build_jar(objs)
        try:
            resp = session.get(args.url, timeout=TIMEOUT, allow_redirects=True)
            if args.sso_url:
                session.get(args.sso_url, timeout=TIMEOUT, allow_redirects=True)
        except requests.RequestException as exc:
            consecutive_errors += 1
            log.warning("Request failed (%d in a row): %s", consecutive_errors, exc)
            _store.wait(min(30 * consecutive_errors, 300))
            continue
        consecutive_errors = 0

        if not session_is_alive(resp):
            if state == "alive":
                log.warning("Session expired (bounced to %s) — re-acquiring every %ds.", resp.url, ACQUIRE_INTERVAL)
            else:
                acquire_ticks += 1
                if acquire_ticks == 1 or acquire_ticks % 6 == 0:
                    log.warning("Pushed cookie isn't a live session yet (finish logging in). Still waiting...")
            state = "acquiring"
            _store.wait(ACQUIRE_INTERVAL)
            continue

        # Alive.
        if state != "alive":
            log.info("Session ACQUIRED — logged in. Keepalive active (ping every %ds).", args.interval)
            check_expected_cookies(flat)
            state = "alive"
            acquire_ticks = 0
        else:
            log.info("Session alive (HTTP %s, %d bytes).", resp.status_code, len(resp.content))

        persist(objs, args.cookie_file)
        _store.wait(next_delay(args.interval))  # a fresh push (re-login) wakes us early

    server.shutdown()
    log.info("Stopped cleanly.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cookie-file", type=Path, help="Warm-start/persist path for the cookie JSON")
    ap.add_argument("--listen-port", type=int, default=LISTEN_PORT, help="Local port the extension pushes to")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help=f"Keepalive ping cadence in seconds")
    ap.add_argument("--url", default=PING_URL, help="Authenticated URL to ping")
    ap.add_argument("--sso-url", help="Optional second URL to keep the SSO session warm")
    ap.add_argument("--log-file", type=Path)
    args = ap.parse_args()
    if args.interval < MIN_INTERVAL:
        ap.error(f"--interval must be at least {MIN_INTERVAL} seconds")

    _setup_logging(args.log_file)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if args.cookie_file is None:
        args.cookie_file = DEFAULT_COOKIE_PATH
    return run_auto_loop(args)


if __name__ == "__main__":
    sys.exit(main())
