#!/usr/bin/env python3
"""
elentra_keepalive.py — keep a UBC Elentra session from idling out.

You log in once in your browser. This script borrows that session and pokes a
cheap authenticated page every ~15 minutes so the server never marks you idle.

A companion browser extension (./brave-extension) pushes your Elentra cookies to
this script on 127.0.0.1 whenever they change. This script never contacts the
CWL login server itself: a ping that Elentra redirects there just means
"logged out", and logging back in is left to the browser.

If the session dies (e.g. after the laptop sleeps), the script asks the
extension to re-login in a background tab. If CWL asks for a password there,
the extension fills it in from your OS keychain — only if you saved one with
--set-login.

Usage:
    python elentra_keepalive.py                 # run
    python elentra_keepalive.py --set-login     # save your CWL login to the OS keychain
    python elentra_keepalive.py --forget-login  # remove it
    python elentra_keepalive.py --debug         # verbose console output

Logs (always at debug detail, rotated daily, kept 14 days):
    %LOCALAPPDATA%\\elentra-keepalive\\logs   (Windows)
    ~/.local/state/elentra-keepalive/logs   (macOS/Linux)

Requires: requests, keyring
"""

from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import logging
import logging.handlers
import os
import random
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlsplit

try:
    import keyring
    import requests
except ImportError:
    req_file = Path(__file__).resolve().parent / "requirements.txt"
    print(f"Missing dependencies — installing from {req_file} ...", file=sys.stderr)
    if subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req_file)]).returncode != 0:
        # Typically Homebrew/system Python on macOS refusing pip installs (PEP 668).
        sys.exit(
            "\nCouldn't install the dependencies automatically.\n"
            "Install Python from https://www.python.org/downloads/ and run this again, or run:\n"
            f'  "{sys.executable}" -m pip install --user -r "{req_file}"'
        )
    import keyring
    import requests

ELENTRA_HOST = "ubc.elentra.cloud"
BASE = f"https://{ELENTRA_HOST}"
PING_URL = f"{BASE}/dashboard"
# Loaded in a background browser tab to re-login. Must stay on BASE — the
# extension refuses any other origin.
LOGIN_URL = os.environ.get("ELENTRA_LOGIN_URL", f"{BASE}/")

DEFAULT_INTERVAL = 900  # keepalive ping cadence once the session is alive
MIN_INTERVAL = 60  # refuse anything faster; avoids hammering Elentra
JITTER = 180  # max jitter (s), clamped to a quarter of the interval
TIMEOUT = 25
MAX_HOPS = 5  # same-host redirects followed per ping

# While logged out, re-check only when the cookies change, on wake/resync, or
# at this slow cadence (Elentra may keep the same cookie across a re-login).
WAITING_RECHECK = 300
ACQUIRE_WAIT = 10  # loop cadence while logged out (pings are gated separately)
WAIT_SLICE = 30  # sleep in slices this long so a suspend/resume is noticed
RESUME_GAP = 60  # wall clock jumping this far past a slice = we were asleep

# Background re-login: attempts per logged-out spell and the delay before each.
REFRESH_BACKOFF = (0, 120, 300)
# How long one attempt may take (extension pickup on its 1-min push + SSO).
REFRESH_WINDOW = 180

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

KEYRING_SERVICE = "elentra-keepalive"
KEYRING_USER_KEY = "__username__"  # entry holding which CWL username is saved


def _state_dir() -> Path:
    # Keep this out of the project directory — it may live in a synced folder
    # (OneDrive, Dropbox, ...) and holds live session cookie values.
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "elentra-keepalive"


STATE_DIR = _state_dir()
DEFAULT_COOKIE_PATH = STATE_DIR / "cookies.json"
DEFAULT_LOG_DIR = STATE_DIR / "logs"
# Present = the saved CWL login was rejected; autofill stays off until
# --set-login, so a stale password can't lock the account with retries.
LOGIN_BLOCK_PATH = STATE_DIR / "login-rejected"

