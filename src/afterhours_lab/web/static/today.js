(function () {
  "use strict";
  var script = document.currentScript;
  var marketDate = script && script.dataset.marketDate;
  var target = document.getElementById("today-snapshot");
  var state = document.getElementById("stream-state");
  if (!marketDate || !target || !state || !("EventSource" in window)) {
    if (state) state.textContent = "Live updates unavailable — showing page snapshot";
    return;
  }
  var stream = new EventSource("/today/stream?date=" + encodeURIComponent(marketDate));
  stream.addEventListener("open", function () {
    state.textContent = "Connected — live database updates enabled";
    state.className = "tag final";
  });
  stream.addEventListener("snapshot", function (event) {
    var payload = JSON.parse(event.data);
    target.innerHTML = payload.html;
    state.textContent = "Connected — updated " + new Date(payload.generated_at).toLocaleTimeString();
    state.className = "tag final";
  });
  stream.addEventListener("degraded", function (event) {
    var payload = JSON.parse(event.data);
    state.textContent = payload.message;
    state.className = "tag bad";
  });
  stream.onerror = function () {
    state.textContent = "Reconnecting — the displayed snapshot may be stale";
    state.className = "tag provisional";
  };
  window.addEventListener("pagehide", function () { stream.close(); }, {once: true});
}());
