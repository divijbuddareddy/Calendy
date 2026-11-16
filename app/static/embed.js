/*! AAL Scheduler embed v1 - inline widget, popup widget, popup text.
 * Load asynchronously:  <script src="https://YOUR-HOST/embed.js" async></script>
 *
 *   AAL.initInlineWidget({url, parentElement, prefill:{name,email}, utm:{utm_source,...},
 *                         theme:{primary_color, background_color, text_color, border_radius, hide_details}})
 *   AAL.initPopupWidget({url, text, color, textColor, prefill, utm, theme})   // floating button
 *   AAL.initPopupText({url, prefill, utm, theme})                             // open popup now (bind to a link)
 *   AAL.closePopup()
 *
 * Markup auto-init:  <div class="aal-inline-widget" data-url="..." data-prefill-name="..." data-primary-color="#..."></div>
 *                    <a href="..." data-aal-popup>Book a call</a>
 * Events: window/document CustomEvent "aal:event_scheduled" (detail = booking info), "aal:popup_closed",
 *         "aal:page_height"; the same payloads are re-posted as window messages {type:"aal.event_scheduled"}.
 */
(function (w, d) {
  "use strict";
  if (w.AAL && w.AAL.__loaded) return;
  var frames = []; // {iframe, origin, inline}
  var popup = null;
  var STYLE_ID = "aal-embed-style";

  function injectStyles() {
    if (d.getElementById(STYLE_ID)) return;
    var s = d.createElement("style");
    s.id = STYLE_ID;
    s.textContent =
      ".aal-inline-frame{width:100%;min-width:280px;border:0;display:block;transition:height .15s ease;}" +
      ".aal-overlay{position:fixed;inset:0;z-index:2147483000;background:rgba(15,18,32,.62);display:flex;align-items:center;justify-content:center;padding:24px;opacity:0;transition:opacity .15s;}" +
      ".aal-overlay.aal-open{opacity:1}" +
      ".aal-popup{position:relative;width:100%;max-width:1060px;height:min(720px,100%);background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.35)}" +
      ".aal-popup iframe{width:100%;height:100%;border:0;display:block}" +
      ".aal-close{position:absolute;top:10px;right:12px;width:36px;height:36px;border-radius:50%;border:0;background:rgba(26,31,54,.85);color:#fff;font:600 20px/36px system-ui,sans-serif;cursor:pointer;z-index:2}" +
      ".aal-close:focus-visible{outline:3px solid #8b93ff;outline-offset:2px}" +
      ".aal-badge{position:fixed;right:20px;bottom:20px;z-index:2147482999;border:0;border-radius:999px;padding:14px 22px;font:600 15px/1 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;box-shadow:0 8px 24px rgba(16,24,40,.25);cursor:pointer}" +
      ".aal-badge:focus-visible{outline:3px solid #8b93ff;outline-offset:3px}" +
      ".aal-loading{display:flex;align-items:center;justify-content:center;height:100%;font:15px system-ui,sans-serif;color:#5b6380}" +
      "@media (max-width:640px){.aal-overlay{padding:0}.aal-popup{height:100%;border-radius:0}}";
    (d.head || d.documentElement).appendChild(s);
  }

  function hex(v) { return v ? String(v).replace(/^#/, "") : ""; }

  function buildUrl(opts) {
    var url;
    try { url = new URL(opts.url, w.location.href); } catch (e) { throw new Error("AAL: invalid url " + opts.url); }
    var p = url.searchParams;
    p.set("embed", "1");
    p.set("embed_domain", w.location.hostname || "");
    p.set("embed_type", opts._type || "inline");
    var pre = opts.prefill || {};
    if (pre.name) p.set("name", pre.name);
    if (pre.email) p.set("email", pre.email);
    if (pre.customAnswers) for (var k in pre.customAnswers) p.set(k, pre.customAnswers[k]);
    var utm = opts.utm || {};
    ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"].forEach(function (k) {
      var camel = k.replace(/_([a-z])/g, function (_, c) { return c.toUpperCase(); });
      var v = utm[k] || utm[camel];
      if (!v) { try { v = new URL(w.location.href).searchParams.get(k); } catch (e) {} } // pass-through from host page
      if (v) p.set(k, v);
    });
    var t = opts.theme || {};
    if (t.primary_color || t.primaryColor) p.set("primary_color", hex(t.primary_color || t.primaryColor));
    // long names + the short aliases understood by the booking pages (bg / text / radius)
    if (t.background_color || t.backgroundColor) { p.set("background_color", hex(t.background_color || t.backgroundColor)); p.set("bg", hex(t.background_color || t.backgroundColor)); }
    if (t.text_color || t.textColor) { p.set("text_color", hex(t.text_color || t.textColor)); p.set("text", hex(t.text_color || t.textColor)); }
    if (t.border_radius != null || t.borderRadius != null) { var rad = String(t.border_radius != null ? t.border_radius : t.borderRadius); p.set("border_radius", rad); p.set("radius", rad); }
    if (t.hide_details || t.hideDetails) p.set("hide_details", "1");
    if (t.hide_branding || t.hideBranding) p.set("hide_branding", "1");
    return url;
  }

  function makeFrame(opts, type) {
    opts._type = type;
    var url = buildUrl(opts);
    var f = d.createElement("iframe");
    f.src = url.toString();
    f.title = opts.title || "Schedule a meeting";
    f.setAttribute("loading", type === "inline" ? "lazy" : "eager");
    f.setAttribute("allow", "payment");
    f.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");
    frames.push({ iframe: f, origin: url.origin, inline: type === "inline", parent: null });
    return f;
  }

  function emit(name, detail, target) {
    var ev;
    try { ev = new CustomEvent(name, { detail: detail, bubbles: true }); }
    catch (e) { ev = d.createEvent("CustomEvent"); ev.initCustomEvent(name, true, false, detail); }
    (target || d).dispatchEvent(ev);
    if (target && target !== d) {
      try { d.dispatchEvent(new CustomEvent(name, { detail: detail })); } catch (e) {}
    }
    try { w.dispatchEvent(new CustomEvent(name, { detail: detail })); } catch (e) {}
  }

  function relay(type, detail) {
    try { w.postMessage({ type: type, payload: detail, __aalRelay: true }, "*"); } catch (e) {}
  }

  w.addEventListener("message", function (e) {
    var data = e.data;
    if (!data || typeof data !== "object" || data.__aalRelay) return;
    var entry = null;
    for (var i = 0; i < frames.length; i++) {
      if (frames[i].iframe.contentWindow === e.source) { entry = frames[i]; break; }
    }
    if (!entry || e.origin !== entry.origin) return; // only trust our own iframes
    var type = data.type || data.event;
    var payload = data.payload || data.detail || data;
    if (type === "aal.page_height") {
      var h = parseInt(data.height || (payload && payload.height), 10);
      if (entry.inline && h > 0) entry.iframe.style.height = Math.min(h + 2, 4000) + "px";
      emit("aal:page_height", { height: h }, entry.parent);
    } else if (type === "aal.event_scheduled") {
      emit("aal:event_scheduled", payload, entry.parent);
      relay("aal.event_scheduled", payload);
    } else if (type === "aal.date_and_time_selected" || type === "aal.event_type_viewed") {
      emit(type.replace("aal.", "aal:"), payload, entry.parent);
    }
  });

  function resolve(el) {
    if (!el) return null;
    if (typeof el === "string") return d.querySelector(el);
    return el;
  }

  function initInlineWidget(opts) {
    opts = opts || {};
    if (!opts.url) throw new Error("AAL.initInlineWidget: url is required");
    injectStyles();
    var parent = resolve(opts.parentElement);
    if (!parent) throw new Error("AAL.initInlineWidget: parentElement not found");
    if (parent.__aalInit) return parent.__aalInit;
    var f = makeFrame(opts, "inline");
    frames[frames.length - 1].parent = parent;
    f.className = "aal-inline-frame";
    var minH = parseInt(opts.minHeight || parent.getAttribute("data-min-height") || "700", 10);
    f.style.height = minH + "px";
    var r = (opts.theme || {}).border_radius;
    if (r != null) { f.style.borderRadius = r + "px"; }
    parent.innerHTML = "";
    parent.appendChild(f);
    parent.__aalInit = f;
    return f;
  }

  function closePopup() {
    if (!popup) return;
    var p = popup;
    popup = null;
    p.overlay.classList.remove("aal-open");
    d.removeEventListener("keydown", p.onKey, true);
    d.documentElement.style.overflow = p.prevOverflow;
    setTimeout(function () { if (p.overlay.parentNode) p.overlay.parentNode.removeChild(p.overlay); }, 160);
    frames = frames.filter(function (x) { return x.iframe !== p.iframe; });
    if (p.returnFocus && p.returnFocus.focus) { try { p.returnFocus.focus(); } catch (e) {} }
    emit("aal:popup_closed", { url: p.url });
    relay("aal.popup_closed", { url: p.url });
  }

  function openPopup(opts) {
    opts = opts || {};
    if (!opts.url) throw new Error("AAL: url is required");
    injectStyles();
    if (popup) closePopup();
    var overlay = d.createElement("div");
    overlay.className = "aal-overlay";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", opts.title || "Schedule a meeting");
    var box = d.createElement("div");
    box.className = "aal-popup";
    var close = d.createElement("button");
    close.className = "aal-close";
    close.type = "button";
    close.setAttribute("aria-label", "Close");
    close.innerHTML = "&times;";
    close.addEventListener("click", closePopup);
    var f = makeFrame(opts, "popup");
    box.appendChild(close);
    box.appendChild(f);
    overlay.appendChild(box);
    overlay.addEventListener("click", function (e) { if (e.target === overlay) closePopup(); });
    var onKey = function (e) { if (e.key === "Escape") { e.preventDefault(); closePopup(); } };
    d.addEventListener("keydown", onKey, true);
    popup = { overlay: overlay, iframe: f, onKey: onKey, url: opts.url, returnFocus: d.activeElement,
              prevOverflow: d.documentElement.style.overflow };
    d.documentElement.style.overflow = "hidden";
    d.body.appendChild(overlay);
    requestAnimationFrame(function () { overlay.classList.add("aal-open"); close.focus(); });
    return f;
  }

  function initPopupText(opts) { return openPopup(opts); }

  function initPopupWidget(opts) {
    opts = opts || {};
    if (!opts.url) throw new Error("AAL.initPopupWidget: url is required");
    injectStyles();
    var existing = d.querySelector(".aal-badge");
    if (existing) existing.parentNode.removeChild(existing);
    var b = d.createElement("button");
    b.type = "button";
    b.className = "aal-badge";
    b.textContent = opts.text || "Schedule time with me";
    b.style.background = opts.color || "#3b47d6";
    b.style.color = opts.textColor || "#ffffff";
    if (opts.branding === false) b.setAttribute("data-branding", "off");
    b.addEventListener("click", function () { openPopup(opts); });
    var mount = function () { d.body.appendChild(b); };
    if (d.body) mount(); else d.addEventListener("DOMContentLoaded", mount);
    return b;
  }

  function attr(el, name) { return el.getAttribute("data-" + name) || undefined; }
  function optsFrom(el) {
    return {
      url: attr(el, "url") || el.getAttribute("href"),
      prefill: { name: attr(el, "prefill-name"), email: attr(el, "prefill-email") },
      utm: { utm_source: attr(el, "utm-source"), utm_medium: attr(el, "utm-medium"), utm_campaign: attr(el, "utm-campaign"),
             utm_term: attr(el, "utm-term"), utm_content: attr(el, "utm-content") },
      theme: { primary_color: attr(el, "primary-color"), background_color: attr(el, "background-color"),
               text_color: attr(el, "text-color"), border_radius: attr(el, "border-radius"),
               hide_details: attr(el, "hide-details") === "1" || attr(el, "hide-details") === "true" }
    };
  }

  function autoInit() {
    var els = d.querySelectorAll(".aal-inline-widget[data-url]");
    for (var i = 0; i < els.length; i++) {
      if (els[i].__aalInit) continue;
      var o = optsFrom(els[i]);
      o.parentElement = els[i];
      try { initInlineWidget(o); } catch (e) { if (w.console) console.warn(e); }
    }
  }

  d.addEventListener("click", function (e) {
    var a = e.target && e.target.closest ? e.target.closest("[data-aal-popup]") : null;
    if (!a) return;
    e.preventDefault();
    openPopup(optsFrom(a));
  });

  w.AAL = {
    __loaded: true,
    version: "1.0.0",
    initInlineWidget: initInlineWidget,
    initPopupWidget: initPopupWidget,
    initPopupText: initPopupText,
    closePopup: closePopup,
    init: autoInit
  };

  if (d.readyState === "loading") d.addEventListener("DOMContentLoaded", autoInit);
  else autoInit();

  // Run calls queued before the script loaded: window.AALQueue = [fn, ...]
  var q = w.AALQueue;
  if (q && q.length) { for (var j = 0; j < q.length; j++) { try { q[j](w.AAL); } catch (e) {} } }
  w.AALQueue = { push: function (fn) { try { fn(w.AAL); } catch (e) {} } };
})(window, document);
