# 🩺 Let's Keep Elentra Alive

Are you **frustrated** being logged out of Elentra over and over again in the same lecture? Are you tired of redundantly re-authenticating yourself? Do you love Elentra but feel like Elentra doesn't love you back? Well, you've come to the right place.

## ✨ What does this program do?

As long as you're connected to the internet and your laptop isn't asleep, it prevents your logged-in Elentra session from idling out — so you can keep working without interruption.

## 🚀 How do I use this?

0. **Install [Python](https://www.python.org/downloads/latest/python3.13/)**

1. **Load the extension once**: visit `brave://extensions` (or `chrome://extensions` — see [Browser support](#-browser-support)) → enable Developer mode → click **Load unpacked** → select the `brave-extension` folder. That's it.

2. **Start the script** any time (logged in or out) by double-clicking `run-keepalive.bat`.

3. The running script will be visible in a terminal where it will show the status of the program. Use Ctrl+C x2 to stop it or simply close to shut it down.

## ⚙️ How does this program work?

Two moving parts:

1. **Browser extension** — `brave-extension/`
   An MV3 extension that waits for and reads your Elentra + SSO cookies (including httpOnly ones). Once available, it pushes them to your local listener at `127.0.0.1:8765`.
2. **Python script** — `elentra_keepalive.py`
   Listens on `127.0.0.1:8765` until a valid cookie is received. Once validated, it pings the Elentra dashboard every ~10 minutes (± a few minutes of jitter) to keep the session warm. Without a live cookie, it just keeps listening for one.

## 🌐 Browser support

Built and tested in **Brave**, but it's a plain MV3 extension using only standard `chrome.*` APIs — no Brave-specific behavior. It loads and runs the same way in **Chrome** (and any other Chromium-based browser) via `chrome://extensions` → Developer mode → Load unpacked. Firefox is not currently supported by the extension (its cookie APIs differ), though the script's `--browser firefox` flag can read Firefox's cookie store directly since Firefox doesn't use App-Bound Encryption.

## 📋 Requirements

- Python 3.9+
- [`requests`](https://pypi.org/project/requests/) — install with `pip install requests`
- A Chromium-based browser (Brave, Chrome, Edge, etc.) to run the companion extension

---

## 🤔 Considerations

#### Why do I need an extension? Why not just read Brave's/Chrome's cookie database directly?

> On current Chromium-based browsers, the `Cookies` SQLite file is held under an exclusive OS lock while the browser runs (external reads are refused). On top of that, App-Bound Encryption re-encrypts cookie values in a way that can't be decrypted outside the browser process. The companion extension sidesteps both by handing over the already-decrypted cookie itself and catching new logins the moment they happen.

#### 🔒 Does this program store my credentials?

> **No.** It uses what's called a browser cookie. Elentra sends you a cookie after you "shake its hand" logging in. Cookies are randomly generated tokens websites use to remember you, and they eventually expire, forcing you to log in again or refresh the session. This program stores only the current session cookies in use — outside the project folder, in `%LOCALAPPDATA%\elentra-keepalive\cookies.json` on Windows (or `~/.local/state/elentra-keepalive/cookies.json` on Linux/Mac) — and they never leave your machine except to Elentra itself.

#### 🛡️ Security notes

> The local python listener binds to `127.0.0.1` only — nothing off your machine can reach it. Every cookie the extension pushes is **validated against Elentra before being adopted**, so a stray or forged request can't do anything to a working session.
>
> The extension similarly only ever talks to `127.0.0.1`. It sends your cookie nowhere else.

#### 🔋Battery Usage

> **Little to none**. This is a lightweight python script that pings every 10 or so minutes, running directly out of terminal.

## ⚠️ Disclaimer

This is an independent, unofficial hobby project — it is **not affiliated with, endorsed by, or supported by UBC or Elentra**. It automates nothing beyond what you could already do by keeping a browser tab open and periodically refreshing it, and it never touches, stores, or transmits your CWL credentials. Use at your own discretion and in line with your institution's acceptable-use policies.

## 📑 A note on AI

Portions of this project (code, debugging, and documentation) were developed with the assistance of AI tools. Everything has been reviewed and tested by a human before being published.

## 📄 License

[MIT](LICENSE) — do whatever you like with this, no warranty provided.
