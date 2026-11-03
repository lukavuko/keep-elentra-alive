// Elentra Keepalive Cookie Bridge — MV3 service worker.
//
// Pushes the Elentra session cookies (including httpOnly ones) to the local
// keepalive script: on cookie changes, on a 1-minute alarm, on browser start,
// when the computer wakes (idle API), and when you click the toolbar icon.
//
// Only ubc.elentra.cloud cookies are sent. CWL login-server cookies never leave
// the browser — the script used to replay them, which started competing logins
// and caused "Stale Request" on the CWL login page.
//
// The script's reply drives the badge and can ask for a background re-login:
// Elentra is opened in an inactive tab; if CWL shows its login form there, the
// saved login (OS keychain, via the script) is filled in. Every step is
// reported to the script's log so failures can be diagnosed.
//
// Keep PORT and TOKEN in sync with elentra_keepalive.py (LISTEN_PORT, PUSH_TOKEN).

const PORT = 8765;
const SCRIPT = `http://127.0.0.1:${PORT}`;
const TOKEN = "elentra-keepalive-local";

const ELENTRA_HOST = "ubc.elentra.cloud";
const ELENTRA_ORIGIN = `https://${ELENTRA_HOST}`; // the only origin a refresh tab may open
const IDP_HOST = "authentication.ubc.ca";

const LOGIN_BUSY_MS = 2 * 60 * 1000; // a CWL page navigated this recently = you're logging in
const REFRESH_TIMEOUT_MIN = 1.5;

const BADGES = {
  alive: ["ON", "#2e7d32", "Elentra keepalive: session alive (click to resync)"],
  waiting: ["…", "#f9a825", "Elentra keepalive: running, waiting for a live Elentra session (click to resync)"],
  off: ["OFF", "#757575", "Elentra keepalive: script not running"],
};

function setBadge(state) {
  const [text, color, title] = BADGES[state] || BADGES.waiting;
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
  chrome.action.setTitle({ title });
}

function hostOf(url) {
  try {
    return new URL(url).hostname;
  } catch (e) {
    return "";
  }
}

// host/path only — query strings carry SAML requests.
function safe(url) {
  try {
    const u = new URL(url);
    return u.hostname + u.pathname;
  } catch (e) {
    return "?";
  }
}

const isElentra = (d) => {
  d = (d || "").replace(/^\./, "");
  return d === ELENTRA_HOST || d.endsWith("." + ELENTRA_HOST);
};

async function gather() {
  let list = [];
  try {
    list = await chrome.cookies.getAll({ domain: ELENTRA_HOST });
  } catch (e) {}
  return list
    .filter((c) => isElentra(c.domain))
    .map((c) => ({ name: c.name, value: c.value, domain: c.domain, path: c.path, secure: c.secure, httpOnly: c.httpOnly }));
}

async function post(path, body) {
  const res = await fetch(SCRIPT + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: TOKEN, ...body }),
  });
  return res.ok ? res.json().catch(() => ({})) : null;
}

function report(event, detail = {}) {
  return post("/event", { event, detail }).catch(() => {});
}

// --- You logging in yourself ---------------------------------------------- //
// Remembered so the script holds off while you're mid-login (a parallel
// background login would make your form stale).

async function noteIdpActivity() {
  await chrome.storage.session.set({ idpActivity: Date.now() });
}

async function loginInProgress() {
  const { idpActivity } = await chrome.storage.session.get("idpActivity");
  return !!idpActivity && Date.now() - idpActivity < LOGIN_BUSY_MS;
}

// --- Push ------------------------------------------------------------------ //

async function push(reason) {
  const cookies = await gather();
  let reply;
  try {
    reply = await post("/cookies", { reason, cookies, loginInProgress: await loginInProgress() });
  } catch (e) {
    console.debug("elentra-keepalive: push failed:", e.message);
    setBadge("off");
    return null;
  }
  reply = reply || {};
  setBadge(reply.state);
  if (reply.action && reply.action.type === "refresh") silentRefresh(reply.action.url);
  // After a wake/resync the script decides on a re-login a moment later; come
  // back for it instead of waiting for the 1-minute alarm.
  if ((reason === "resume" || reason === "resync") && reply.state !== "alive") {
    setTimeout(() => push("followup"), 15000);
  }
  return reply;
}

// --- Background re-login --------------------------------------------------- //
// State lives in storage.session so it survives service-worker restarts; the
// saved login is kept only in memory, only for the duration of one attempt.

let creds = null;

