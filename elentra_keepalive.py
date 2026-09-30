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
import time
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
# Loaded in a background browser tab to silently re-login while the SSO session
# is still valid. Must stay on BASE — the extension refuses any other origin.
LOGIN_URL = os.environ.get("ELENTRA_LOGIN_URL", f"{BASE}/")

IDP_HOST = "authentication.ubc.ca"

DEFAULT_INTERVAL = 900  # keepalive ping cadence once the session is alive
MIN_INTERVAL = 60  # refuse anything faster; avoids hammering Elentra
JITTER = 180  # max jitter (s), clamped to a quarter of the interval
TIMEOUT = 25

ACQUIRE_INTERVAL = 10  # re-check cadence while waiting for a valid session
# How long to give the extension to pick up a refresh request (next 1-min push)
# and finish the background re-login before asking the user to log in.
REFRESH_WINDOW = 150

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


class _ConsoleHandler(logging.StreamHandler):
    """In a real terminal, keeps one status line pinned below the log so routine
    pings update it in place instead of scrolling a new line every time."""

    def __init__(self, live: bool) -> None:
        super().__init__()
        self.live = live
        self._status = ""

    def emit(self, record: logging.LogRecord) -> None:
        if self._status:
            self.stream.write("\r\033[K")
        super().emit(record)
        if self._status:
            self.stream.write(self._status)
            self.flush()

    def set_status(self, text: str) -> None:
        self.acquire()
        try:
            self.stream.write("\r\033[K" + text)
            self.flush()
            self._status = text
        finally:
            self.release()

    def clear_screen(self) -> None:
        self.acquire()
        try:
            self.stream.write("\033[2J\033[3J\033[H")  # screen + scrollback, cursor home
            self.flush()
            self._status = ""
        finally:
            self.release()


_console: _ConsoleHandler | None = None


def _setup_logging(log_file: Path | None) -> None:
    global _console

    if log_file:
        # Plain text in the log file — no point coloring something you'll grep/tail.
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s  %(levelname)-7s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            filename=str(log_file),
            encoding="utf-8",
        )
        return

    ansi = sys.stderr.isatty() and (os.name != "nt" or _enable_windows_ansi())
    use_color = ansi and os.environ.get("NO_COLOR") is None

    _console = _ConsoleHandler(live=ansi)
    if use_color:
        # Color already conveys the level, so drop the level name.
        _console.setFormatter(_ColorFormatter("%(asctime)s  %(message)s", "%H:%M:%S"))
    else:
        _console.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=[_console])


def _status(text: str) -> None:
    """Routine, repetitive state: a pinned line in a terminal, a log line elsewhere."""
    if _console and _console.live:
        _console.set_status(text)
    elif text:
        log.info(text)


def _clear_screen() -> None:
    if _console and _console.live:
        _console.clear_screen()


