/* The relay inside the dialer tab. It passes the extension's messages to the page and the page's back.
 * It accepts only messages tagged by the page itself, from this window and this origin. */
"use strict";
(function () {
  if (window.__siftDialerBridge) { return; }
  window.__siftDialerBridge = true;

  window.addEventListener("message", function (ev) {
    if (ev.source !== window || ev.origin !== location.origin) { return; }
    var m = ev.data;
    if (!m || m.source !== "sift-dialer-page") { return; }
    if (m.type === "state" || m.type === "open_record") {
      try { chrome.runtime.sendMessage({ from: "page", type: m.type, state: m.state, uuid: m.uuid, link: m.link }); } catch (e) { /* extension reloaded */ }
    }
  });

  chrome.runtime.onMessage.addListener(function (msg) {
    if (!msg || msg.to !== "page") { return; }
    var out = { source: "sift-dialer-ext" };
    for (var k in msg) { if (k !== "to" && k !== "from") { out[k] = msg[k]; } }
    window.postMessage(out, location.origin);
  });

  function hello() {
    try { chrome.runtime.sendMessage({ from: "page", type: "bridge_ready" }); } catch (e) { /* ignore */ }
    window.postMessage({ source: "sift-dialer-ext", type: "hello" }, location.origin);
  }
  if (document.readyState === "complete") { hello(); } else { window.addEventListener("load", hello); }
})();
