/* SiftStack dialer: the phone page.
 *
 * One Twilio Device for the whole session. Every call is prepared by the server first (the rules check,
 * the record), then connected with only its id as a parameter; the server decides everything else. The
 * server's events (ringing, answered, ended) drive what the caller sees; the Device's own events only
 * say whether the browser leg is up. Nothing a homeowner wrote is ever put into the page as HTML.
 */
"use strict";
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var JSON_HEADERS = { "Content-Type": "application/json", "X-Dialer": "1" };
  var S = {
    me: null, device: null, conn: null, side: null, call: null, record: null, number: "",
    muted: false, held: false, session: null, item: null, picked: "", outcomeFor: "",
    timerId: 0, countdownId: 0, incoming: null, incomingCallId: "", ready: false, ext: false, nextLoops: 0
  };

  // ------------------------------------------------------------------ small helpers
  function api(method, path, body) {
    var opt = { method: method, headers: method === "GET" ? {} : JSON_HEADERS, credentials: "same-origin" };
    if (body !== undefined) { opt.body = JSON.stringify(body); }
    return fetch(path, opt).then(function (r) {
      if (r.status === 401) { location.href = "/signin"; throw new Error("signed out"); }
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok && d.ok === undefined) { d.ok = false; }
        return d;
      });
    });
  }

  var toastTimer = 0;
  function toast(text, ms) {
    var t = $("toast");
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, ms || 4200);
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text !== undefined && text !== null) { e.textContent = String(text); }
    return e;
  }

  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }

  function digits(n) {
    var d = String(n || "").replace(/\D/g, "");
    if (d.length === 11 && d.charAt(0) === "1") { d = d.slice(1); }
    return d.length === 10 ? d : "";
  }

  function pretty(n) {
    var d = digits(n);
    return d ? "(" + d.slice(0, 3) + ") " + d.slice(3, 6) + "-" + d.slice(6) : String(n || "");
  }

  function clock(secs) {
    secs = Math.max(0, Math.floor(secs || 0));
    var m = Math.floor(secs / 60), s = secs % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function talkText(secs) {
    secs = Math.floor(secs || 0);
    if (!secs) { return ""; }
    var m = Math.floor(secs / 60), s = secs % 60;
    return (m ? m + "m " : "") + s + "s";
  }

  function lineState(text, good) {
    var e = $("lineState");
    e.textContent = text;
    e.className = "line-state " + (good ? "ok" : "bad");
    S.ready = !!good;
    broadcast();
  }

  // ------------------------------------------------------------------ the extension bridge
  // The SiftStack Chrome extension injects a small relay into this page. It passes call buttons from the
  // DataSift tab in, and this page's state out (so a call bar inside DataSift can show it). Messages from
  // anywhere else are ignored.
  function toExt(type, data) {
    var msg = { source: "sift-dialer-page", type: type };
    for (var k in (data || {})) { msg[k] = data[k]; }
    window.postMessage(msg, location.origin);
  }

  function snapshot() {
    var c = S.call;
    return {
      ready: S.ready, name: S.me ? S.me.name : "",
      call: c ? { id: c.id, status: c.status, number: c.number, pretty: c.pretty,
                  owner: (c.record && c.record.owner) || "", street: (c.record && c.record.street) || "",
                  answered_at: c.answered_at, seconds: c.seconds, end_status: c.end_status } : null,
      muted: S.muted, held: S.held, awaiting_outcome: !!S.outcomeFor, suggest: c ? c.suggest : "",
      outcomes: (S.me ? S.me.dispositions : []).map(function (r) { return { key: r.key, label: r.label, group: r.group, needs_note: r.needs_note, task: r.task }; }),
      session: S.session ? { preset: S.session.preset, counts: S.session.counts } : null,
      vm_drop: !!(S.me && S.me.settings.vm_drop === "on")
    };
  }

  function broadcast() { toExt("state", { state: snapshot() }); }

  window.addEventListener("message", function (ev) {
    if (ev.source !== window || ev.origin !== location.origin) { return; }
    var m = ev.data;
    if (!m || m.source !== "sift-dialer-ext") { return; }
    if (m.type === "hello") { S.ext = true; broadcast(); return; }
    if (m.type === "dial") { dial(m.number, m.record_uuid || "", {}); return; }
    if (m.type === "hangup") { hangup(); return; }
    if (m.type === "mute") { toggleMute(); return; }
    if (m.type === "hold") { toggleHold(); return; }
    if (m.type === "vmdrop") { vmDrop(); return; }
    if (m.type === "outcome") { chooseOutcome(m.key, true, m.note || ""); return; }
    if (m.type === "next") { queueDial(); return; }
  });

  function openInSift(rec) {
    if (rec && rec.uuid) { toExt("open_record", { uuid: rec.uuid, link: rec.link || "" }); }
  }

  // ------------------------------------------------------------------ boot
  function boot() {
    api("GET", "/api/me").then(function (me) {
      if (!me.ok) { return; }
      S.me = me;
      $("callerName").textContent = me.name;
      $("callerNumber").textContent = me.caller_id_pretty || "no outbound number yet";
      renderModes(me);
      buildOutcomes();
      $("vmBtn").hidden = me.settings.vm_drop !== "on";
      startEvents();
      loadPresets();
      loadSession();
      loadHot();
      loadRecent();
      if (me.role === "manager") {
        $("mgrBox").hidden = false;
        loadActive();
        setInterval(loadActive, 10000);
      }
      startDevice();
      broadcast();
    });
  }

  function renderModes(me) {
    var box = $("modes");
    clear(box);
    var s = me.settings;
    if (s.calls === "test") { box.appendChild(el("div", "note", "Test mode: only the test numbers can be dialed.")); }
    if (s.calls === "off") { box.appendChild(el("div", "note", "Calling is switched off on this dialer.")); }
    if (s.writeback === "dry") { box.appendChild(el("div", "note", "Write-back is dry: outcomes are logged here and nothing is written to DataSift yet.")); }
    if (s.writeback === "off") { box.appendChild(el("div", "note", "Write-back is off: nothing is written to DataSift.")); }
    if (!s.twilio_ready) { box.appendChild(el("div", "note", "Twilio is not set up on this dialer yet, so calls cannot connect.")); }
    (me.problems || []).forEach(function (p) { box.appendChild(el("div", "note info", p)); });
  }

  // ------------------------------------------------------------------ the Twilio Device
  function startDevice() {
    if (!window.Twilio || !window.Twilio.Device) { lineState("The phone library did not load", false); return; }
    api("GET", "/api/phone-token").then(function (t) {
      if (!t.ok) { lineState(t.error || "No phone token", false); return; }
      var opts = { codecPreferences: ["opus", "pcmu"], closeProtection: "A call is still connected. Leave anyway?",
                   maxCallSignalingTimeoutMs: 30000, logLevel: "warn" };
      if (S.me.settings.edge) { opts.edge = [S.me.settings.edge, "roaming"]; }
      var d = new window.Twilio.Device(t.token, opts);
      d.on("registered", function () { lineState("Phone ready", true); });
      d.on("unregistered", function () { lineState("Phone offline, reconnecting", false); setTimeout(function () { d.register().catch(function () {}); }, 3000); });
      d.on("error", function (e) { lineState("Phone problem: " + ((e && (e.message || e.code)) || "unknown"), false); });
      d.on("tokenWillExpire", function () { api("GET", "/api/phone-token").then(function (n) { if (n.ok) { d.updateToken(n.token); } }); });
      d.on("incoming", onIncoming);
      S.device = d;
      d.register().catch(function (e) { lineState("The phone could not register: " + e.message, false); });
    });
  }

  // ------------------------------------------------------------------ server events
  function startEvents() {
    var es = new EventSource("/api/events");
    es.addEventListener("call", function (e) { onServerCall(JSON.parse(e.data)); });
    es.addEventListener("conference", function (e) { onServerCall(JSON.parse(e.data)); });
    es.addEventListener("incoming", function (e) { var c = JSON.parse(e.data); S.incomingCallId = c.id; showIncomingRecord(c); });
    es.addEventListener("voicemail", function () { loadRecent(); toast("A missed call left a voicemail. It is under Today."); });
    es.addEventListener("handraise", function (e) { var h = JSON.parse(e.data); loadHot(); notify("Texted back interested", h.pretty + ": " + (h.body || "")); toast("Texted back interested: " + h.pretty); });
    es.addEventListener("vm_saved", function () { toast("Your voicemail message is saved."); loadMe(); });
    es.addEventListener("transfer_offer", function (e) { var c = JSON.parse(e.data); toast(c.from_caller + " is adding you to a call with " + ((c.record && c.record.owner) || c.pretty) + ". Answer when it rings.", 8000); });
    es.addEventListener("hold", function (e) { var h = JSON.parse(e.data); if (S.call && h.call_id === S.call.id) { S.held = !!h.on; renderControls(); } });
    es.addEventListener("monitor", function (e) { var m = JSON.parse(e.data); toast(m.by + " is listening to this call (" + m.mode + ")."); });
    es.onerror = function () { /* EventSource reconnects by itself, sending the last event id */ };
  }

  function loadMe() { api("GET", "/api/me").then(function (me) { if (me.ok) { S.me = me; } }); }

  function onServerCall(c) {
    if (!c || !c.id) { return; }
    if (S.call && c.id !== S.call.id) { if (c.status === "ended") { loadRecent(); } return; }
    if (!S.call) { return; }
    S.call = c;
    renderCall();
    // The card opens once per call, and never again after its outcome is saved (the server's own
    // confirmation of the outcome arrives as one more "ended" event for the same call).
    if ((c.status === "ended" || c.status === "failed") && !c.disposition && S.outcomeFor !== c.id) { callOver(); }
  }

  // ------------------------------------------------------------------ dialing
  function busy() { return !!(S.call && ["prepared", "connecting", "ringing", "answered"].indexOf(S.call.status) >= 0); }

  function dial(number, recordUuid, opts) {
    opts = opts || {};
    if (busy()) { toast("Finish this call first."); return; }
    if (S.outcomeFor) { toast("Save how the last call went first."); $("outcomeCard").scrollIntoView({ block: "nearest" }); return; }
    if (!S.device) { toast("The phone is not connected yet."); return; }
    askNotifications();
    var body = { number: String(number || ""), record_uuid: recordUuid || "" };
    if (opts.item) { body.item_id = opts.item.id; body.session_id = opts.item.session_id; }
    if (opts.confirm) { body.confirm = true; }
    $("dialBtn").disabled = true;
    api("POST", "/api/call/prepare", body).then(function (res) {
      $("dialBtn").disabled = false;
      if (!res.ok) {
        if (res.need_confirm) {
          confirmSheet("Before you call " + pretty(number), res.ask || []).then(function (yes) {
            if (yes) { opts.confirm = true; dial(number, recordUuid, opts); }
          });
          return;
        }
        stopSheet(res.stop || [res.error || "That number cannot be called."]);
        return;
      }
      S.call = res.call;
      S.muted = false; S.held = false;
      S.number = res.call.number;
      renderCall();
      if (res.call.record && res.call.record.uuid) { showRecord(res.call.record, res.call.number); } else { showRecord(null, res.call.number); }
      loadThread(res.call.number);
      openInSift(res.call.record);
      return S.device.connect({ params: { call_id: res.call.id } }).then(function (conn) {
        S.conn = conn;
        conn.on("disconnect", function () { S.conn = null; checkEnded(); });
        conn.on("cancel", function () { S.conn = null; checkEnded(); });
        conn.on("error", function (e) { toast("Call problem: " + (e.message || e.code)); });
        conn.on("warning", function (name) { if (name === "high-rtt" || name === "high-packet-loss" || name === "low-mos") { $("phoneAside").textContent = "Weak connection"; } });
        conn.on("warning-cleared", function () { $("phoneAside").textContent = ""; });
      });
    }).catch(function (e) { $("dialBtn").disabled = false; if (String(e.message) !== "signed out") { toast("The call could not start: " + e.message); } });
  }

  function checkEnded() {
    // The browser leg is gone; the server says how the homeowner's leg ended. Ask until it does.
    if (!S.call) { return; }
    var id = S.call.id, tries = 0;
    (function poll() {
      if (!S.call || S.call.id !== id || S.call.status === "ended" || S.call.status === "failed") { return; }
      api("GET", "/api/call/" + id).then(function (r) {
        if (r.ok && r.call) { onServerCall(r.call); }
        if (++tries < 15 && S.call && S.call.status !== "ended" && S.call.status !== "failed") { setTimeout(poll, 1000); }
      });
    })();
  }

  function hangup() {
    if (S.conn) { try { S.conn.disconnect(); } catch (e) { /* already gone */ } }
    if (S.call && busy()) { return api("POST", "/api/call/" + S.call.id + "/hangup", {}); }
    return Promise.resolve();
  }

  function toggleMute() {
    if (!S.conn) { return; }
    S.muted = !S.muted;
    S.conn.mute(S.muted);
    renderControls();
  }

  function toggleHold() {
    if (!S.call || S.call.status !== "answered") { return; }
    api("POST", "/api/call/" + S.call.id + "/hold", { on: !S.held }).then(function (r) {
      if (r.ok) { S.held = r.on; renderControls(); } else { toast(r.error || "Hold did not work."); }
    });
  }

  function vmDrop() {
    if (!S.call) { return; }
    api("POST", "/api/call/" + S.call.id + "/vmdrop", {}).then(function (r) {
      if (r.ok) { toast("Leaving your voicemail. You are free for the next call."); } else { toast(r.error || "The voicemail could not be left."); }
    });
  }

  // ------------------------------------------------------------------ the call box
  function renderCall() {
    var c = S.call, box = $("callBox");
    if (!c) { box.hidden = true; broadcast(); return; }
    box.hidden = false;
    var st = c.status, label = "Ready", cls = "callbox";
    if (st === "prepared" || st === "connecting") { label = "Connecting"; cls += " ringing"; }
    else if (st === "ringing") { label = "Ringing"; cls += " ringing"; }
    else if (st === "answered") { label = c.amd && c.amd.indexOf("machine") === 0 ? "Talking (a machine picked up)" : "Talking"; cls += " live"; }
    else if (st === "ended") { label = endLabel(c); cls += " ended"; }
    else if (st === "failed") { label = "Could not connect"; cls += " ended"; }
    box.className = cls;
    $("callState").textContent = label + (c.recorded && st === "answered" ? ", recording" : "");
    $("callWho").textContent = (c.record && c.record.owner) || "Not in DataSift";
    $("callNum").textContent = c.pretty + ((c.record && c.record.street) ? ", " + c.record.street : "");
    var w = $("callWarnings");
    clear(w);
    (c.warnings || []).forEach(function (x) { w.appendChild(el("li", "", x)); });
    if (st === "answered" && c.answered_at) {
      $("callTimer").hidden = false;
      startTimer(c.answered_at);
    } else if (st === "ended") {
      stopTimer();
      $("callTimer").hidden = !c.seconds;
      $("callTimer").textContent = clock(c.seconds);
    } else { stopTimer(); $("callTimer").hidden = true; }
    $("callControls").hidden = (st === "ended" || st === "failed");
    renderControls();
    broadcast();
  }

  function endLabel(c) {
    if (c.vm_dropped) { return "Voicemail left"; }
    var e = c.end_status;
    if (e === "completed") { return "Ended, talked " + talkText(c.seconds); }
    if (e === "no-answer") { return "No answer"; }
    if (e === "busy") { return "Busy"; }
    if (e === "failed") { return "The number did not connect"; }
    if (e === "canceled") { return "Hung up before an answer"; }
    if (e === "voicemail") { return "They left a voicemail"; }
    if (e === "never_connected") { return "The call never connected"; }
    return "Ended";
  }

  function renderControls() {
    var live = !!(S.call && S.call.status === "answered");
    var up = busy();
    $("muteBtn").disabled = !S.conn;
    $("muteBtn").textContent = S.muted ? "Unmute" : "Mute";
    $("muteBtn").classList.toggle("on", S.muted);
    $("holdBtn").disabled = !live;
    $("holdBtn").textContent = S.held ? "Resume" : "Hold";
    $("holdBtn").classList.toggle("on", S.held);
    $("vmBtn").disabled = !live;
    $("xferBtn").disabled = !live;
    $("hangBtn").disabled = !up && !S.conn;
    if (!live) { $("xferBox").hidden = true; }
  }

  function startTimer(fromIso) {
    stopTimer();
    var start = Date.parse(fromIso);
    var tick = function () { $("callTimer").textContent = clock((Date.now() - start) / 1000); };
    tick();
    S.timerId = setInterval(tick, 1000);
  }

  function stopTimer() { if (S.timerId) { clearInterval(S.timerId); S.timerId = 0; } }

  // ------------------------------------------------------------------ outcomes
  function buildOutcomes() {
    var groups = [["contact", "Reached the owner"], ["person", "Someone else answered"], ["no_contact", "Nobody to talk to"]];
    var box = $("outGroups");
    clear(box);
    groups.forEach(function (g) {
      var rows = S.me.dispositions.filter(function (r) { return r.group === g[0]; });
      if (!rows.length) { return; }
      var wrap = el("div", "outgroup");
      wrap.appendChild(el("h3", "", g[1]));
      var grid = el("div", "outs");
      rows.forEach(function (r) {
        var b = el("button", "btn", r.label);
        b.type = "button";
        b.dataset.key = r.key;
        b.addEventListener("click", function () { chooseOutcome(r.key, false, ""); });
        grid.appendChild(b);
      });
      wrap.appendChild(grid);
      box.appendChild(wrap);
    });
  }

  function rowFor(key) { return (S.me.dispositions || []).filter(function (r) { return r.key === key; })[0]; }

  function callOver() {
    stopTimer();
    if (!S.call) { return; }
    // The server ended the conference; a browser leg still up (a dropped event) is closed here.
    if (S.conn) { try { S.conn.disconnect(); } catch (e) { /* already gone */ } S.conn = null; }
    S.muted = false; S.held = false;
    renderControls();
    S.outcomeFor = S.call.id;
    $("outcomeCard").hidden = false;
    $("outAside").textContent = S.call.seconds ? "Talked " + talkText(S.call.seconds) : endLabel(S.call);
    markPicked(S.call.suggest || "", true);
    var answered = !!S.call.answered_at;
    Array.prototype.forEach.call(document.querySelectorAll("#outGroups .btn"), function (b) {
      var r = rowFor(b.dataset.key);
      b.disabled = !!(r && r.contact && !answered && S.call.direction === "outbound");
    });
    loadRecent();
    broadcast();
  }

  function markPicked(key, asSuggestion) {
    S.picked = asSuggestion ? "" : key;
    Array.prototype.forEach.call(document.querySelectorAll("#outGroups .btn"), function (b) {
      b.classList.toggle("suggested", asSuggestion && b.dataset.key === key);
      b.classList.toggle("picked", !asSuggestion && b.dataset.key === key);
    });
    var r = rowFor(key);
    $("callbackBox").hidden = !(r && r.task && !asSuggestion);
    if (r && r.task && !asSuggestion && !$("callbackAt").value) { $("callbackAt").value = tomorrowAt(10); }
    $("saveOutcome").disabled = !S.picked;
    $("outNote").placeholder = r && r.needs_note ? "Who answered: their name and how they are related" : "What they said, in their words";
  }

  function tomorrowAt(h) {
    var d = new Date(Date.now() + 86400000);
    d.setHours(h, 0, 0, 0);
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + "T" + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  // One click saves an outcome that needs nothing more; an outcome that needs a note or a time is picked
  // and waits for Save. Picked while the call is still up, the call is hung up first.
  function chooseOutcome(key, fromBar, note) {
    var r = rowFor(key);
    if (!r || !S.call) { return; }
    if (note) { $("outNote").value = note; }
    if (busy()) {
      hangup().then(function () { waitEnded(S.call.id, 10).then(function () { chooseOutcome(key, fromBar, note); }); });
      return;
    }
    markPicked(key, false);
    if ((r.needs_note && !$("outNote").value.trim()) || r.task) {
      $("outNote").focus();
      if (fromBar) { toast("Finish this outcome on the phone page: it needs " + (r.task ? "a call-back time." : "a note.")); }
      return;
    }
    saveOutcome();
  }

  function waitEnded(id, seconds) {
    return new Promise(function (resolve) {
      var left = seconds;
      (function poll() {
        api("GET", "/api/call/" + id).then(function (r) {
          if (r.ok && r.call) { onServerCall(r.call); }
          if ((r.call && (r.call.status === "ended" || r.call.status === "failed")) || --left <= 0) { resolve(); } else { setTimeout(poll, 1000); }
        });
      })();
    });
  }

  function saveOutcome() {
    if (!S.picked || !S.outcomeFor) { return; }
    var r = rowFor(S.picked);
    var body = { key: S.picked, note: $("outNote").value };
    if (r && r.task) {
      var v = $("callbackAt").value;
      if (!v) { $("outMsg").textContent = "Pick a call-back time."; return; }
      body.callback_at = new Date(v).toISOString();
    }
    $("saveOutcome").disabled = true;
    api("POST", "/api/call/" + S.outcomeFor + "/dispose", body).then(function (res) {
      if (!res.ok) { $("outMsg").textContent = res.error || "That outcome was not saved."; $("saveOutcome").disabled = false; return; }
      $("outMsg").textContent = "";
      S.outcomeFor = "";
      $("outNote").value = "";
      $("callbackAt").value = "";
      $("outcomeCard").hidden = true;
      if (res.call) { S.call = res.call; }
      toast("Saved: " + r.label + ".", 2200);
      loadRecent();
      if (S.record && S.record.uuid) { setTimeout(function () { refreshRecord(S.record.uuid); }, 2500); }
      broadcast();
      if (S.item && res.next && (res.next.record_done !== undefined)) { afterQueueCall(res.next); }
    });
  }

  // ------------------------------------------------------------------ the record and its texts
  function showRecord(rec, number) {
    S.record = rec;
    if (!rec || !rec.uuid) {
      $("recBody").hidden = true;
      $("recEmpty").hidden = false;
      $("recEmpty").textContent = number ? pretty(number) + " is not on a DataSift record." : "Call a number, or click one in DataSift, and the record shows here.";
      $("recAside").textContent = "";
      return;
    }
    $("recEmpty").hidden = true;
    $("recBody").hidden = false;
    $("recOwner").textContent = rec.owner + (rec.deceased ? " (marked deceased)" : "");
    $("recAddr").textContent = [rec.street, rec.city, rec.state].filter(Boolean).join(", ");
    $("recOpen").href = rec.link || "#";
    $("recStatus").textContent = rec.status ? rec.status.replace(/_/g, " ") : "No status";
    $("recAttempts").textContent = String(rec.attempts || 0);
    $("recValue").textContent = rec.value ? "$" + Math.round(rec.value).toLocaleString("en-US") : "Unknown";
    $("recEquity").textContent = (rec.equity !== null && rec.equity !== undefined && rec.equity !== "") ? Math.round(rec.equity) + "%" : "Unknown";
    var tags = $("recTags");
    clear(tags);
    (rec.lists || []).forEach(function (t) { tags.appendChild(el("span", "tag", t)); });
    (rec.tags || []).slice(0, 18).forEach(function (t) {
      var gold = /deceased|verify|litigator|do not|dnc|mail only|auction/i.test(t);
      tags.appendChild(el("span", "tag" + (gold ? " gold" : ""), t));
    });
    var list = $("recPhones");
    clear(list);
    (rec.phones || []).forEach(function (p) {
      var li = el("li", digits(p.number) === digits(number) ? "current" : "");
      li.appendChild(el("span", "pn", pretty(p.number)));
      var bits = [p.tier || "No tier", (p.type || "").toLowerCase() || "unknown line", statusWords(p.status)];
      if (p.registry) { bits.push("on the do-not-call registry"); }
      if (p.tags && p.tags.length) { bits.push(p.tags.join(", ")); }
      li.appendChild(el("span", "meta", bits.join(" / ")));
      var b = el("button", "btn btn-sm act", "Call");
      b.type = "button";
      b.addEventListener("click", function () { dial(p.number, rec.uuid, {}); });
      li.appendChild(b);
      list.appendChild(li);
    });
    var np = (rec.phones || []).length;
    $("recAside").textContent = np + (np === 1 ? " number" : " numbers");
    broadcast();
  }

  function statusWords(st) {
    var w = { CORRECT: "confirmed", UNKNOWN: "not confirmed", WRONG: "wrong number", DEAD: "dead", NO_ANSWER: "no answer",
              DNC: "do not call", CORRECT_DNC: "do not call", WRONG_DNC: "wrong, do not call" };
    return w[String(st || "UNKNOWN").toUpperCase()] || String(st || "").toLowerCase();
  }

  function refreshRecord(uuid) {
    api("GET", "/api/record/" + uuid).then(function (r) { if (r.ok && S.record && S.record.uuid === uuid) { showRecord(r.record, S.number); } });
  }

  function showIncomingRecord(c) {
    if (c.record && c.record.uuid) { $("inWho").textContent = c.record.owner + (c.record.street ? ", " + c.record.street : ""); }
  }

  function loadThread(number) {
    var box = $("thread");
    clear(box);
    if (!S.me.settings.texting) { box.appendChild(el("div", "empty", "The texting app is not connected to this dialer yet.")); $("txtAside").textContent = ""; return; }
    box.appendChild(el("div", "empty", "Reading the texts..."));
    api("GET", "/api/thread?number=" + encodeURIComponent(digits(number))).then(function (r) {
      clear(box);
      var t = (r && r.thread) || {};
      var msgs = t.messages || [];
      $("txtAside").textContent = t.conversation && t.conversation.state ? "Thread " + t.conversation.state.replace(/_/g, " ") : "";
      if (!msgs.length) { box.appendChild(el("div", "empty", "No texts with this number.")); return; }
      msgs.forEach(function (m) {
        var b = el("div", "msg " + (m.direction === "in" ? "in" : "out"), m.body);
        var when = m.created_at ? new Date(m.created_at).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
        b.appendChild(el("span", "when", (m.direction === "in" ? "They wrote" : (m.author === "ai" ? "Sent by the texting agent" : "Sent")) + (when ? ", " + when : "")));
        box.appendChild(b);
      });
      box.scrollTop = box.scrollHeight;
    });
  }

  // ------------------------------------------------------------------ the power dialer
  function loadPresets() {
    api("GET", "/api/presets").then(function (r) {
      var sel = $("presetPick");
      clear(sel);
      if (!r.ok) { sel.appendChild(el("option", "", "DataSift could not be read")); return; }
      var folder = "", group = null;
      r.presets.forEach(function (p) {
        if (p.folder !== folder) { folder = p.folder; group = document.createElement("optgroup"); group.label = folder || "Presets"; sel.appendChild(group); }
        var o = el("option", "", p.title);
        o.value = p.title;
        group.appendChild(o);
      });
      if (!r.presets.length) { sel.appendChild(el("option", "", "No call presets found")); }
    });
  }

  function loadSession() {
    api("GET", "/api/session/current").then(function (r) { if (r.ok && r.session && r.session.state !== "done") { S.session = r.session; showRun(); } });
  }

  function showRun() {
    $("qStart").hidden = !!S.session;
    $("qRun").hidden = !S.session;
    if (!S.session) { return; }
    $("qPreset").textContent = S.session.preset;
    var c = S.session.counts || {};
    $("qDone").textContent = String(c.done || 0);
    $("qSkipped").textContent = String(c.skipped || 0);
    $("qTotal").textContent = String(S.session.total || 0);
    var reasons = $("qReasons");
    clear(reasons);
    Object.keys(S.session.skipped_for || {}).forEach(function (k) { reasons.appendChild(el("li", "", S.session.skipped_for[k] + " " + k)); });
    $("qAside").textContent = S.session.state === "done" ? "Finished" : "";
    broadcast();
  }

  function startSession() {
    var preset = $("presetPick").value;
    if (!preset) { return; }
    $("qStartBtn").disabled = true;
    api("POST", "/api/session/start", { preset: preset }).then(function (r) {
      $("qStartBtn").disabled = false;
      if (!r.ok) { toast(r.error || "The list could not be started."); return; }
      S.session = r.session;
      if (r.note) { toast(r.note, 7000); }
      showRun();
      nextRecord();
    });
  }

  function nextRecord() {
    if (!S.session) { return; }
    cancelCountdown();
    api("POST", "/api/session/" + S.session.id + "/next", {}).then(function (r) {
      if (!r.ok) { toast(r.error || "The next record could not be read."); return; }
      if (r.session) { S.session = r.session; showRun(); }
      if (r.done) { S.item = null; qNow("The list is done: every record was called or skipped."); return; }
      if (r.more) {
        if (++S.nextLoops > 40) { S.nextLoops = 0; qNow("Many records in a row were held back. Press Dial next to keep looking."); return; }
        qNow("Passed over " + r.skipped + " records the rules hold back. Looking further...");
        nextRecord();
        return;
      }
      S.nextLoops = 0;
      S.item = r.item;
      showRecord(r.record, r.item.number);
      loadThread(r.item.number);
      openInSift(r.record);
      var n = r.item.numbers[r.item.next] || {};
      queueNow(r.record, n);
    });
  }

  function queueNow(rec, n) {
    var item = S.item;
    var which = "number " + (item.next + 1) + " of " + item.numbers.length + (n.tier ? " (" + n.tier + ")" : "");
    var text = "Next: " + rec.owner + ", " + pretty(n.number) + ", " + which + ".";
    if (n.manual_only) {
      qNow(text + " This state's law needs you to press Dial next yourself.");
      return;
    }
    if ($("qAuto").checked && !$("qPause").checked) { countdown(text); } else { qNow(text + " Press Dial next."); }
  }

  function countdown(text) {
    cancelCountdown();
    var left = Math.max(1, (S.me.settings.advance_seconds || 3));
    var tick = function () {
      qNow(text + " Dialing in " + left + "...");
      if (left-- <= 0) { cancelCountdown(); queueDial(); }
    };
    tick();
    S.countdownId = setInterval(tick, 1000);
  }

  function cancelCountdown() { if (S.countdownId) { clearInterval(S.countdownId); S.countdownId = 0; } }

  function qNow(text) { var n = $("qNow"); n.hidden = !text; n.textContent = text; }

  function queueDial() {
    cancelCountdown();
    if (!S.session) { return; }
    if (!S.item) { nextRecord(); return; }
    var n = S.item.numbers[S.item.next];
    if (!n) { nextRecord(); return; }
    dial(n.number, S.item.record_uuid, { item: S.item });
  }

  function afterQueueCall(next) {
    if (!S.item) { return; }
    if ($("qPause").checked) { $("qPause").checked = false; qNow("Paused. Press Dial next when you are ready."); if (!next.record_done) { S.item.next += 1; } else { S.item = null; } return; }
    if (next.record_done) { S.item = null; nextRecord(); return; }
    S.item.next += 1;
    var n = S.item.numbers[S.item.next] || {};
    queueNow(S.record || { owner: "" }, n);
  }

  function skipRecord() {
    if (!S.session || !S.item) { nextRecord(); return; }
    api("POST", "/api/session/" + S.session.id + "/skip", { item_id: S.item.id }).then(function () { S.item = null; nextRecord(); });
  }

  function stopSession() {
    if (!S.session) { return; }
    cancelCountdown();
    api("POST", "/api/session/" + S.session.id + "/stop", {}).then(function () { S.session = null; S.item = null; showRun(); qNow(""); });
  }

  // ------------------------------------------------------------------ waiting on you, today, live calls
  function loadHot() {
    api("GET", "/api/handraises").then(function (r) {
      var list = $("hotList");
      clear(list);
      var rows = (r && r.handraises) || [];
      $("inboxAside").textContent = rows.length ? rows.length + " texted back" : "";
      if (!rows.length) { list.appendChild(el("li", "empty", "Nobody is waiting on a call back.")); return; }
      rows.forEach(function (h) {
        var li = el("li", "hot");
        var g = el("div", "grow");
        g.appendChild(el("div", "pn tnum", h.pretty));
        g.appendChild(el("div", "small", "Texted: " + (h.body || "")));
        li.appendChild(g);
        var b = el("button", "btn btn-sm btn-primary", "Call");
        b.type = "button";
        b.addEventListener("click", function () { dial(h.number, h.record_uuid || "", {}); });
        li.appendChild(b);
        list.appendChild(li);
      });
    });
  }

  function loadRecent() {
    api("GET", "/api/recent").then(function (r) {
      var list = $("recentList");
      clear(list);
      var rows = (r && r.calls) || [];
      if (!rows.length) { list.appendChild(el("li", "empty", "No calls yet today.")); return; }
      rows.slice(0, 25).forEach(function (c) {
        var li = el("li", c.voicemail ? "hot" : "");
        var g = el("div", "grow");
        g.appendChild(el("div", "", ((c.record && c.record.owner) || c.pretty) + (c.direction === "inbound" ? " (called in)" : "")));
        var label = c.disposition ? outcomeLabel(c.disposition) + (c.seconds ? ", " + talkText(c.seconds) : "")
                                  : (c.status === "blocked" ? "Not dialed" : endLabel(c));
        g.appendChild(el("div", "small tnum", c.pretty + ", " + label));
        li.appendChild(g);
        if (c.direction === "inbound" || c.voicemail) {
          var b = el("button", "btn btn-sm", "Call back");
          b.type = "button";
          b.addEventListener("click", function () { dial(c.number, c.record_uuid || "", {}); });
          li.appendChild(b);
        }
        list.appendChild(li);
      });
    });
  }

  function outcomeLabel(key) { var r = rowFor(key); return r ? r.label : key; }

  function loadActive() {
    api("GET", "/api/active").then(function (r) {
      var list = $("activeList");
      clear(list);
      var rows = (r && r.calls) || [];
      if (!rows.length) { list.appendChild(el("li", "empty", "Nobody is on a call.")); return; }
      rows.forEach(function (c) {
        var li = el("li");
        li.appendChild(el("div", "grow", c.caller + " with " + ((c.record && c.record.owner) || c.pretty) + ", " + c.status));
        ["listen", "whisper", "barge"].forEach(function (mode) {
          var b = el("button", "btn btn-sm", mode.charAt(0).toUpperCase() + mode.slice(1));
          b.type = "button";
          b.addEventListener("click", function () { monitor(c.id, mode); });
          li.appendChild(b);
        });
        list.appendChild(li);
      });
    });
  }

  function monitor(callId, mode) {
    if (S.side || busy()) { toast("Hang up your own call first."); return; }
    api("POST", "/api/call/" + callId + "/monitor", {}).then(function (r) {
      if (!r.ok) { toast(r.error || "You cannot join that call."); return; }
      S.device.connect({ params: { monitor: callId, mode: mode } }).then(function (conn) {
        S.side = conn;
        toast("Joined to " + mode + ". Hang up to leave.");
        conn.on("disconnect", function () { S.side = null; });
      });
    });
  }

  // ------------------------------------------------------------------ incoming calls
  function onIncoming(call) {
    S.incoming = call;
    var p = call.customParameters;
    var callId = (p && p.get && p.get("call_id")) || S.incomingCallId || "";
    S.incomingCallId = callId;
    var from = (call.parameters && call.parameters.From) || "";
    $("incomingBox").hidden = false;
    $("inNum").textContent = pretty(from);
    $("inWho").textContent = "A call back";
    if (callId) {
      api("GET", "/api/call/" + callId).then(function (r) { if (r.ok && r.call) { showIncomingRecord(r.call); } });
    }
    notify("Incoming call", pretty(from));
    var gone = function () { $("incomingBox").hidden = true; S.incoming = null; };
    call.on("cancel", gone);
    call.on("disconnect", function () { gone(); S.conn = null; checkEnded(); });
    call.on("reject", gone);
  }

  function acceptIncoming() {
    var call = S.incoming;
    if (!call) { return; }
    if (busy()) { toast("Hang up your call first, or let this one go to voicemail."); return; }
    call.accept();
    S.conn = call;
    $("incomingBox").hidden = true;
    var id = S.incomingCallId;
    if (id) {
      api("POST", "/api/call/" + id + "/accepted", {}).then(function () {
        return api("GET", "/api/call/" + id);
      }).then(function (r) {
        if (r && r.ok && r.call) {
          S.call = r.call;
          S.number = r.call.number;
          renderCall();
          if (r.call.record && r.call.record.uuid) { showRecord(r.call.record, r.call.number); openInSift(r.call.record); }
          loadThread(r.call.number);
        }
      });
    }
  }

  function notify(title, body) {
    try {
      if (document.hidden && window.Notification && Notification.permission === "granted") { new Notification(title, { body: body }); }
    } catch (e) { /* notifications are a nicety */ }
  }

  function askNotifications() {
    try { if (window.Notification && Notification.permission === "default") { Notification.requestPermission(); } } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------------ sheets
  function confirmSheet(title, lines) {
    return new Promise(function (resolve) {
      $("sheetTitle").textContent = title;
      var body = $("sheetBody");
      clear(body);
      var ul = el("ul", "warnlist");
      lines.forEach(function (l) { ul.appendChild(el("li", "", l)); });
      body.appendChild(ul);
      $("sheetYes").hidden = false;
      $("sheetYes").textContent = "Call anyway";
      $("sheetWrap").hidden = false;
      $("sheetYes").focus();
      var done = function (v) { $("sheetWrap").hidden = true; $("sheetYes").onclick = null; $("sheetNo").onclick = null; resolve(v); };
      $("sheetYes").onclick = function () { done(true); };
      $("sheetNo").onclick = function () { done(false); };
    });
  }

  function stopSheet(lines) {
    $("sheetTitle").textContent = "Not dialed";
    var body = $("sheetBody");
    clear(body);
    var ul = el("ul", "warnlist");
    lines.forEach(function (l) { ul.appendChild(el("li", "", l)); });
    body.appendChild(ul);
    $("sheetYes").hidden = true;
    $("sheetNo").textContent = "Close";
    $("sheetWrap").hidden = false;
    $("sheetNo").onclick = function () { $("sheetWrap").hidden = true; $("sheetNo").textContent = "Cancel"; $("sheetYes").hidden = false; };
  }

  function openSettings() {
    var me = S.me;
    $("vmState").textContent = me.vm_ready ? "Your voicemail message is saved." : "You have not recorded a voicemail message yet.";
    $("vmPreview").hidden = !me.vm_ready;
    if (me.vm_ready) { $("vmPreview").src = "/api/vm/preview?t=" + Date.now(); }
    $("settingsWrap").hidden = false;
  }

  function recordVm() {
    if (!S.device || busy()) { toast("The phone has to be free to record."); return; }
    S.device.connect({ params: { action: "record_vm" } }).then(function (conn) {
      S.side = conn;
      toast("Speak your message after the tone, then press the pound key or hang up.", 8000);
      conn.on("disconnect", function () { S.side = null; });
    });
  }

  function uploadVm(file) {
    if (!file) { return; }
    fetch("/api/vm/upload", { method: "POST", headers: { "Content-Type": "audio/mpeg", "X-Dialer": "1" }, body: file, credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (r) { if (r.ok) { toast("Voicemail message saved."); loadMe(); setTimeout(openSettings, 400); } else { toast(r.error || "That file was not saved."); } });
  }

  // ------------------------------------------------------------------ wiring
  function wire() {
    $("dialForm").addEventListener("submit", function (e) {
      e.preventDefault();
      var n = digits($("dialNumber").value);
      if (!n) { toast("Type a ten-digit number."); return; }
      dial(n, "", {});
    });
    $("hangBtn").addEventListener("click", hangup);
    $("muteBtn").addEventListener("click", toggleMute);
    $("holdBtn").addEventListener("click", toggleHold);
    $("vmBtn").addEventListener("click", vmDrop);
    $("xferBtn").addEventListener("click", function () {
      var box = $("xferBox");
      box.hidden = !box.hidden;
      if (!box.hidden) {
        api("GET", "/api/team").then(function (r) {
          var sel = $("xferTeam");
          clear(sel);
          var none = el("option", "", "A teammate");
          none.value = "";
          sel.appendChild(none);
          ((r && r.team) || []).forEach(function (t) { var o = el("option", "", t.name); o.value = t.identity; sel.appendChild(o); });
        });
      }
    });
    $("xferGo").addEventListener("click", function () {
      if (!S.call) { return; }
      var who = $("xferTeam").value, num = digits($("xferNumber").value);
      if (!who && !num) { toast("Pick a teammate or type a number."); return; }
      api("POST", "/api/call/" + S.call.id + "/transfer", who ? { identity: who } : { number: num }).then(function (r) {
        toast(r.ok ? "Adding them to the call. Introduce them, then hang up to leave." : (r.error || "The transfer did not work."), 6000);
      });
    });
    $("saveOutcome").addEventListener("click", saveOutcome);
    $("qStartBtn").addEventListener("click", startSession);
    $("qDialBtn").addEventListener("click", queueDial);
    $("qSkipBtn").addEventListener("click", skipRecord);
    $("qStopBtn").addEventListener("click", stopSession);
    $("qAuto").addEventListener("change", function () { if (!$("qAuto").checked && S.countdownId) { cancelCountdown(); qNow("Auto dial is off. Press Dial next."); } });
    $("qPause").addEventListener("change", function () { if ($("qPause").checked) { cancelCountdown(); qNow("Paused. Press Dial next when you are ready."); } });
    $("acceptBtn").addEventListener("click", acceptIncoming);
    $("declineBtn").addEventListener("click", function () { if (S.incoming) { S.incoming.reject(); } });
    $("signOutBtn").addEventListener("click", function () { api("POST", "/api/signout", {}).then(function () { location.href = "/signin"; }); });
    $("settingsBtn").addEventListener("click", openSettings);
    $("setClose").addEventListener("click", function () { $("settingsWrap").hidden = true; });
    $("vmRecordBtn").addEventListener("click", recordVm);
    $("vmFile").addEventListener("change", function (e) { uploadVm(e.target.files && e.target.files[0]); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !$("sheetWrap").hidden) { $("sheetNo").click(); }
    });
  }

  wire();
  boot();
})();
