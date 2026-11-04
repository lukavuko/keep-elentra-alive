---
updated: 2026-11-03 22:07:17
---

# 🩺 Let's Keep Elentra Alive

Tired of being logged out of Elentra mid-lecture? Keep reading.

## ⚙️ What it do?

**While online, this program prevents your Elentra session from timing out. It just keeps you logged in as long as it's running!**

In addition, I've added a few recent improvements that allows the software to auto-login for you in the background. This is an optional feature as it requires you to store your CWL credentials locally, however this is done in an encrypted keyring which adds security   

> [!TIP]
> **If you're already set up:**
> 1. Double-click `run-keepalive-for-Windows.bat` (or `run-keepalive-for-macOS.command`)
> 2. Log in to Elentra in Brave/Chrome. The extension badge will turn 😎 when you're good.
> 3. Logged out after sleep? With a saved login it can sign you back in by itself. Without one, just log in again.
>

## 🚀 How to intall it



> 
>
> If you only need to update it, run `git pull` in your downloaded directory and re-load the web extension.

#### If you've never heard of Git, you may start here

1. Identify your 💻**Terminal** for executing commands
    - Windows: <u>PowerShell</u> which you can use with [Windows Terminal](https://apps.microsoft.com/detail/9n0dx20hk701?hl=en-US&gl=US) for a nicer experience
    - MacOS: <u>ZSH</u> which you can use with Mac's build in [terminal](https://support.apple.com/en-ca/guide/terminal/trmld4c92d55/mac)
2. Download and install **Git**
    - Link to the [latest installer](https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.4/Git-2.56.0.4-64-bit.exe) for [Git for Windows](https://git-scm.com/install/windows)
    - On MacOS you can install it via commands in terminal ([see this page](https://git-scm.com/install/mac))

#### If you have Git, start here

1. **Install** **Python**.
    - The [link for Windows installer](https://www.python.org/ftp/python/pymanager/python-manager-26.4.msix) (see all downloads [here](https://www.python.org/downloads/))
    - The [link for Mac installer](https://www.python.org/ftp/python/3.13.17/python-3.13.17-macos11.pkg) (see all downloads [here](https://www.python.org/downloads/macos/))
2. **Download this repository** to a location you prefer.
    - Open terminal and paste+enter `git clone https://github.com/lukavuko/keep-elentra-alive.git`
    - Or manually [download the zip](https://github.com/lukavuko/keep-elentra-alive/archive/refs/heads/master.zip) then unzip it.
3. Next, **add the extension** to your browser (Chrome or Brave are tested).
    - Go to your manage extensions screen. Get here by clicking or pasting these links in your browser:
    - [`chrome://extensions`](chrome://extensions) or [`brave://extensions`](brave://extensions) *(Brave is superior)*
    - Switch on **Developer mode** in the top right of your screen.
    - Click **Load unpacked** and select the `brave-extension` folder you see inside of your download.
    - You should now see this as one of your new extensions!
4. **Start the script.** 
    Double-click `run-keepalive-for-Windows.bat` (Windows) or `run-keepalive-for-macOS.command` (macOS).
5. **Login** to Elentra and see the magic!



> **On a Mac:** the first time you open a `.command` file, macOS may say it can't be opened. 
> Right-click it, choose **Open**, then **Open** again. 
>
> If macOS asks whether Python may use your keychain, choose **Always Allow** so that
> re-login works while you're away.



#### For automatically logging you in (optional but super useful)

1. Save your CWL login by double-clicking `save-login-for-Windows.bat` or`save-login-for-macOS.command`
2. It will interactively prompt you to save these to a locally encrypted keyring (text will remain invisible when you type)
3. Restart the `run-keepalive-for-yourOS.bat/command` script and voila!

For more information, see the [automatic login section](#-auto-login-feature).


## 📋 Requirements

- A Chromium-based browser (Brave, Chrome, Edge)
- Python 3.9+
- [`requests`](https://pypi.org/project/requests/) and [`keyring`](https://pypi.org/project/keyring/): `pip install -r requirements.txt`
  (the script installs them itself if missing)

## ⚙️ How it works for the curious minded 

1. **Extension** (`brave-extension/`): an MV3 extension that reads your Elentra
   session cookies, including httpOnly ones, and sends them to
   `127.0.0.1:8765`. CWL login cookies never leave the browser.
2. **Script** (`elentra_keepalive.py`): listens on `127.0.0.1:8765`. Once it has
   a valid cookie, it pings the Elentra dashboard every ~15 minutes (± 3).

![How Elentra Keepalive keeps you logged in](mermaid-graphical-view.png)


#### 🔁 Auto-login feature

When you shutdown/sleep/disconnect from the internet, the software won't be 
able to keep you logged in. As such, you have the option to store credentials securely 
on your local machine in an OS keyring (*a system-level encrypted database used*
*by the operating system to securely store and manage sensitive credentials*). This 
can be done with the `save-login` launcher file. These credentials can then be used 
to log you back in automatically when you're back online. Easy!

###### How is this accomplished?

By the browser extension opening Elentra in a background tab. 

If your session is still valid, the tab closes and the routine resumes. 
Otherwise, the extension fills in the login page and tries to log you in up 
up to 3 times over ~5 minutes if unsuccessful on the first attempt.

Keynotes
- Your login is stored in an OS keychain (Windows Credential Manager / macOS
  Keychain), never in a file.
- If CWL rejects it, the autofill will switch off until you save it again so a
  changed password can't ever lock your account with multiple retries.
- If CWL asks for MFA (e.g. on a VPN), the re-login feature will wait for it, then give up.
- You can remove any stored credentials by running `python elentra_keepalive.py --forget-login` 
    in your terminal from inside the directory of the software.


## 🤔 Considerations

#### Why an extension?

> While a Chromium browser runs, its cookie file is locked, and the values are
> encrypted so only the browser can read them. The extension hands over the
> cookie directly and catches new logins as they happen.

#### 🌐 Browser support

> Built in **Brave**. Works in **Chrome** and other Chromium browsers the same way. Firefox is not supported.

#### 🔒 Does it store my credentials?

> **Only if you ask it to**, and only in your OS keychain (see
> [Re-login after sleep](#-re-login-after-sleep)). Otherwise it stores only the
> Elentra session cookies, in
> `%LOCALAPPDATA%\elentra-keepalive\cookies.json` (Windows) or
> `~/.local/state/elentra-keepalive/cookies.json` (macOS/Linux). They are sent
> nowhere but Elentra.

#### 🛡️ Security

> The script listens on `127.0.0.1` only. Every cookie is checked against
> Elentra before use, so a forged request cannot break a working session.
>
> The extension sends only Elentra cookies, and only to `127.0.0.1`. Its
> re-login tab can only open `https://ubc.elentra.cloud`, and it fills a saved
> login only into that tab. Its `webRequest` permission only observes page
> loads in that tab (address, status code) for the log.
>
> The saved login travels from the script to the extension over `127.0.0.1`,
> guarded by a fixed token.

#### 🔋 Battery

> **Little to none.** One ping every ~15 minutes.


## ⚠️ Disclaimer

This is an unofficial hobby project. It's not affiliated with, endorsed by, or supported by
any party. It does nothing more than regularly ping Elentra to keep an authentication cookie 
alive (pretending you're still there being all studious). It touches your CWL login only if you 
choose to save one, and then only to fills the CWL login form for you. Use it within your institution's
acceptable-use policies.

In addition, parts (the majority) of this project was built with AI tooling though a human reviewed and tested
everything before publishing anything.

## 📄 License

[MIT](LICENSE). No warranty.
