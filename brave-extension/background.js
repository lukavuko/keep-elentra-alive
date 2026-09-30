// Elentra Keepalive Cookie Bridge — MV3 service worker.
//
// Reads the UBC Elentra session cookies (including httpOnly ones the page's
// document.cookie can't see) and POSTs them to the local keepalive script.
// Fires on cookie changes (you log in / out / they rotate), on a 1-minute
// alarm, and on startup — so the script is fed whether it or the browser
// started first.
//
// The script's reply drives the toolbar badge (ON / … / OFF) and can ask for a
// silent re-login: Elentra is loaded in a background tab so a still-valid SSO
// session re-issues the Elentra cookies, then the tab is closed.
//
// Keep PORT and TOKEN in sync with elentra_keepalive.py (LISTEN_PORT, PUSH_TOKEN).

const PORT = 8765;
const ENDPOINT = `http://127.0.0.1:${PORT}/cookies`;
const TOKEN = "elentra-keepalive-local";

// The only origin a refresh tab may ever be opened on, whatever the script asks.
const ELENTRA_ORIGIN = "https://ubc.elentra.cloud";

// Cookies we care about live on these registrable domains (gather() queries
// ubc.elentra.cloud specifically, which also matches its subdomains).
const DOMAINS = ["ubc.elentra.cloud", "authentication.ubc.ca"];

const BADGES = {
  alive: ["ON", "#2e7d32", "Elentra keepalive: session alive"],
  waiting: [
    "…",
    "#f9a825",
    "Elentra keepalive: running, waiting for a live Elentra session",
  ],
  off: ["OFF", "#757575", "Elentra keepalive: script not running"],
};

function setBadge(state) {
  const [text, color, title] = BADGES[state] || BADGES.waiting;
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
  chrome.action.setTitle({ title });
}

function relevant(domain) {
  if (!domain) return false;
  const d = domain.replace(/^\./, "");
  return DOMAINS.some((base) => d === base || d.endsWith("." + base));
}

async function gather() {
  const seen = new Set();
  const out = [];
  for (const domain of DOMAINS) {
    let list = [];
    try {
      list = await chrome.cookies.getAll({ domain });
    } catch (e) {
      // ignore; domain may simply have no cookies
    }
    for (const c of list) {
      const key = `${c.name} ${c.domain} ${c.path}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({
        name: c.name,
        value: c.value,
        domain: c.domain,
        path: c.path,
        secure: c.secure,
        httpOnly: c.httpOnly,
      });
    }
  }
  return out;
}

async function push(reason) {
  const cookies = await gather();
  let reply = {};
  try {
    const res = await fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: TOKEN, reason, cookies }),
    });
    reply = await res.json().catch(() => ({}));
  } catch (e) {
    // Script isn't running yet — that's fine; the alarm will retry.
    console.debug("elentra-keepalive: push failed:", e.message);
    setBadge("off");
    return;
  }
  setBadge(reply.state);
  if (reply.action && reply.action.type === "refresh")
    silentRefresh(reply.action.url);
}

// --- Silent re-login ------------------------------------------------------ //
// The refresh tab id lives in storage.session so it survives service-worker
// restarts; the timeout is an alarm for the same reason.

async function refreshTabId() {
  const { refreshTab } = await chrome.storage.session.get("refreshTab");
  return refreshTab;
}

async function silentRefresh(url) {
  let target;
  try {
    target = new URL(url);
  } catch (e) {
    return;
  }
  if (target.origin !== ELENTRA_ORIGIN) return;
  if ((await refreshTabId()) !== undefined) return; // one at a time

  const tab = await chrome.tabs.create({ url: target.href, active: false });
  await chrome.storage.session.set({ refreshTab: tab.id });
  chrome.alarms.create("refresh-timeout", { delayInMinutes: 1 });
}

async function endRefresh(close) {
  const id = await refreshTabId();
  if (id === undefined) return;
  await chrome.storage.session.remove("refreshTab");
  chrome.alarms.clear("refresh-timeout");
  if (close) chrome.tabs.remove(id).catch(() => {});
}

// The SSO round trip passes through the IdP; it's done once the tab settles
// back on Elentra. Cookie changes along the way trigger pushes on their own.
chrome.tabs.onUpdated.addListener(async (tabId, info, tab) => {
  if (info.status !== "complete" || !tab.url) return;
  if (tabId !== (await refreshTabId())) return;
  if (new URL(tab.url).origin === ELENTRA_ORIGIN) endRefresh(true);
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  if (tabId === (await refreshTabId())) endRefresh(false);
});

// --- Triggers --------------------------------------------------------------- //

// Debounce bursts of cookie changes into a single push. Kept short — MV3
// service workers can be torn down while a longer timer is still pending.
let pending = null;
function schedule(reason) {
  if (pending) clearTimeout(pending);
  pending = setTimeout(() => {
    pending = null;
    push(reason);
  }, 50);
}

chrome.cookies.onChanged.addListener((info) => {
  if (relevant(info.cookie.domain)) schedule("cookie-change");
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("push", { periodInMinutes: 1 });
  push("installed");
});

chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create("push", { periodInMinutes: 1 });
  push("startup");
});

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "push") push("alarm");
  else if (a.name === "refresh-timeout") endRefresh(true);
});

// Ensure the alarm exists whenever the worker spins up. Don't push here too —
// onInstalled/onStartup/onAlarm already cover it, and the alarm firing is
// itself what respawns the worker, so an unconditional push here doubles up
// with the onAlarm handler for the same tick.
chrome.alarms.create("push", { periodInMinutes: 1 });