def next_delay(interval: int) -> int:
    """Keepalive delay with jitter, clamped so a short --interval can't go negative."""
    jitter = min(JITTER, interval // 4)
    return max(30, interval + random.randint(-jitter, jitter))


def _prevent_sleep() -> None:
    """Ask Windows not to sleep while this process runs."""
    if os.name == "nt":
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        log.info("Keeping this PC awake while running — close me when you're done.")


_sigint_count = 0


def _handle_signal(signum, _frame):
    global _stop, _sigint_count
    _sigint_count += 1
    if _sigint_count >= 2:
        log.warning("Second signal — forcing immediate exit.")
        os._exit(1)
    _stop = True
    _store.wake()  # break any wait immediately
    log.info("Stopping... (press Ctrl+C again to force quit)")


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
        self._action: dict | None = None  # one-shot instruction for the extension
        self.state = "waiting"  # reported to the extension for its toolbar badge

    def request_refresh(self, url: str) -> None:
        with self._lock:
            self._action = {"type": "refresh", "url": url}

    def take_action(self) -> dict | None:
        with self._lock:
            action, self._action = self._action, None
            return action

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

        reply = {"state": _store.state}
        action = _store.take_action()
        if action:
            reply["action"] = action
        body = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
    log.info("Listening for the browser extension on %s:%d.", host, port)
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
            log.debug("Warm-started from %s (%d cookie(s)); will re-validate.", args.cookie_file, len(seed))
        except Exception as exc:  # noqa: BLE001 — best effort
            log.debug("Warm start skipped: %s", exc)

    session = make_session()
    _prevent_sleep()

    state = ""  # "" (just started), "alive", or "waiting"
    errors = 0
    refresh_at: float | None = None  # when this waiting spell's silent refresh was requested
    last_note = ""

    def note(level: int, msg: str) -> None:
        """Log only when the message changes, so waiting doesn't repeat itself."""
        nonlocal last_note
        if msg != last_note:
            log.log(level, msg)
            last_note = msg

    # Warm start seeds the store, which marks the event "changed" — drain it so
    # we don't fire an extra ping the instant the loop starts.
    _store.wait(0)

    while not _stop:
        objs = _store.get()
        flat = cookies_from_objects(objs) if objs else {}

        alive = False
        if SESSION_COOKIE in flat:
            # Rebuilt from the browser's cookies every time (dropping anything the
            # server set on our last request): the point is to keep the *browser's*
            # session warm, not to drift onto a session only this script holds.
            session.cookies = build_jar(objs)
            try:
                resp = session.get(args.url, timeout=TIMEOUT, allow_redirects=True)
                if args.sso_url:
                    session.get(args.sso_url, timeout=TIMEOUT, allow_redirects=True)
            except requests.RequestException as exc:
                errors += 1
                delay = min(30 * errors, 300)
                if errors == 1:
                    log.warning("Can't reach Elentra (%s) — retrying.", type(exc).__name__)
                else:
                    _status(f"Can't reach Elentra — {errors} failed attempts, retrying in {delay}s")
                _store.wait(delay)
                continue
            if errors:
                log.info("Connection to Elentra restored.")
                errors = 0
            alive = session_is_alive(resp)

        if alive:
            if state != "alive":
                _clear_screen()
                log.info("Logged in to Elentra — keepalive active, pinging every ~%d min.", round(args.interval / 60))
                state = _store.state = "alive"
                refresh_at = None
                last_note = ""
            persist(objs, args.cookie_file)
            delay = next_delay(args.interval)
            _status(f"Session alive · last ping {time.strftime('%H:%M')} · next ~{time.strftime('%H:%M', time.localtime(time.time() + delay))}")
            _store.wait(delay)  # a fresh push (re-login) wakes us early
            continue

        if state == "alive":
            _status("")
            note(logging.WARNING, "Elentra session expired." if SESSION_COOKIE in flat else "Logged out of Elentra in the browser.")
        state = _store.state = "waiting"

        if not objs:
            note(logging.WARNING, "Waiting for the browser extension — log in to Elentra in Brave.")
        elif refresh_at is None:
            # Once per waiting spell: if the SSO session is still valid, loading
            # Elentra in a background tab logs back in without a password or MFA.
            _store.request_refresh(args.login_url)
            refresh_at = time.monotonic()
            note(logging.WARNING, "Not logged in — trying a silent re-login in the background...")
        elif time.monotonic() - refresh_at > REFRESH_WINDOW:
            note(logging.WARNING, "Silent re-login didn't work — please log in to Elentra in Brave.")
        _store.wait(ACQUIRE_INTERVAL)

    server.shutdown()
    _status("")
    log.info("Stopped.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cookie-file", type=Path, help="Warm-start/persist path for the cookie JSON")
    ap.add_argument("--listen-port", type=int, default=LISTEN_PORT, help="Local port the extension pushes to")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help=f"Keepalive ping cadence in seconds")
    ap.add_argument("--url", default=PING_URL, help="Authenticated URL to ping")
    ap.add_argument("--sso-url", help="Optional second URL to keep the SSO session warm")
    ap.add_argument("--login-url", default=LOGIN_URL, help="Elentra URL the extension loads to silently re-login (env: ELENTRA_LOGIN_URL)")
    ap.add_argument("--log-file", type=Path)
    args = ap.parse_args()
    if args.interval < MIN_INTERVAL:
        ap.error(f"--interval must be at least {MIN_INTERVAL} seconds")
    if not args.login_url.startswith(f"{BASE}/"):
        ap.error(f"--login-url / ELENTRA_LOGIN_URL must start with {BASE}/")

    _setup_logging(args.log_file)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if args.cookie_file is None:
        args.cookie_file = DEFAULT_COOKIE_PATH
    return run_auto_loop(args)


if __name__ == "__main__":
    sys.exit(main())
