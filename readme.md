# 🩺 Let's Keep Elentra Alive

Tired of being logged out of Elentra mid-lecture? This keeps you logged in.

> [!TIP]
> **Already set up? To start:**
> 1. Double-click `run-keepalive-for-Windows.bat` (or `run-keepalive-for-macOS.command`) and leave the window open.
> 2. Log in to Elentra in Brave/Chrome. The extension badge turns 😎 when you're kept alive.
> 3. Logged out after sleep? With a saved login it signs you back in by itself. Without one, just log in again.
>
> Stuck? 💀 means the script isn't running, and 🫠 means it's waiting for you to log in.

## ✨ What it does

While you're online and your laptop is awake, it stops your Elentra session from
timing out.

## 🚀 How to use it

0. **Install [Python](https://www.python.org/downloads/latest/python3.13/).**
1. **`git clone`** or **download and unzip** this
   [repository](https://github.com/lukavuko/keep-elentra-alive/archive/refs/heads/master.zip).
2. **Add the extension.** Open [`brave://extensions`](brave://extensions) or
   [`chrome://extensions`](chrome://extensions), turn on Developer mode, click
   **Load unpacked**, and pick the `brave-extension` folder.
3. **Start the script.** Double-click `run-keepalive-for-Windows.bat` (Windows)
   or `run-keepalive-for-macOS.command` (macOS). Ctrl+C or close the window to
   stop it.
4. **Check the badge** (pin the extension): **ON** = kept alive, **…** =
   running but not logged in, **OFF** = script not running. Click the icon to
   make the script re-check right away.
5. **Optional: save your CWL login** so you're logged back in after the laptop
   sleeps. Double-click `save-login-for-Windows.bat` or
   `save-login-for-macOS.command`. See [Re-login after sleep](#-re-login-after-sleep).

> **On a Mac:** the first time you open a `.command` file, macOS may say it
> can't be opened. Right-click it, choose **Open**, then **Open** again. If
> macOS asks whether Python may use your keychain, choose **Always Allow** so
> re-login works while you're away. Install Python from python.org: Homebrew's
> Python refuses the automatic dependency install.

> Updating? Click reload ↻ on the extension's card so the browser loads the new
> code.

## ⚙️ How it works

1. **Extension** (`brave-extension/`): an MV3 extension that reads your Elentra
   and SSO cookies, including httpOnly ones, and sends them to
   `127.0.0.1:8765`.
2. **Script** (`elentra_keepalive.py`): listens on `127.0.0.1:8765`. Once it has
   a valid cookie, it pings the Elentra dashboard every ~15 minutes (± 3).

The script only ever talks to Elentra. If Elentra redirects a ping to the CWL
login server, that just means "logged out"; the script stops there. (Older
versions followed that redirect, which is what caused the _"Stale Request"_
page when you logged in.) While you have a CWL login page open, the script
holds off.

The terminal keeps everything you should know on screen: the startup notes
(where logs go, whether a CWL login is saved and where, that the computer is
kept awake, how to stop), wake-ups, logouts and failures. Lines that repeat
(the latest ping, each re-login attempt with every request it made and the
response) are tidied away once the session is back.

#### 📝 Logs

Detailed logs go to `%LOCALAPPDATA%\elentra-keepalive\logs` (Windows) or
`~/.local/state/elentra-keepalive/logs` (macOS/Linux): one file per day,
kept for 14 days. Change that with `--log-days 60`, or keep everything with
`--log-days 0`. They include every ping result, every push from the extension
and each step of a background re-login. They never contain cookie values,
passwords or full URLs. Run with `--debug` to see the same detail in the
terminal.

#### 🔁 Re-login after sleep

When the computer wakes (or the session dies), the extension opens Elentra in a
background tab. If your CWL session is still valid, that's enough. If CWL asks
for your username and password, the extension fills in the login you saved with
the save-login launcher, and the tab closes once you're back on Elentra. It
tries up to 3 times (immediately, then after 2 and 5 minutes), and starts over
the next time the computer wakes.

- Your login is stored in the OS keychain (Windows Credential Manager / macOS
  Keychain), never in a file. It is handed to the extension once per re-login
  attempt, only while that attempt is running.
- If CWL rejects it, autofill switches off until you save it again, so a
  changed password can't lock your account with retries.
- If CWL asks for MFA (e.g. on a VPN), the re-login waits for it, then gives up.
- Remove it with `python elentra_keepalive.py --forget-login`.

The page defaults to `https://ubc.elentra.cloud/`. Change it with the
`ELENTRA_LOGIN_URL` environment variable or `--login-url`. It must start with
`https://ubc.elentra.cloud/`.

## 🌐 Browser support

Built in **Brave**. Works in **Chrome** and other Chromium browsers the same
way. Firefox is not supported.

## 📋 Requirements

- Python 3.9+
- [`requests`](https://pypi.org/project/requests/) and
  [`keyring`](https://pypi.org/project/keyring/): `pip install -r requirements.txt`
  (the script installs them itself if missing)
- A Chromium-based browser (Brave, Chrome, Edge)

## 🤔 Considerations

#### Why an extension?

> While a Chromium browser runs, its cookie file is locked, and the values are
> encrypted so only the browser can read them. The extension hands over the
> cookie directly and catches new logins as they happen.

#### 🔒 Does it store my credentials?

> **Only if you ask it to**, and only in your OS keychain (see
> [Re-login after sleep](#-re-login-after-sleep)). Otherwise it stores only the
> Elentra session cookies, in
> `%LOCALAPPDATA%\elentra-keepalive\cookies.json` (Windows) or
> `~/.local/state/elentra-keepalive/cookies.json` (macOS/Linux). They are sent
> nowhere but Elentra.

#### 🛡️ Security

> The script listens on `127.0.0.1` only. Every cookie is checked against
> Elentra before use, so a forged request can't break a working session.
>
> The extension sends only Elentra cookies, and only to `127.0.0.1`. Its
> re-login tab can only open `https://ubc.elentra.cloud`, and it fills a saved
> login only into that tab.
>
> The saved login travels from the script to the extension over `127.0.0.1`,
> guarded by a fixed token and refused to web pages. Any program running as you
> could read it from the keychain anyway, but on a computer shared with other
> user accounts, don't save a login.

#### 🔋 Battery

> **Little to none.** One ping every ~15 minutes.

## ⚠️ Disclaimer

An unofficial hobby project. **Not affiliated with, endorsed by, or supported by
UBC or Elentra.** It does nothing more than keeping a tab open and refreshing
it. It touches your CWL login only if you save one, and then only to fill
the CWL login form for you. Use it within your institution's
acceptable-use policies.

## 📑 A note on AI

Parts of this project were built with AI help. A human reviewed and tested
everything before publishing.

## 📄 License

[MIT](LICENSE). No warranty.
