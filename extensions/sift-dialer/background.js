/* The extension's service worker: a switchboard between DataSift tabs and the dialer tab.
 *
 * It holds no keys and never talks to the dialer server. A call button in DataSift sends a message here;
 * this finds the dialer tab (opening it, pinned, if it is not open) and hands the message to the relay
 * in that tab, which passes it to the page. The page's state comes back the same way and is sent to every
 * DataSift tab so its call bar can show it. When the power dialer moves to a record, the DataSift tab the
 * caller used last is moved to that record. Chrome may stop this worker whenever it is idle, so what it
 * must remember lives in chrome.storage.session.
 */
"use strict";

const DEFAULT_URL = "https://siftstack-dialer.fly.dev";
const SIFT_ORIGIN = "https://app.reisift.io";

async function settings() {
  const s = await chrome.storage.sync.get({ dialerUrl: DEFAULT_URL, follow: true, bar: true });
  s.dialerUrl = String(s.dialerUrl || DEFAULT_URL).replace(/\/+$/, "");
  return s;
}

async function mem() {
  return chrome.storage.session.get({ dialerTab: 0, siftTab: 0, pending: [], state: null, bridged: false });
}

async function remember(patch) { await chrome.storage.session.set(patch); }

async function findDialerTab(url) {
  const { dialerTab } = await mem();
  if (dialerTab) {
    try {
      const t = await chrome.tabs.get(dialerTab);
      if (t && t.url && t.url.startsWith(url + "/phone")) { return t; }
    } catch (e) { /* the tab is gone */ }
  }
  const tabs = await chrome.tabs.query({ url: url + "/phone*" });
  if (tabs.length) {
    await remember({ dialerTab: tabs[0].id });
    return tabs[0];
  }
  return null;
}

async function toDialer(msg) {
  const s = await settings();
  const tab = await findDialerTab(s.dialerUrl);
  const m = await mem();
  if (tab && m.bridged) {
    try {
      await chrome.tabs.sendMessage(tab.id, Object.assign({ to: "page" }, msg));
      return { ok: true };
    } catch (e) { /* the relay is not loaded yet: queue it */ }
  }
  const pending = (m.pending || []).concat([msg]).slice(-5);
  await remember({ pending: pending });
  if (!tab) {
    const t = await chrome.tabs.create({ url: s.dialerUrl + "/phone", pinned: true, active: msg.type === "dial" });
    await remember({ dialerTab: t.id, bridged: false });
    return { ok: true, opened: true };
  }
  if (msg.type === "dial") { await chrome.tabs.update(tab.id, { active: true }); }
  return { ok: true, queued: true };
}

async function flushPending(tabId) {
  const m = await mem();
  const pending = m.pending || [];
  await remember({ pending: [] });
  for (const msg of pending) {
    try { await chrome.tabs.sendMessage(tabId, Object.assign({ to: "page" }, msg)); } catch (e) { /* dropped with the tab */ }
  }
}

async function toSiftTabs(msg) {
  const tabs = await chrome.tabs.query({ url: SIFT_ORIGIN + "/*" });
  for (const t of tabs) {
    try { await chrome.tabs.sendMessage(t.id, Object.assign({ to: "sift" }, msg)); } catch (e) { /* no content script there yet */ }
  }
}