async function getRefresh() {
  const { refresh } = await chrome.storage.session.get("refresh");
  return refresh;
}

async function setRefresh(patch) {
  const cur = (await getRefresh()) || {};
  await chrome.storage.session.set({ refresh: { ...cur, ...patch } });
}

async function silentRefresh(url) {
  let target;
  try {
    target = new URL(url);
  } catch (e) {
    return;
  }
  if (target.origin !== ELENTRA_ORIGIN) return;
  if (await getRefresh()) return; // one at a time
  if (await loginInProgress()) {
    report("refresh-skipped", { why: "you have a CWL login page open" });
    return;
  }
  creds = null;
  const tab = await chrome.tabs.create({ url: target.href, active: false });
  await chrome.storage.session.set({
    refresh: { tabId: tab.id, sawIdp: false, userSent: false, pwSent: false, clicked: false, last: safe(target.href) },
  });
  chrome.alarms.create("refresh-timeout", { delayInMinutes: REFRESH_TIMEOUT_MIN });
  report("refresh-opened", { url: safe(target.href) });
}

async function finishRefresh(result, detail = {}, close = true) {
  const r = await getRefresh();
  if (!r) return;
  creds = null;
  await chrome.storage.session.remove("refresh");
  chrome.alarms.clear("refresh-timeout");
  if (close) chrome.tabs.remove(r.tabId).catch(() => {});
  await report(result, { last: r.last, sawIdp: r.sawIdp, ...detail });
  push("refresh-done");
}

async function fetchCreds() {
  if (creds) return creds;
  try {
    const c = await post("/credentials", {});
    if (c && c.username && c.password) creds = c;
  } catch (e) {}
  return creds;
}

// Runs inside the page. Describes the page for the log and, if given a
// username/password, fills the visible login form and submits it.
function pageAgent(mode, username, password) {
  const visible = (el) => !!el && !el.disabled && el.getClientRects().length > 0;
  const pw = [...document.querySelectorAll("input[type=password]")].find(visible);
  const scope = (pw && pw.form) || document;
  const user = [...scope.querySelectorAll("input[type=text],input[type=email],input:not([type])")].find(visible);
  const errEl = [...document.querySelectorAll("[class*=error],[class*=alert],[role=alert]")].find(
    (e) => visible(e) && e.textContent.trim()
  );
  const info = {
    title: document.title.slice(0, 80),
    hasPassword: !!pw,
    hasUser: !!user,
    error: errEl ? errEl.textContent.trim().replace(/\s+/g, " ").slice(0, 160) : "",
    forms: [...document.forms].map((f) => {
      try {
        const u = new URL(f.action, location.href);
        return u.hostname + u.pathname;
      } catch (e) {
        return "?";
      }
    }),
  };

  if (mode === "elentra") {
    // Logged in already? (a visible log-out control) — nothing to click.
    const all = [...document.querySelectorAll("a,button")].filter(visible);
    info.loggedIn = all.some((el) => /\b(log|sign)\s?out\b/i.test(el.textContent || ""));
    if (info.loggedIn) return info;
    // Elentra's own sign-in page: click through to CWL.
    const cands = [...document.querySelectorAll("a[href],button,input[type=submit]")].filter(visible);
    const pick =
      cands.find((el) => (el.href || "").includes("authentication.ubc.ca")) ||
      cands.find((el) => /\bcwl\b/i.test(el.textContent || el.value || "")) ||
      cands.find((el) => /\b(log|sign)\s?in\b/i.test(el.textContent || el.value || "") && !/out/i.test(el.textContent || ""));
    info.clicked = pick ? (pick.textContent || pick.value || "").trim().slice(0, 40) : "";
    if (pick) pick.click();
    return info;
  }

  if (mode === "fill") {
    // Re-check here: the tab may have navigated since the caller looked, and
    // the login must only ever be posted back to CWL itself.
    const form0 = (pw || user || {}).form;
    const action = form0 ? new URL(form0.action, location.href) : null;
    if (location.protocol !== "https:" || location.hostname !== "authentication.ubc.ca" || !action || action.origin !== location.origin) {
      info.refused = true;
      return info;
    }
    const set = (el, v) => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(el, v);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    };
    if (user && username) set(user, username);
    if (pw && password) set(pw, password);
    const form = (pw || user || {}).form;
    info.form = `${(form0.method || "get").toUpperCase()} ${action.hostname}${action.pathname}`;
    const btn = form && form.querySelector("button[type=submit],input[type=submit],button:not([type])");
    if (btn) btn.click();
    else if (form) form.requestSubmit();
    info.submitted = !!form;
  }
  return info;
}

