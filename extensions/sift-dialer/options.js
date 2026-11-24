"use strict";
(function () {
  var DEFAULT_URL = "https://siftstack-dialer.fly.dev";
  var $ = function (id) { return document.getElementById(id); };
  chrome.storage.sync.get({ dialerUrl: DEFAULT_URL, bar: true, follow: true }, function (s) {
    $("dialerUrl").value = s.dialerUrl;
    $("bar").checked = s.bar !== false;
    $("follow").checked = s.follow !== false;
  });
  $("save").addEventListener("click", function () {
    var raw = ($("dialerUrl").value || DEFAULT_URL).trim().replace(/\/+$/, "");
    var url;
    try { url = new URL(raw); } catch (e) { $("msg").textContent = "That is not a web address."; return; }
    if (url.protocol !== "https:" && url.hostname !== "127.0.0.1" && url.hostname !== "localhost") {
      $("msg").textContent = "The dialer's address must start with https://";
      return;
    }
    var save = function () {
      chrome.storage.sync.set({ dialerUrl: url.origin, bar: $("bar").checked, follow: $("follow").checked }, function () {
        $("msg").textContent = "Saved.";
      });
    };
    chrome.permissions.contains({ origins: [url.origin + "/*"] }, function (has) {
      if (has) { save(); return; }
      chrome.permissions.request({ origins: [url.origin + "/*"] }, function (granted) {
        if (granted) { save(); } else { $("msg").textContent = "Chrome was not allowed to reach that address, so nothing changed."; }
      });
    });
  });
})();
