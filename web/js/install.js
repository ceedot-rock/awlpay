/* Android Chrome install prompt + iPhone Safari Add-to-Home-Screen hint. */
(function () {
  var btn = document.getElementById("install-app");
  var note = document.getElementById("install-note");
  var deferred = null;
  var ua = navigator.userAgent || "";
  var isIOS = /iPad|iPhone|iPod/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var isStandalone = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;

  function setNote(text) {
    if (!note) return;
    note.textContent = text;
    note.hidden = !text;
  }

  if (isStandalone) {
    document.documentElement.classList.add("is-standalone");
    if (btn) btn.hidden = true;
    return;
  }

  if (btn) {
    btn.hidden = false;
    btn.textContent = isIOS ? "Add to iPhone" : "Install on Android";
    btn.addEventListener("click", function () {
      if (deferred) {
        deferred.prompt();
        deferred.userChoice.then(function (choice) {
          deferred = null;
          if (choice.outcome === "accepted" && btn) btn.hidden = true;
        });
        return;
      }
      if (isIOS) {
        setNote("On iPhone: tap Share, then Add to Home Screen. Use Safari, not Chrome.");
      } else {
        setNote("On Android Chrome: menu → Install app / Add to Home screen.");
      }
    });
  }

  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    deferred = e;
    if (btn) {
      btn.hidden = false;
      btn.textContent = "Install on Android";
    }
  });

  window.addEventListener("appinstalled", function () {
    if (btn) btn.hidden = true;
    setNote("AwLPay is on this phone. Demo / stubs — not live money.");
  });
})();