async function runAgent(tabId, mode, username = null, password = null) {
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId }, func: pageAgent, args: [mode, username, password] });
    return res && res.result;
  } catch (e) {
    report("refresh-agent-error", { mode, error: String(e.message).slice(0, 120) });
    return null;
  }
}

async function onRefreshPage(tabId, url) {
  const r = await getRefresh();
  if (!r || r.tabId !== tabId) return;
  const host = hostOf(url);
  await setRefresh({ last: safe(url) });

  if (host === ELENTRA_HOST) {
    if (r.sawIdp) return finishRefresh("refresh-returned");
    // Landed on Elentra without visiting CWL: either still logged in, or this
    // is Elentra's own sign-in page.
    if (r.clicked) return; // already clicked through once; let the timeout decide
    await setRefresh({ clicked: true });
    const info = await runAgent(tabId, "elentra");
    report("refresh-elentra-page", { url: safe(url), ...info });
    if (info && info.loggedIn) return finishRefresh("refresh-returned", { note: "already logged in" });
    return;
  }

  if (host !== IDP_HOST) {
    report("refresh-other-page", { url: safe(url) });
    return;
  }

  await setRefresh({ sawIdp: true });
  const info = await runAgent(tabId, "describe");
  if (!info) return;
  report("refresh-cwl-page", { url: safe(url), ...info });

  if (info.hasPassword) {
    // The form came back after we already sent the password: wrong login.
    // Never resubmit — repeated failures can lock the CWL account.
    if (r.pwSent) return finishRefresh("refresh-login-rejected", { error: info.error });
    const c = await fetchCreds();
    if (!c) return finishRefresh("refresh-no-login", { why: "CWL asked for a password; no saved login (or it's blocked)" });
    await setRefresh({ pwSent: true, userSent: true });
    const res = await runAgent(tabId, "fill", c.username, c.password);
    report("refresh-submitted", { step: "password", submitted: !!(res && res.submitted), refused: !!(res && res.refused), form: res && res.form });
  } else if (info.hasUser) {
    // Two-step form (username first).
    if (r.userSent) return finishRefresh("refresh-login-rejected", { error: info.error, step: "username" });
    const c = await fetchCreds();
    if (!c) return finishRefresh("refresh-no-login", { why: "CWL asked for a username; no saved login (or it's blocked)" });
    await setRefresh({ userSent: true });
    const res = await runAgent(tabId, "fill", c.username, null);
    report("refresh-submitted", { step: "username", submitted: !!(res && res.submitted), refused: !!(res && res.refused) });
  }
  // Otherwise (redirect page, MFA prompt...) wait for the next page or the timeout.
}

chrome.tabs.onUpdated.addListener(async (tabId, info, tab) => {
  const url = info.url || tab.url;
  if (!url) return;
  const r = await getRefresh();
  if (r && r.tabId === tabId) {
    if (info.status === "complete") onRefreshPage(tabId, tab.url);
    return;
  }
  if (hostOf(url) === IDP_HOST) noteIdpActivity();
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const r = await getRefresh();
  if (r && r.tabId === tabId) finishRefresh("refresh-closed", { why: "tab closed" }, false);
});

// --- Triggers --------------------------------------------------------------- //

let pending = null;
function schedule(reason) {
  if (pending) clearTimeout(pending);
  pending = setTimeout(() => {
    pending = null;
    push(reason);
  }, 50);
}

chrome.cookies.onChanged.addListener((info) => {
  if (isElentra(info.cookie.domain)) schedule("cookie-change");
});

// Fires when the screen unlocks / the computer wakes and you come back.
chrome.idle.onStateChanged.addListener((state) => {
  if (state === "active") push("resume");
});

chrome.action.onClicked.addListener(() => push("resync"));

chrome.runtime.onInstalled.addListener(() => push("installed"));
chrome.runtime.onStartup.addListener(() => push("startup"));

chrome.alarms.onAlarm.addListener(async (a) => {
  if (a.name === "push") push("alarm");
  else if (a.name === "refresh-timeout") {
    const r = await getRefresh();
    finishRefresh("refresh-timeout", { stuckAt: r ? r.last : "?" });
  }
});

// Create the alarm only if missing — re-creating it on every worker start-up
// would keep resetting its 1-minute schedule.
chrome.alarms.get("push").then((a) => {
  if (!a) chrome.alarms.create("push", { periodInMinutes: 1 });
});
