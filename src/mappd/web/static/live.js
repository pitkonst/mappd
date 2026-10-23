/* Live refresh for MAPP-D pages; MIT, part of mappd.
 *
 * An element with data-live="<events url>" holds one EventSource while its tab is visible;
 * every event fires `live` on the elements inside it that refetch (hx-trigger="live").
 * A hidden tab closes its stream; when it comes back the stream reopens and the page
 * refreshes once, as it does after any reconnect (the streams do not replay).
 *
 * Why not keep streams open: over HTTP/1.1 a browser allows six connections per host,
 * shared by all its tabs. Six open queue pages would hold them all and every other
 * request to the app would hang. Behind an HTTP/2 proxy the limit does not apply.
 */
(function () {
  "use strict";
  var streams = new Map();

  function refresh(root) {
    root.querySelectorAll('[hx-trigger~="live"]').forEach(function (el) {
      htmx.trigger(el, "live");
    });
  }

  function open(root, refreshOnOpen) {
    if (streams.has(root)) return;
    var source = new EventSource(root.dataset.live);
    var again = refreshOnOpen;
    source.onopen = function () {
      if (again) refresh(root); // events may have been missed while closed
      again = true;
    };
    source.onmessage = function () {
      refresh(root);
    };
    streams.set(root, source);
  }

  function close(root) {
    var source = streams.get(root);
    if (source) {
      source.close();
      streams.delete(root);
    }
  }

  function sync(refreshOnOpen) {
    streams.forEach(function (_source, root) {
      if (document.hidden || !root.isConnected) close(root);
    });
    if (document.hidden) return;
    document.querySelectorAll("[data-live]").forEach(function (root) {
      open(root, refreshOnOpen);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    sync(false);
  });
  document.addEventListener("visibilitychange", function () {
    sync(true);
  });
  window.addEventListener("pagehide", function () {
    streams.forEach(function (_source, root) {
      close(root);
    });
  });
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) sync(true); // back from the back-forward cache
  });
})();
