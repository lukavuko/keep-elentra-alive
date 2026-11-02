# 🩺 Let's Keep Elentra Alive

Tired of being logged out of Elentra mid-lecture? This keeps you logged in.

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
   running but not logged in, **OFF** = script not running.

> Updating? Click reload ↻ on the extension's card so the browser loads the new
> code.

## ⚙️ How it works

1. **Extension** (`brave-extension/`): an MV3 extension that reads your Elentra
   and SSO cookies, including httpOnly ones, and sends them to
   `127.0.0.1:8765`.
2. **Script** (`elentra_keepalive.py`): listens on `127.0.0.1:8765`. Once it has
   a valid cookie, it pings the Elentra dashboard every ~15 minutes (± 3).

The terminal prints a line only when something changes. Routine pings update one
status line.

#### 🔁 Silent re-login

If Elentra logs you out but your CWL session is still valid, the extension opens
Elentra in a background tab for a few seconds. SSO completes on its own, with no
password or MFA, and the tab closes. If that fails, you'll see _"Silent re-login
didn't work — please log in to Elentra in Brave."_ It tries once per logout.

The page defaults to `https://ubc.elentra.cloud/`. Change it with the
`ELENTRA_LOGIN_URL` environment variable or `--login-url`. It must start with
`https://ubc.elentra.cloud/`.

## 🌐 Browser support

Built in **Brave**. Works in **Chrome** and other Chromium browsers the same
way. Firefox is not supported.

## 📋 Requirements

- Python 3.9+
- [`requests`](https://pypi.org/project/requests/): `pip install requests`
- A Chromium-based browser (Brave, Chrome, Edge)

## 🤔 Considerations

#### Why an extension?

> While a Chromium browser runs, its cookie file is locked, and the values are
> encrypted so only the browser can read them. The extension hands over the
> cookie directly and catches new logins as they happen.

#### 🔒 Does it store my credentials?

> **No.** It stores only the session cookies, in
> `%LOCALAPPDATA%\elentra-keepalive\cookies.json` (Windows) or
> `~/.local/state/elentra-keepalive/cookies.json` (Linux/Mac). They are sent
> nowhere but Elentra.

#### 🛡️ Security

> The script listens on `127.0.0.1` only. Every cookie is checked against
> Elentra before use, so a forged request can't break a working session.
>
> The extension sends cookies only to `127.0.0.1`. Its re-login tab can only
> open `https://ubc.elentra.cloud`.

#### 🔋 Battery

> **Little to none.** One ping every ~15 minutes.

## ⚠️ Disclaimer

An unofficial hobby project. **Not affiliated with, endorsed by, or supported by
UBC or Elentra.** It does nothing more than keeping a tab open and refreshing
it, and it never touches your CWL credentials. Use it within your institution's
acceptable-use policies.

## 📑 A note on AI

Parts of this project were built with AI help. A human reviewed and tested
everything before publishing.

## 📄 License

[MIT](LICENSE). No warranty.
