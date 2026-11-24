/* The one-time code rides in the link's fragment (after #), which a browser never sends to a server, so it
 * cannot land in an access log. It is posted once and wiped from the address bar. */
"use strict";
(function () {
  var msg = document.getElementById("signinMsg");
  var code = (location.hash || "").replace(/^#/, "");
  if (!code) { return; }
  history.replaceState(null, "", location.pathname);
  msg.textContent = "Signing you in...";
  fetch("/api/signin", { method: "POST", credentials: "same-origin",
                         headers: { "Content-Type": "application/json", "X-Dialer": "1" },
                         body: JSON.stringify({ code: code }) })
    .then(function (r) { return r.json(); })
    .then(function (r) {
      if (r.ok) {
        msg.textContent = "Signed in as " + r.name + ".";
        location.replace("/phone");
      } else {
        msg.textContent = r.error || "That link did not work. Ask for a new one.";
      }
    })
    .catch(function () { msg.textContent = "The dialer could not be reached. Try the link again in a minute."; });
})();