async function openRecord(link, uuid) {
  const s = await settings();
  if (!s.follow || !uuid) { return; }
  const target = link && link.startsWith(SIFT_ORIGIN + "/") ? link : SIFT_ORIGIN + "/records/properties/" + uuid;
  const m = await mem();
  let tab = null;
  if (m.siftTab) {
    try { tab = await chrome.tabs.get(m.siftTab); } catch (e) { tab = null; }
  }
  if (!tab) {
    const tabs = await chrome.tabs.query({ url: SIFT_ORIGIN + "/*" });
    tab = tabs[0] || null;
  }
  if (!tab) { return; }
  if (tab.url && tab.url.indexOf("/records/properties/" + uuid) >= 0) { return; }
  await chrome.tabs.update(tab.id, { url: target });
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    if (!msg || typeof msg !== "object") { return reply({ ok: false }); }
    const tab = sender.tab || {};
    if (msg.from === "sift") {
      if (tab.id) { await remember({ siftTab: tab.id }); }
      if (msg.type === "get_state") { const m = await mem(); const s = await settings(); return reply({ ok: true, state: m.state, bar: s.bar }); }
      if (msg.type === "open_dialer") {
        const s = await settings();
        const t = await findDialerTab(s.dialerUrl);
        if (t) { await chrome.tabs.update(t.id, { active: true }); } else { await chrome.tabs.create({ url: s.dialerUrl + "/phone", pinned: true }); }
        return reply({ ok: true });
      }
      if (["dial", "hangup", "mute", "hold", "vmdrop", "outcome", "next"].indexOf(msg.type) >= 0) {
        return reply(await toDialer(msg));
      }
      return reply({ ok: false, error: "unknown" });
    }
    if (msg.from === "page") {
      const s = await settings();
      // Only the dialer's own page may speak as the page.
      if (!tab.url || !tab.url.startsWith(s.dialerUrl + "/phone")) { return reply({ ok: false }); }
      if (msg.type === "bridge_ready") {
        await remember({ dialerTab: tab.id, bridged: true });
        await flushPending(tab.id);
        return reply({ ok: true });
      }
      if (msg.type === "state") {
        await remember({ state: msg.state || null });
        await toSiftTabs({ type: "state", state: msg.state || null });
        return reply({ ok: true });
      }
      if (msg.type === "open_record") {
        await openRecord(msg.link || "", msg.uuid || "");
        return reply({ ok: true });
      }
    }
    return reply({ ok: false });
  })();
  return true;
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const m = await mem();
  if (tabId === m.dialerTab) {
    await remember({ dialerTab: 0, bridged: false, state: null });
    await toSiftTabs({ type: "state", state: null });
  }
  if (tabId === m.siftTab) { await remember({ siftTab: 0 }); }
});

chrome.action.onClicked.addListener(async () => {
  const s = await settings();
  const t = await findDialerTab(s.dialerUrl);
  if (t) { await chrome.tabs.update(t.id, { active: true }); } else { await chrome.tabs.create({ url: s.dialerUrl + "/phone", pinned: true }); }
});

// A dialer at an address other than the built-in ones gets its relay registered here, once the person has
// granted that one site in the options page (Chrome asks them).
const BUILT_IN = ["https://siftstack-dialer.fly.dev", "http://127.0.0.1", "http://localhost"];

async function syncBridge() {
  const s = await settings();
  const origin = new URL(s.dialerUrl).origin;
  const builtIn = BUILT_IN.some((b) => origin === b || origin.startsWith(b + ":"));
  const have = await chrome.scripting.getRegisteredContentScripts({ ids: ["bridge-custom"] }).catch(() => []);
  if (have.length) { await chrome.scripting.unregisterContentScripts({ ids: ["bridge-custom"] }).catch(() => {}); }
  if (builtIn) { return; }
  const granted = await chrome.permissions.contains({ origins: [origin + "/*"] });
  if (!granted) { return; }
  await chrome.scripting.registerContentScripts([{ id: "bridge-custom", matches: [origin + "/phone*"], js: ["bridge.js"],
                                                  runAt: "document_start", persistAcrossSessions: true }]);
}

chrome.storage.onChanged.addListener((changes, area) => { if (area === "sync" && changes.dialerUrl) { syncBridge(); } });
chrome.permissions.onAdded.addListener(() => { syncBridge(); });

chrome.runtime.onInstalled.addListener(async (info) => {
  await syncBridge();
  if (info.reason === "install") { chrome.runtime.openOptionsPage(); }
});
chrome.runtime.onStartup.addListener(() => { syncBridge(); });
