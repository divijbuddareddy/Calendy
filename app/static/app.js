// Shared front-end helpers (no build step).
(function () {
  const csrf = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || "";

  // fetch wrapper that sends CSRF header + JSON
  window.api = async function (url, opts = {}) {
    const o = Object.assign({ method: "GET", headers: {} }, opts);
    o.headers = Object.assign({ "X-CSRF-Token": csrf(), Accept: "application/json" }, o.headers);
    if (o.body && typeof o.body !== "string" && !(o.body instanceof FormData)) {
      o.headers["Content-Type"] = "application/json";
      o.body = JSON.stringify(o.body);
    }
    const res = await fetch(url, o);
    let data = null;
    try { data = await res.json(); } catch (e) {}
    if (!res.ok) {
      const msg = (data && data.error && data.error.message) || res.statusText;
      const err = new Error(msg); err.status = res.status; err.data = data; throw err;
    }
    return data;
  };

  window.toast = function (msg) {
    let t = document.getElementById("toast");
    if (!t) { t = document.createElement("div"); t.id = "toast"; t.className = "toast"; t.setAttribute("role", "status"); document.body.appendChild(t); }
    t.textContent = msg; t.classList.add("show");
    clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2200);
  };

  // <button data-copy="text">  -> copies to clipboard
  document.addEventListener("click", function (e) {
    const b = e.target.closest("[data-copy]");
    if (b) {
      e.preventDefault();
      navigator.clipboard.writeText(b.getAttribute("data-copy")).then(() => toast("Copied to clipboard"));
    }
    const m = e.target.closest("[data-menu-toggle]");
    if (m) document.querySelector(".sidebar").classList.toggle("open");
    const c = e.target.closest("[data-confirm]");
    if (c && !confirm(c.getAttribute("data-confirm"))) { e.preventDefault(); e.stopPropagation(); }
  });

  // Auto-inject CSRF into every POST form lacking it
  document.addEventListener("submit", function (e) {
    const f = e.target;
    if (f.method && f.method.toLowerCase() === "post" && !f.querySelector('input[name="csrf_token"]')) {
      const i = document.createElement("input"); i.type = "hidden"; i.name = "csrf_token"; i.value = csrf(); f.appendChild(i);
    }
  }, true);
})();

// PWA: register the service worker (offline shell) on app pages only, never inside embeds.
if ("serviceWorker" in navigator && document.querySelector('link[rel="manifest"]') && window.top === window.self) {
  window.addEventListener("load", function () { navigator.serviceWorker.register("/sw.js").catch(function () {}); });
}

