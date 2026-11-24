/* SiftStack Dialer inside DataSift (app.reisift.io).
 *
 * Two things, and nothing else:
 *   1. A small Call button beside every phone number on the page. DataSift is a React app and owns its own
 *      text, so the number itself is never edited: a button is added next to it, and when DataSift redraws
 *      a part of the page that part is scanned again. Clicking sends the number (and the record it belongs
 *      to) to the dialer tab through the extension.
 *   2. A call bar in the corner, in its own shadow root so neither page's styles can touch the other's. It
 *      shows the call the dialer tab is on, its controls, and the outcome buttons once it ends.
 * It reads nothing from the page beyond phone numbers and the record id in a link or the address bar, and it
 * sends nothing anywhere but the extension.
 */
"use strict";
(function () {
  if (window.__siftDialerSift) { return; }
  window.__siftDialerSift = true;

  var PHONE = /(?:\+?1[\s.\-]?)?\(?([2-9]\d{2})\)?[\s.\-]?([2-9]\d{2})[\s.\-]?(\d{4})(?!\d)/g;
  var UUID = /\/records\/properties\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i;
  var SKIP = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, INPUT: 1, SELECT: 1, OPTION: 1, NOSCRIPT: 1, CODE: 1, PRE: 1, svg: 1, SVG: 1, TITLE: 1 };
  var seen = new WeakSet();
  var state = null, showBar = true, collapsed = false, timer = 0;

  function send(msg) {
    try { return chrome.runtime.sendMessage(Object.assign({ from: "sift" }, msg)); } catch (e) { return Promise.resolve({ ok: false }); }
  }

  function pretty(d) { return "(" + d.slice(0, 3) + ") " + d.slice(3, 6) + "-" + d.slice(6); }

  function recordFromUrl() { var m = location.pathname.match(UUID); return m ? m[1] : ""; }

  function recordNear(node) {
    var el = node.parentElement;
    var row = el && el.closest("tr, [role='row'], li");
    if (row) {
      var links = row.querySelectorAll("a[href*='/records/properties/']");
      for (var i = 0; i < links.length; i++) {
        var m = (links[i].getAttribute("href") || "").match(UUID);
        if (m) { return m[1]; }
      }
    }
    return recordFromUrl();
  }

  // ------------------------------------------------------------------ call buttons
  var BTN_CSS = "all:initial;display:inline-flex;align-items:center;justify-content:center;margin-left:6px;height:22px;" +
    "padding:0 8px;border-radius:4px;border:1px solid #316AFF;background:#316AFF;color:#fff;" +
    "font:600 12px/1 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;cursor:pointer;" +
    "vertical-align:middle;white-space:nowrap;";

  function button(number, record, label) {
    var b = document.createElement("button");
    b.type = "button";
    b.dataset.siftDialer = "1";
    b.dataset.number = number;
    b.textContent = label;
    b.title = "Call " + pretty(number) + " with the SiftStack dialer";
    b.setAttribute("aria-label", b.title);
    b.style.cssText = BTN_CSS;
    b.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      send({ type: "dial", number: number, record_uuid: record || recordFromUrl() });
      b.textContent = "Calling";
      setTimeout(function () { b.textContent = label; }, 2500);
    }, true);
    return b;
  }

  function numbersIn(text) {
    var out = [], m;
    PHONE.lastIndex = 0;
    while ((m = PHONE.exec(text)) !== null) {
      var d = m[1] + m[2] + m[3];
      if (out.indexOf(d) < 0) { out.push(d); }
      if (out.length >= 4) { break; }
    }
    return out;
  }

  function scan(root) {
    if (!root || !root.nodeType) { return; }
    if (root.nodeType === 3) { root = root.parentNode; }
    if (!root || root.nodeType !== 1 || root.closest && root.closest("[data-sift-dialer]")) { return; }
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        if (seen.has(n)) { return NodeFilter.FILTER_REJECT; }
        var p = n.parentElement;
        if (!p || SKIP[p.tagName] || p.isContentEditable || p.closest("[data-sift-dialer]")) { return NodeFilter.FILTER_REJECT; }
        var v = n.nodeValue || "";
        if (v.length < 10 || v.length > 500) { return NodeFilter.FILTER_REJECT; }
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var hits = [];
    while (walker.nextNode()) { hits.push(walker.currentNode); }
    hits.forEach(function (n) {
      var nums = numbersIn(n.nodeValue || "");
      if (!nums.length) { return; }
      seen.add(n);
      var after = n.nextSibling;
      var record = recordNear(n);
      nums.forEach(function (d) {
        if (after && after.dataset && after.dataset.siftDialer && after.dataset.number === d) { return; }
        var label = nums.length > 1 ? "Call " + d.slice(6) : "Call";
        n.parentNode.insertBefore(button(d, record, label), n.nextSibling);
      });
    });
  }

  var pending = [], scanTimer = 0;
  function queueScan(node) {
    pending.push(node);
    if (scanTimer) { return; }
    scanTimer = setTimeout(function () {
      var roots = pending;
      pending = [];
      scanTimer = 0;
      roots.forEach(scan);
    }, 250);
  }

  var observer = new MutationObserver(function (records) {
    for (var i = 0; i < records.length; i++) {
      var r = records[i];
      if (r.type === "characterData") { queueScan(r.target); continue; }
      for (var j = 0; j < r.addedNodes.length; j++) {
        var n = r.addedNodes[j];
        if (n.nodeType === 1 && n.dataset && n.dataset.siftDialer) { continue; }
        queueScan(n);
      }
    }
  });

  var lastUrl = location.href;
  setInterval(function () {
    if (location.href !== lastUrl) { lastUrl = location.href; queueScan(document.body); }
  }, 1000);

  // ------------------------------------------------------------------ the call bar
  var host, root;
  var BAR_CSS = [
    ":host{all:initial}",
    ".bar{font:13px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0A1130;",
    "background:#fff;border:1px solid #E2E6EF;border-radius:12px;box-shadow:0 1px 2px rgba(10,17,48,.06);",
    "width:300px;max-width:calc(100vw - 32px);overflow:hidden}",
    ".hd{background:#0A1130;color:#fff;display:flex;align-items:center;gap:8px;padding:8px 10px}",
    ".hd b{font-size:13px;flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
    ".hd button{all:initial;color:#fff;font-family:inherit;font-size:12px;font-weight:600;line-height:1;cursor:pointer;padding:4px 6px;border-radius:4px;border:1px solid rgba(255,255,255,.3)}",
    ".bd{padding:10px}",
    ".st{font-size:12px;font-weight:700;color:#4A5372}",
    ".who{font-size:15px;font-weight:700;overflow-wrap:anywhere}",
    ".num{color:#4A5372;font-variant-numeric:tabular-nums}",
    ".tm{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}",
    ".row{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;margin-top:8px}",
    ".btn{all:initial;box-sizing:border-box;font-family:inherit;font-size:13px;font-weight:600;color:#0A1130;background:#fff;",
    "border:1px solid #CDD3E0;border-radius:6px;min-height:32px;padding:6px 8px;text-align:center;cursor:pointer;white-space:normal;line-height:1.2}",
    ".btn:hover{background:#EEF0F6}.btn:active{transform:translateY(1px)}",
    ".btn.dark{background:#0A1130;border-color:#0A1130;color:#fff}.btn.blue{background:#316AFF;border-color:#316AFF;color:#fff}",
    ".btn.sug{border:2px solid #316AFF}.btn[disabled]{opacity:.45;cursor:not-allowed}",
    ".lbl{font-size:12px;font-weight:700;color:#4A5372;margin-top:10px}",
    ".muted{color:#5E6785}",
    ".live{border-left:3px solid #1B9E5A;padding-left:8px}"
  ].join("");

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text !== undefined && text !== null) { e.textContent = String(text); }
    return e;
  }

  function mountBar() {
    if (host) { return; }
    host = document.createElement("div");
    host.dataset.siftDialer = "1";
    host.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483000;";
    root = host.attachShadow({ mode: "open" });
    var style = document.createElement("style");
    style.textContent = BAR_CSS;
    root.appendChild(style);
    (document.body || document.documentElement).appendChild(host);
  }

  function clock(secs) {
    secs = Math.max(0, Math.floor(secs));
    var m = Math.floor(secs / 60), s = secs % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function control(label, type, cls, extra) {
    var b = el("button", "btn" + (cls ? " " + cls : ""), label);
    b.type = "button";
    b.addEventListener("click", function () { send(Object.assign({ type: type }, extra || {})); });
    return b;
  }

  function render() {
    if (!showBar) { if (host) { host.hidden = true; } return; }
    mountBar();
    host.hidden = false;
    while (root.childNodes.length > 1) { root.removeChild(root.lastChild); }
    clearInterval(timer);
    var bar = el("div", "bar");
    var hd = el("div", "hd");
    var c = state && state.call;
    hd.appendChild(el("b", "", c ? (c.owner || c.pretty) : "SiftStack dialer"));
    var tog = el("button", "", collapsed ? "Show" : "Hide");
    tog.type = "button";
    tog.addEventListener("click", function () { collapsed = !collapsed; render(); });
    hd.appendChild(tog);
    bar.appendChild(hd);
    if (!collapsed) {
      var bd = el("div", "bd");
      if (!state) {
        bd.appendChild(el("div", "muted", "The dialer tab is closed. Click a Call button, or open it here."));
        var row = el("div", "row");
        var open = el("button", "btn blue", "Open the dialer");
        open.type = "button";
        open.addEventListener("click", function () { send({ type: "open_dialer" }); });
        row.appendChild(open);
        bd.appendChild(row);
      } else if (!c) {
        bd.appendChild(el("div", "st", state.ready ? "Phone ready" : "The phone is connecting"));
        bd.appendChild(el("div", "muted", "Click Call beside any number on this page."));
        if (state.session) {
          bd.appendChild(el("div", "lbl", "Power dialer: " + state.session.preset));
          var r2 = el("div", "row");
          r2.appendChild(control("Dial next", "next", "blue"));
          bd.appendChild(r2);
        }
      } else {
        var live = c.status === "answered";
        var box = el("div", live ? "live" : "");
        var label = { prepared: "Connecting", connecting: "Connecting", ringing: "Ringing", answered: "Talking",
                      ended: "Ended", failed: "Could not connect" }[c.status] || c.status;
        box.appendChild(el("div", "st", label));
        box.appendChild(el("div", "num", c.pretty + (c.street ? ", " + c.street : "")));
        if (live && c.answered_at) {
          var tm = el("div", "tm", "0:00");
          box.appendChild(tm);
          var start = Date.parse(c.answered_at);
          var tick = function () { tm.textContent = clock((Date.now() - start) / 1000); };
          tick();
          timer = setInterval(tick, 1000);
        }
        bd.appendChild(box);
        if (["prepared", "connecting", "ringing", "answered"].indexOf(c.status) >= 0) {
          var r = el("div", "row");
          r.appendChild(control(state.muted ? "Unmute" : "Mute", "mute"));
          var h = control(state.held ? "Resume" : "Hold", "hold");
          if (!live) { h.disabled = true; }
          r.appendChild(h);
          if (state.vm_drop) { var v = control("Leave voicemail", "vmdrop"); if (!live) { v.disabled = true; } r.appendChild(v); }
          r.appendChild(control("Hang up", "hangup", "dark"));
          bd.appendChild(r);
        }
        if (state.awaiting_outcome) {
          bd.appendChild(el("div", "lbl", "How did it go?"));
          var grid = el("div", "row");
          (state.outcomes || []).forEach(function (o) {
            var b = control(o.label + ((o.needs_note || o.task) ? " ..." : ""), "outcome", o.key === state.suggest ? "sug" : "", { key: o.key });
            grid.appendChild(b);
          });
          bd.appendChild(grid);
        }
      }
      bar.appendChild(bd);
    }
    root.appendChild(bar);
  }

  chrome.runtime.onMessage.addListener(function (msg) {
    if (!msg || msg.to !== "sift") { return; }
    if (msg.type === "state") { state = msg.state || null; render(); }
  });

  function start() {
    queueScan(document.body);
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    chrome.storage.sync.get({ bar: true }, function (s) {
      showBar = s.bar !== false;
      send({ type: "get_state" }).then(function (r) { if (r && r.ok) { state = r.state || null; } render(); }, render);
    });
    chrome.storage.onChanged.addListener(function (changes, area) {
      if (area === "sync" && changes.bar) { showBar = changes.bar.newValue !== false; render(); }
    });
  }

  if (document.body) { start(); } else { document.addEventListener("DOMContentLoaded", start); }
})();