# Local listener the extension pushes to. Bound to loopback only.
# Keep these two in sync with brave-extension/background.js.
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8765
PUSH_TOKEN = "elentra-keepalive-local"

# The cookie whose presence means "there is an Elentra app session to keep warm".
SESSION_COOKIE = "entrada-me"

log = logging.getLogger("keepalive")
_stop = False


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
class _ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[2;37m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        color = self.COLORS.get(record.levelno)
        return f"{color}{text}{self.RESET}" if color else text


def _enable_windows_ansi() -> bool:
    try:
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = ctypes.windll.kernel32.GetStdHandle(-12)  # STD_ERROR_HANDLE
        mode = ctypes.c_uint32()
        if not ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except Exception:  # noqa: BLE001 — color is a nicety
        return False


class _ConsoleHandler(logging.StreamHandler):
    """In a real terminal, keeps one status line pinned below the log."""

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
            self.stream.write("\033[2J\033[3J\033[H")
            self.flush()
            self._status = ""
        finally:
            self.release()


_console: _ConsoleHandler | None = None


def _setup_logging(log_dir: Path | None, debug: bool) -> Path | None:
    """Console at INFO (DEBUG with --debug); a daily-rotated file always at DEBUG.
    Nothing logged may contain cookie values, passwords or URL query strings."""
    global _console
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    logging.getLogger("urllib3").setLevel(logging.WARNING)  # logs full URLs

    ansi = sys.stderr.isatty() and (os.name != "nt" or _enable_windows_ansi())
    _console = _ConsoleHandler(live=ansi)
    _console.setLevel(logging.DEBUG if debug else logging.INFO)
    if ansi and os.environ.get("NO_COLOR") is None:
        _console.setFormatter(_ColorFormatter("%(asctime)s  %(message)s", "%H:%M:%S"))
    else:
        _console.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S"))
    root.addHandler(_console)

    if log_dir is None:
        return None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "keepalive.log"
        fh = logging.handlers.TimedRotatingFileHandler(path, when="midnight", backupCount=14, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(threadName)-15s %(message)s"))
        root.addHandler(fh)
        return path
    except OSError as exc:
        log.warning("Can't write logs to %s (%s) — console only.", log_dir, exc)
        return None


def _status(text: str) -> None:
    """Routine, repetitive state: a pinned line in a terminal, a log line elsewhere."""
    if _console and _console.live:
        _console.set_status(text)
    elif text:
        log.info(text)


def _clear_screen() -> None:
    if _console and _console.live:
        _console.clear_screen()


def safe_url(url: str) -> str:
    """host/path only — query strings can carry SAML requests and tokens."""
    p = urlsplit(url)
    return f"{p.hostname or '?'}{p.path or '/'}"


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #
def next_delay(interval: int) -> int:
    jitter = min(JITTER, interval // 4)
    return max(30, interval + random.randint(-jitter, jitter))


def _prevent_sleep() -> None:
    """Ask the OS not to idle-sleep while this process runs (closing the lid or
    choosing Sleep still sleeps — the wake handling covers that)."""
    if os.name == "nt":
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        log.info("Keeping this PC awake while running — close me when you're done.")
    elif sys.platform == "darwin":
        # caffeinate -w exits by itself when this process does.
        try:
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.info("Keeping this Mac awake while running — close me when you're done.")
        except OSError:
            log.debug("caffeinate not available; the Mac may idle-sleep.")


_sigint_count = 0


def _handle_signal(signum, _frame):
    global _stop, _sigint_count
    _sigint_count += 1
    if _sigint_count >= 2:
        log.warning("Second signal — forcing immediate exit.")
        os._exit(1)
    _stop = True
    _store.wake()
    log.info("Stopping... (press Ctrl+C again to force quit)")


# --------------------------------------------------------------------------- #
# Saved CWL login (OS keychain)
# --------------------------------------------------------------------------- #
def saved_login() -> tuple[str, str] | None:
    try:
        user = keyring.get_password(KEYRING_SERVICE, KEYRING_USER_KEY)
        pw = keyring.get_password(KEYRING_SERVICE, user) if user else None
    except Exception as exc:  # noqa: BLE001 — no usable keychain backend
        log.debug("Keychain unavailable: %s", type(exc).__name__)
        return None
    return (user, pw) if user and pw else None


def login_blocked() -> bool:
    return LOGIN_BLOCK_PATH.exists()


def set_login() -> int:
    print("Saves your CWL login in the OS keychain (Windows Credential Manager / macOS Keychain).")
    print("It's only used to fill the CWL login page in a background tab the extension opens.\n")
    user = input("CWL username: ").strip()
    if not user:
        print("No username given — nothing saved.")
        return 1
    pw = getpass.getpass("CWL password (not shown): ")
    if not pw:
        print("No password given — nothing saved.")
        return 1
    forget_login(quiet=True)
    keyring.set_password(KEYRING_SERVICE, user, pw)
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER_KEY, user)
    LOGIN_BLOCK_PATH.unlink(missing_ok=True)
    print(f"Saved login for {user}. Remove it any time with --forget-login.")
    return 0


def forget_login(quiet: bool = False) -> int:
    try:
        user = keyring.get_password(KEYRING_SERVICE, KEYRING_USER_KEY)
        if user:
            try:
                keyring.delete_password(KEYRING_SERVICE, user)
            except keyring.errors.PasswordDeleteError:
                pass
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER_KEY)
    except keyring.errors.PasswordDeleteError:
        pass
    LOGIN_BLOCK_PATH.unlink(missing_ok=True)
    if not quiet:
        print("Saved CWL login removed." if user else "No saved CWL login.")
    return 0


# --------------------------------------------------------------------------- #
# Shared store: the listener thread writes here; the main loop reads.
# --------------------------------------------------------------------------- #
class CookieStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._cookies: list[dict] | None = None
        self._action: dict | None = None  # one-shot instruction for the extension
        self._force = False  # re-check now, even if cookies look unchanged
        self._login_busy = False  # user has a CWL login page open
        self._refresh_result: str | None = None
        self._creds_armed_until = 0.0  # monotonic; one credential hand-out allowed
        self.state = "waiting"  # reported to the extension for its toolbar badge

    # -- cookies --
    def set(self, cookies: list[dict], login_busy: bool, force: bool) -> None:
        with self._lock:
            changed = cookies_from_objects(cookies) != cookies_from_objects(self._cookies or [])
            self._cookies = cookies
            self._login_busy = login_busy
            if force:
                self._force = True
        # The extension re-pushes every minute even when nothing changed; only an
        # actual change (or an explicit wake/resync) cuts the current wait short.
        if changed or force:
            self._event.set()

    def get(self) -> list[dict] | None:
        with self._lock:
            return None if self._cookies is None else list(self._cookies)

    def login_busy(self) -> bool:
        with self._lock:
            return self._login_busy

    def take_force(self) -> bool:
        with self._lock:
            force, self._force = self._force, False
            return force

    # -- re-login --
    def request_refresh(self, url: str) -> None:
        with self._lock:
            self._action = {"type": "refresh", "url": url}
            self._refresh_result = None

    def take_action(self) -> dict | None:
        with self._lock:
            action, self._action = self._action, None
            if action and action["type"] == "refresh":
                self._creds_armed_until = time.monotonic() + REFRESH_WINDOW
            return action

    def take_credentials(self) -> tuple[str, str] | None:
        """One hand-out per delivered refresh, within its window."""
        with self._lock:
            armed = time.monotonic() < self._creds_armed_until
            self._creds_armed_until = 0.0
        if not armed or login_blocked():
            return None
        return saved_login()

    def refresh_finished(self, result: str) -> None:
        with self._lock:
            self._refresh_result = result
            self._creds_armed_until = 0.0
            self._force = True
        self._event.set()

    def take_refresh_result(self) -> str | None:
        with self._lock:
            result, self._refresh_result = self._refresh_result, None
            return result

    # -- waiting --
    def wake(self) -> None:
        self._event.set()

    def wait(self, timeout: float) -> str:
        """Sleep up to `timeout`s. Returns "event" (woken early), "resume" (the
        machine slept through it — wall clock jumped), or "timeout"."""
        end = time.time() + timeout
        while True:
            left = end - time.time()
            if left <= 0:
                return "timeout"
            chunk = min(WAIT_SLICE, left)
            t0 = time.time()
            woke = self._event.wait(chunk)
            self._event.clear()
            if woke:
                return "event"
            if time.time() - t0 > chunk + RESUME_GAP:
                return "resume"


_store = CookieStore()


def is_elentra(c: dict) -> bool:
    d = (c.get("domain") or "").lstrip(".").lower()
    return d == ELENTRA_HOST or d.endswith("." + ELENTRA_HOST)


def cookies_from_objects(objs: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for c in objs:
        name, value = c.get("name"), c.get("value")
        if name is not None and value is not None:
            out[name] = value
    return out


def build_jar(objs: list[dict]) -> requests.cookies.RequestsCookieJar:
    jar = requests.cookies.RequestsCookieJar()
    for c in objs:
        name, value = c.get("name"), c.get("value")
        if name is None or value is None:
            continue
        jar.set(name, value, domain=(c.get("domain") or ELENTRA_HOST).lstrip("."), path=c.get("path") or "/")
    return jar


# --------------------------------------------------------------------------- #
# Local listener
# --------------------------------------------------------------------------- #
MAX_PUSH_BYTES = 1 << 20
EVENT_NAME = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
REFRESH_RESULTS = {"refresh-returned", "refresh-timeout", "refresh-login-rejected", "refresh-skipped", "refresh-closed", "refresh-no-login"}


class _PushHandler(BaseHTTPRequestHandler):
    def _reject_browser_origin(self) -> bool:
        """The extension's service worker sends a chrome-extension:// origin (or
        none). A web page always sends its own — refuse those."""
        origin = self.headers.get("Origin")
        if origin and not origin.startswith("chrome-extension://"):
            self._send(403)
            return True
        return False

    def _send(self, code: int, body: dict | None = None) -> None:
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        if body is not None:
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _payload(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= MAX_PUSH_BYTES:
                raise ValueError(f"bad Content-Length: {length}")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("not an object")
        except ValueError:
            self._send(400)
            return None
        if payload.get("token") != PUSH_TOKEN:
            self._send(403)
            return None
        return payload

    def do_GET(self):  # noqa: N802
        if self._reject_browser_origin():
            return
        if self.path.rstrip("/") == "/health":
            self._send(200, {"ok": True})
        else:
            self._send(404)

    def do_POST(self):  # noqa: N802
        if self._reject_browser_origin():
            return
        route = self.path.rstrip("/")
        if route not in ("/cookies", "/event", "/credentials"):
            self._send(404)
            return
        payload = self._payload()
        if payload is None:
            return
        if route == "/cookies":
            self._cookies(payload)
        elif route == "/event":
            self._event(payload)
        else:
            self._credentials()

    def _cookies(self, payload: dict) -> None:
        cookies = payload.get("cookies")
        if not isinstance(cookies, list) or not all(isinstance(c, dict) for c in cookies):
            self._send(400)
            return
        # Older extension versions also sent CWL login-server cookies; never keep those.
        cookies = [c for c in cookies if is_elentra(c)]
        reason = str(payload.get("reason", "?"))[:20]
        login_busy = bool(payload.get("loginInProgress"))
        _store.set(cookies, login_busy, force=reason in ("resume", "resync", "refresh-landed"))
        names = ", ".join(sorted({str(c.get("name", "?")) for c in cookies})) or "(none)"
        log.debug("Push (%s): %d cookie(s) [%s]%s", reason, len(cookies), names, " · CWL login page open" if login_busy else "")
        if reason == "resume":
            log.info("Browser reports the computer woke up — checking the session.")
        elif reason == "resync":
            log.info("Resync requested from the extension.")

        reply = {"state": _store.state}
        action = _store.take_action()
        if action:
            reply["action"] = action
            log.debug("Handing the extension: %s", action["type"])
        self._send(200, reply)

    def _event(self, payload: dict) -> None:
        name = str(payload.get("event", ""))
        detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
        if not EVENT_NAME.match(name):
            self._send(400)
            return
        text = json.dumps(detail, separators=(",", ":"))[:600]
        log.debug("Extension: %s %s", name, text)
        if name == "refresh-login-rejected":
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            LOGIN_BLOCK_PATH.touch()
            log.error("CWL rejected the saved login. Autofill is off until you run --set-login again.")
        if name in REFRESH_RESULTS:
            _store.refresh_finished(name)
        self._send(200, {"ok": True})

    def _credentials(self) -> None:
        creds = _store.take_credentials()
        if creds is None:
            log.debug("Extension asked for the saved login: none available (not saved, blocked, or not armed).")
            self._send(404, {})
            return
        log.info("Filling the saved CWL login in the background re-login tab.")
        self._send(200, {"username": creds[0], "password": creds[1]})

    def log_message(self, fmt, *args):
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
    threading.Thread(target=server.serve_forever, name="listener", daemon=True).start()
    log.info("Listening for the browser extension on %s:%d.", host, port)
    return server


# --------------------------------------------------------------------------- #
# Session checks / persistence
# --------------------------------------------------------------------------- #
def lock_down(path: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.warning("Couldn't restrict permissions on %s: %s", path, result.stdout.strip() or result.stderr.strip())
    else:
        path.chmod(0o600)


def load_cookie_objects(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        return []
    return [c for c in data if isinstance(c, dict) and "name" in c and "value" in c and is_elentra(c)]


def persist(objs: list[dict], path: Path | None) -> None:
    if path is None:
        return
    slim = [{k: c[k] for k in ("name", "value", "domain", "path") if k in c} for c in objs if is_elentra(c)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(slim, indent=2))
    lock_down(path)


def check_session(session: requests.Session, url: str) -> tuple[str, list[str]]:
    """Ping Elentra, following redirects only while they stay on Elentra.

    Returns ("alive" | "dead" | "error", trail). A redirect anywhere else (the
    CWL login server) means logged out — and we stop there, never touching the
    login server: requests sent there with the browser's cookies would start a
    competing login and leave the browser's login form "Stale Request".
    """
    trail: list[str] = []
    for _ in range(MAX_HOPS):
        resp = session.get(url, timeout=TIMEOUT, allow_redirects=False)
        trail.append(f"{resp.status_code} {safe_url(url)}")
        if resp.is_redirect:
            nxt = urljoin(url, resp.headers.get("Location", ""))
            p = urlsplit(nxt)
            if p.scheme != "https" or (p.hostname or "").lower() != ELENTRA_HOST:
                trail.append(f"-> {p.hostname} (not followed)")
                return "dead", trail
            if "/login" in p.path.lower():
                trail.append(f"-> {p.path} (login page)")
                return "dead", trail
            url = nxt
            continue
        break
    else:
        trail.append("too many redirects")
        return "dead", trail

    if resp.status_code >= 500 or resp.status_code == 429:
        return "error", trail
    if resp.status_code in (401, 403):
        return "dead", trail
    body = resp.text[:20000].lower()
    title = re.search(r"<title[^>]*>(.*?)</title>", body, re.S)
    if title:
        trail.append(f"title={title.group(1).strip()[:60]!r}")
    if "samlrequest" in body or 'name="j_username"' in body:
        trail.append("login form in page")
        return "dead", trail
    return "alive", trail


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": os.environ.get(
                "KEEPALIVE_UA",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    return session


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def run_auto_loop(args) -> int:
    server = start_listener(LISTEN_HOST, args.listen_port)

    # Warm start from the last known-good cookies; they're re-validated first and
    # the file is deleted if they turn out dead.
    if args.cookie_file.exists():
        try:
            seed = load_cookie_objects(args.cookie_file)
            _store.set(seed, login_busy=False, force=True)
            log.debug("Warm start from %s (%d cookie(s)).", args.cookie_file, len(seed))
        except Exception as exc:  # noqa: BLE001
            log.debug("Warm start skipped: %s", exc)

    creds = saved_login()
    if creds and not login_blocked():
        log.info("Saved CWL login found (%s) — background re-login can fill it in.", creds[0])
    elif creds:
        log.warning("Saved CWL login was rejected last time — run --set-login to update it.")
    else:
        log.debug("No saved CWL login; background re-login relies on CWL's own session.")

    session = make_session()
    _prevent_sleep()

    state = ""  # "", "alive" or "waiting"
    errors = 0
    dead_sig: frozenset | None = None  # cookies last found dead
    last_check = 0.0  # monotonic, last ping while waiting
    attempts: list[float] = []  # monotonic start of each re-login attempt this spell
    attempt_open = False
    last_note = ""
    force = True

    def note(level: int, msg: str) -> None:
        nonlocal last_note
        if msg != last_note:
            log.log(level, msg)
            last_note = msg

    def new_spell(why: str) -> None:
        nonlocal attempts, attempt_open, dead_sig, force
        log.debug("Re-check: %s", why)
        attempts, attempt_open, dead_sig, force = [], False, None, True

    while not _stop:
        if _store.take_force():
            force = True
        result = _store.take_refresh_result()
        if result:
            attempt_open = False
            log.debug("Re-login attempt %d ended: %s", len(attempts), result)
            if result == "refresh-skipped" and attempts:
                attempts.pop()  # user was logging in themselves; doesn't count

        objs = _store.get()
        flat = cookies_from_objects(objs) if objs else {}
        sig = frozenset(flat.items())
        verdict = None

        due = force or state != "waiting" or sig != dead_sig or time.monotonic() - last_check >= WAITING_RECHECK
        if SESSION_COOKIE in flat and due:
            if _store.login_busy() and state == "waiting" and not force:
                note(logging.INFO, "CWL login page open in the browser — waiting for you to finish.")
                _store.wait(ACQUIRE_WAIT)
                continue
            force = False
            session.cookies = build_jar(objs)
            try:
                verdict, trail = check_session(session, args.url)
            except requests.RequestException as exc:
                verdict, trail = "error", [type(exc).__name__]
            log.debug("Ping %s: %s", verdict, " | ".join(trail))
            last_check = time.monotonic()

            if verdict == "error":
                errors += 1
                delay = min(30 * errors, 300)
                if errors == 1:
                    log.warning("Can't reach Elentra (%s) — retrying.", trail[-1])
                else:
                    _status(f"Can't reach Elentra — {errors} failed attempts, retrying in {delay}s")
                if _store.wait(delay) == "resume":
                    new_spell("woke from sleep")
                continue
            if errors:
                log.info("Connection to Elentra restored.")
                errors = 0
        elif SESSION_COOKIE not in flat:
            force = False

        if verdict == "alive":
            if state != "alive":
                _clear_screen()
                log.info("Logged in to Elentra — keepalive active, pinging every ~%d min.", round(args.interval / 60))
                state = _store.state = "alive"
                last_note = ""
            attempts, attempt_open, dead_sig = [], False, None
            persist(objs, args.cookie_file)
            delay = next_delay(args.interval)
            _status(f"Session alive · last ping {time.strftime('%H:%M')} · next ~{time.strftime('%H:%M', time.localtime(time.time() + delay))}")
            if _store.wait(delay) == "resume":
                log.info("Computer woke up — checking the session now.")
                force = True
            continue

        if verdict == "dead":
            dead_sig = sig
            args.cookie_file.unlink(missing_ok=True)

        if state == "alive":
            _status("")
            note(logging.WARNING, "Elentra session expired." if SESSION_COOKIE in flat else "Logged out of Elentra in the browser.")
        if state != "waiting":
            attempts, attempt_open = [], False
        state = _store.state = "waiting"

        if objs is None:
            note(logging.WARNING, "Waiting for the browser extension — is it loaded and enabled?")
        else:
            maybe_refresh = objs or saved_login()
            if attempt_open and time.monotonic() - attempts[-1] > REFRESH_WINDOW:
                attempt_open = False
                log.debug("Re-login attempt %d: no result from the extension in time.", len(attempts))
            if not maybe_refresh:
                note(logging.WARNING, "Waiting for you to log in to Elentra.")
            elif _store.login_busy():
                note(logging.INFO, "CWL login page open in the browser — waiting for you to finish.")
            elif not attempt_open and len(attempts) < len(REFRESH_BACKOFF):
                since = time.monotonic() - attempts[-1] if attempts else float("inf")
                if since >= REFRESH_BACKOFF[len(attempts)]:
                    attempts.append(time.monotonic())
                    attempt_open = True
                    _store.request_refresh(args.login_url)
                    note(logging.WARNING, f"Not logged in — re-login attempt {len(attempts)}/{len(REFRESH_BACKOFF)} in a background tab...")
            elif not attempt_open:
                hint = "" if saved_login() else " (Tip: save your CWL login with --set-login.)"
                note(logging.WARNING, f"Background re-login didn't work — please log in to Elentra in the browser.{hint}")

        if _store.wait(ACQUIRE_WAIT) == "resume":
            new_spell("woke from sleep")

    server.shutdown()
    _status("")
    log.info("Stopped.")
    return 0



def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set-login", action="store_true", help="Save your CWL login in the OS keychain and exit")
    ap.add_argument("--forget-login", action="store_true", help="Remove the saved CWL login and exit")
    ap.add_argument("--cookie-file", type=Path, default=DEFAULT_COOKIE_PATH, help="Warm-start/persist path for the cookie JSON")
    ap.add_argument("--listen-port", type=int, default=LISTEN_PORT, help="Local port the extension pushes to")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="Keepalive ping cadence in seconds")
    ap.add_argument("--url", default=PING_URL, help="Authenticated Elentra URL to ping")
    ap.add_argument("--login-url", default=LOGIN_URL, help="Elentra URL the extension loads to re-login (env: ELENTRA_LOGIN_URL)")
    ap.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help="Where to keep rotated debug logs")
    ap.add_argument("--no-log-files", action="store_true", help="Don't write log files")
    ap.add_argument("--debug", action="store_true", help="Show debug detail in the console too")
    args = ap.parse_args()

    if args.set_login:
        return set_login()
    if args.forget_login:
        return forget_login()
    if args.interval < MIN_INTERVAL:
        ap.error(f"--interval must be at least {MIN_INTERVAL} seconds")
    for name in ("url", "login_url"):
        if not getattr(args, name).startswith(f"{BASE}/"):
            ap.error(f"--{name.replace('_', '-')} must start with {BASE}/")

    log_path = _setup_logging(None if args.no_log_files else args.log_dir, args.debug)
    if log_path:
        log.info("Detailed log: %s", log_path)
    log.debug("Started: python %s, interval %ds, ping %s", sys.version.split()[0], args.interval, safe_url(args.url))

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    return run_auto_loop(args)


if __name__ == "__main__":
    sys.exit(main())
