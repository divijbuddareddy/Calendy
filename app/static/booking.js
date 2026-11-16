// Public booking page: calendar grid, slot list, invitee form, reschedule, waitlist, embed messaging.
// Uses the public JSON API (/api/v1/public/...). Never shows a booking as confirmed until the backend says so.
// Progressive enhancement: the server renders a working month/day/slot picker (links, ?month=&date=&tz=&slot=)
// and a plain POST details form; this script takes over those same elements when JavaScript is available.
(function () {
  "use strict";

  // ---------------------------------------------------------------- embed: responsive iframe height
  var inFrame = window.parent && window.parent !== window;
  function postHeight() {
    if (!inFrame) return;
    var h = Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0);
    window.parent.postMessage({ type: "aal.page_height", height: h }, "*");
  }
  if (inFrame) {
    window.addEventListener("load", postHeight);
    if (window.ResizeObserver) { try { new ResizeObserver(postHeight).observe(document.documentElement); } catch (e) {} }
    setTimeout(postHeight, 50);
  }

  var cfgEl = document.getElementById("booking-config");
  if (!cfgEl) return;
  var cfg = JSON.parse(cfgEl.textContent);
  var $ = function (id) { return document.getElementById(id); };

  // ---------------------------------------------------------------- i18n (catalog injected by booking.html)
  var I18N = window.AAL_I18N || {};
  var LOCALE = window.AAL_LOCALE || document.documentElement.lang || "en";
  function tr(key, vars) {
    var s = I18N[key] || key;
    if (vars) s = s.replace(/\{(\w+)\}/g, function (m, k) { return vars[k] !== undefined && vars[k] !== null ? String(vars[k]) : m; });
    return s;
  }

  // ---------------------------------------------------------------- state
  var store = {
    get: function (k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { window.localStorage.setItem(k, v); } catch (e) {} }
  };
  var detected = null;
  try { detected = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) {}
  var state = {
    tz: cfg.tz || store.get("aal_tz") || detected || cfg.host_tz || "UTC",
    h12: false,
    duration: cfg.duration,
    year: 0, month: 0, // month is 1-12
    days: {},           // "YYYY-MM-DD" -> [slots]
    selectedDate: cfg.date || null,
    slot: null,
    idemKey: null,
    loading: false,
    autoJumps: 0
  };
  if (cfg.selected_slot) { state.slot = cfg.selected_slot; }
  var h12pref = store.get("aal_h12");
  state.h12 = h12pref ? h12pref === "1" : /^America\//.test(state.tz);

  var tzApply = $("tz-apply"); if (tzApply) tzApply.hidden = true;
  var tzForm = $("tz-form"); if (tzForm) tzForm.addEventListener("submit", function (ev) { ev.preventDefault(); });
  var h12wrap = $("h12-wrap"); if (h12wrap) h12wrap.hidden = false;
  var tzSel = $("tz-select");
  if (tzSel) {
    if (!Array.prototype.some.call(tzSel.options, function (o) { return o.value === state.tz; })) {
      var opt = document.createElement("option"); opt.value = state.tz; opt.textContent = state.tz; tzSel.insertBefore(opt, tzSel.firstChild);
    }
    tzSel.value = state.tz;
    tzSel.addEventListener("change", function () {
      state.tz = tzSel.value; store.set("aal_tz", state.tz); state.slot = null; updateTzHelp(); loadMonth();
    });
  }
  function updateTzHelp() {
    var help = $("tz-help");
    if (help) help.textContent = (detected && detected !== state.tz) ? tr("book.device_tz", { tz: detected }) : tr("book.times_in_tz");
  }
  updateTzHelp();
  var h12box = $("h12");
  if (h12box) {
    h12box.checked = state.h12;
    h12box.addEventListener("change", function () { state.h12 = h12box.checked; store.set("aal_h12", state.h12 ? "1" : "0"); renderSlots(); });
  }

  // ---------------------------------------------------------------- date helpers
  function todayIn(tz) {
    try { return new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date()); }
    catch (e) { return new Date().toISOString().slice(0, 10); }
  }
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function ymd(y, m, d) { return y + "-" + pad(m) + "-" + pad(d); }
  function daysInMonth(y, m) { return new Date(Date.UTC(y, m, 0)).getUTCDate(); }
  function prettyDate(s, opts) {
    var p = s.split("-");
    return new Date(Date.UTC(+p[0], +p[1] - 1, +p[2])).toLocaleDateString(LOCALE, Object.assign({ timeZone: "UTC" }, opts || { weekday: "long", day: "numeric", month: "long", year: "numeric" }));
  }
  var initial = cfg.month ? cfg.month + "-01" : (cfg.date || todayIn(state.tz));
  state.year = +initial.slice(0, 4); state.month = +initial.slice(5, 7);

  // ---------------------------------------------------------------- availability
  function setState(msg, kind) {
    var el = $("pick-state");
    if (!el) return;
    el.innerHTML = "";
    if (!msg) return;
    var d = document.createElement("div"); d.className = "state" + (kind ? " " + kind : ""); d.textContent = msg; el.appendChild(d);
  }
  function apiBase() { return "/api/v1/public/event-types/" + encodeURIComponent(cfg.owner_slug) + "/" + encodeURIComponent(cfg.event_slug); }

  function loadMonth() {
    var today = todayIn(state.tz);
    var first = ymd(state.year, state.month, 1);
    var last = ymd(state.year, state.month, daysInMonth(state.year, state.month));
    var from = first < today ? today : first;
    state.days = {};
    renderGrid();
    $("slots").innerHTML = "";
    if (last < today) { setState(tr("book.no_times_this_month"), ""); return; }
    var qs = "?from=" + from + "&to=" + last + "&timezone=" + encodeURIComponent(state.tz) + "&duration=" + state.duration;
    if (cfg.mode === "reschedule" && cfg.manage_token) qs += "&reschedule_token=" + encodeURIComponent(cfg.manage_token);
    state.loading = true;
    setState(tr("book.loading"), "");
    var reqMonth = state.year * 100 + state.month, reqTz = state.tz, reqDur = state.duration;
    window.api(apiBase() + "/availability" + qs).then(function (data) {
      if (reqMonth !== state.year * 100 + state.month || reqTz !== state.tz || reqDur !== state.duration) return;
      state.loading = false;
      (data.days || []).forEach(function (d) { state.days[d.date] = d.slots; });
      var keys = Object.keys(state.days).sort();
      if (!keys.length) {
        if (state.autoJumps < 2 && !cfg.month) { state.autoJumps++; shiftMonth(1); return; }
        setState(tr("book.no_times_month", { month: monthTitle() }), "");
      } else {
        setState("", "");
        state.autoJumps = 99;
      }
      if (!state.selectedDate || !state.days[state.selectedDate]) state.selectedDate = null;
      renderGrid();
      renderSlots();
    }).catch(function (err) {
      state.loading = false;
      if (err.data && err.data.error && err.data.error.code === "CALENDAR_UNAVAILABLE") {
        setState(tr("book.calendar_unavailable"), "warn");
      } else if (err.status === 404) {
        setState(tr("book.event_not_accepting"), "error");
      } else {
        setState(err.message || tr("book.load_error"), "error");
      }
      renderGrid();
    });
  }

  function monthTitle() {
    return new Date(Date.UTC(state.year, state.month - 1, 1)).toLocaleDateString(LOCALE, { timeZone: "UTC", month: "long", year: "numeric" });
  }
  function shiftMonth(delta) {
    var m = state.month + delta, y = state.year;
    if (m < 1) { m = 12; y--; } if (m > 12) { m = 1; y++; }
    state.year = y; state.month = m; state.selectedDate = null; state.slot = null;
    loadMonth();
  }
  var prev = $("cal-prev"), next = $("cal-next");
  function navMonth(ev, delta) {
    if (ev) ev.preventDefault();
    var el = delta < 0 ? prev : next;
    if (el && el.getAttribute("aria-disabled") === "true") return;
    state.autoJumps = 99; shiftMonth(delta);
  }
  if (prev) prev.addEventListener("click", function (ev) { navMonth(ev, -1); });
  if (next) next.addEventListener("click", function (ev) { navMonth(ev, 1); });

  // ---------------------------------------------------------------- calendar grid (keyboard accessible, roving tabindex)
  var weekdayCache = null;
  function weekdayNames() { // [[long, short], ...] Monday first, in the page locale
    if (weekdayCache) return weekdayCache;
    var longs = (I18N["date.weekdays"] || "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday,Sunday").split(",");
    var shorts = (I18N["date.weekdays_short"] || "Mon,Tue,Wed,Thu,Fri,Sat,Sun").split(",");
    weekdayCache = longs.map(function (l, i) {
      var d = new Date(Date.UTC(2024, 0, 1 + i)); // 2024-01-01 was a Monday
      try {
        return [new Intl.DateTimeFormat(LOCALE, { weekday: "long", timeZone: "UTC" }).format(d),
                new Intl.DateTimeFormat(LOCALE, { weekday: "short", timeZone: "UTC" }).format(d)];
      } catch (e) { return [l, shorts[i] || l.slice(0, 3)]; }
    });
    return weekdayCache;
  }
  function renderGrid() {
    var grid = $("cal-grid");
    if (!grid) return;
    $("cal-title").textContent = monthTitle();
    var today = todayIn(state.tz);
    if (prev) {
      var atStart = ymd(state.year, state.month, 1) <= today.slice(0, 8) + "01";
      if (atStart) { prev.setAttribute("aria-disabled", "true"); prev.removeAttribute("href"); }
      else { prev.removeAttribute("aria-disabled"); prev.setAttribute("href", "#"); }
    }
    var hadFocus = grid.contains(document.activeElement);
    grid.innerHTML = "";
    weekdayNames().forEach(function (d) {
      var h = document.createElement("div"); h.className = "dow";
      var ab = document.createElement("abbr"); ab.title = d[0]; ab.textContent = d[1]; h.appendChild(ab); grid.appendChild(h);
    });
    var firstDow = (new Date(Date.UTC(state.year, state.month - 1, 1)).getUTCDay() + 6) % 7;
    for (var i = 0; i < firstDow; i++) { var e = document.createElement("div"); e.setAttribute("aria-hidden", "true"); grid.appendChild(e); }
    var n = daysInMonth(state.year, state.month), focusDate = state.selectedDate;
    if (!focusDate) { var ks = Object.keys(state.days).sort(); focusDate = ks[0] || null; }
    for (var d = 1; d <= n; d++) {
      var key = ymd(state.year, state.month, d);
      var b = document.createElement("button");
      b.type = "button"; b.className = "cal-day"; b.textContent = d; b.dataset.date = key;
      var slots = state.days[key];
      var avail = !!(slots && slots.some(function (s) { return !s.full; }) || (slots && cfg.waitlist && slots.length));
      if (avail) b.classList.add("available"); else b.setAttribute("aria-disabled", "true");
      if (key === today) b.classList.add("today");
      if (key === state.selectedDate) { b.classList.add("selected"); b.setAttribute("aria-pressed", "true"); }
      else if (avail) b.setAttribute("aria-pressed", "false");
      b.setAttribute("aria-label", prettyDate(key) + ", " + (avail ? (slots.length === 1 ? tr("book.time_available") : tr("book.times_available", { n: slots.length })) : tr("book.no_times")));
      b.tabIndex = (key === focusDate || (!focusDate && d === 1)) ? 0 : -1;
      grid.appendChild(b);
    }
    if (hadFocus || state.focusGrid) {
      state.focusGrid = false;
      var f = grid.querySelector('.cal-day[tabindex="0"]'); if (f) f.focus();
    }
  }
  var gridEl = $("cal-grid");
  if (gridEl) {
    gridEl.addEventListener("click", function (ev) {
      var b = ev.target.closest(".cal-day");
      if (!b || b.getAttribute("aria-disabled") === "true") return;
      selectDate(b.dataset.date, true);
    });
    gridEl.addEventListener("keydown", function (ev) {
      var b = ev.target.closest(".cal-day");
      if (!b) return;
      var moves = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
      var all = Array.prototype.slice.call(gridEl.querySelectorAll(".cal-day"));
      var cur = all.indexOf(b), dow = (new Date(Date.UTC(state.year, state.month - 1, cur + 1)).getUTCDay() + 6) % 7;
      var target = null;
      if (moves[ev.key] !== undefined) target = cur + moves[ev.key];
      else if (ev.key === "Home") target = cur - dow;
      else if (ev.key === "End") target = cur + (6 - dow);
      if (target !== null) {
        ev.preventDefault();
        target = Math.max(0, Math.min(all.length - 1, target));
        b.tabIndex = -1; all[target].tabIndex = 0; all[target].focus();
      } else if (ev.key === "PageUp" || ev.key === "PageDown") {
        ev.preventDefault();
        state.focusGrid = true;
        navMonth(null, ev.key === "PageUp" ? -1 : 1);
      } else if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        if (b.getAttribute("aria-disabled") !== "true") selectDate(b.dataset.date, true);
      }
    });
  }
  function selectDate(date, focusSlots) {
    state.selectedDate = date; state.slot = null;
    renderGrid(); renderSlots();
    if (focusSlots) { var f = document.querySelector("#slots button.slot:not(.full)") || document.querySelector("#slots .slot"); if (f) f.focus(); }
  }

  // ---------------------------------------------------------------- slots
  function fmtTime(iso) {
    try {
      return new Intl.DateTimeFormat(LOCALE, { timeZone: state.tz, hour: state.h12 ? "numeric" : "2-digit", minute: "2-digit", hour12: state.h12 }).format(new Date(iso));
    } catch (e) { return ""; }
  }
  function slotLabel(s) { return fmtTime(s.start_at) || (state.h12 ? s.label_12h : s.label); }
  function renderSlots() {
    var box = $("slots"), title = $("slots-title");
    if (!box) return;
    box.innerHTML = "";
    if (!state.selectedDate) { title.textContent = Object.keys(state.days).length ? tr("book.choose_day") : ""; return; }
    title.textContent = prettyDate(state.selectedDate, { weekday: "long", day: "numeric", month: "long" });
    (state.days[state.selectedDate] || []).forEach(function (s) {
      var row = document.createElement("div"); row.className = "slot-row"; row.setAttribute("role", "listitem");
      var b = document.createElement("button"); b.type = "button"; b.className = "slot";
      var txt = slotLabel(s);
      if (s.full) {
        b.classList.add("full"); b.innerHTML = ""; b.appendChild(document.createTextNode(txt));
        var sm = document.createElement("small"); sm.textContent = tr("book.full_join_waitlist"); b.appendChild(sm);
        b.addEventListener("click", function () { openWaitlist(s); });
      } else {
        b.appendChild(document.createTextNode(txt));
        if (cfg.show_seats && s.seats_remaining !== null && s.seats_remaining !== undefined) {
          var sm2 = document.createElement("small"); sm2.textContent = s.seats_remaining === 1 ? tr("book.spot_left") : tr("book.spots_left", { n: s.seats_remaining }); b.appendChild(sm2);
        }
        b.setAttribute("aria-label", tr("book.select_time_on", { time: txt, date: prettyDate(state.selectedDate) }));
        if (state.slot && state.slot.start_at === s.start_at) b.classList.add("selected");
        b.setAttribute("aria-pressed", state.slot && state.slot.start_at === s.start_at ? "true" : "false");
        b.addEventListener("click", function () { state.slot = s; renderSlots(); var nb = document.querySelector("#slots .btn-primary"); if (nb) nb.focus(); });
      }
      row.appendChild(b);
      if (state.slot && state.slot.start_at === s.start_at) {
        var go = document.createElement("button"); go.type = "button"; go.className = "btn btn-primary"; go.textContent = tr("book.next");
        go.setAttribute("aria-label", tr("book.continue_with", { time: txt }));
        go.addEventListener("click", function () { openDetails(); });
        row.appendChild(go);
      }
      box.appendChild(row);
    });
    postHeight();
  }

  // ---------------------------------------------------------------- duration selector
  Array.prototype.forEach.call(document.querySelectorAll("[data-duration]"), function (btn) {
    btn.addEventListener("click", function (ev) {
      ev.preventDefault();
      state.duration = +btn.dataset.duration; state.slot = null;
      Array.prototype.forEach.call(document.querySelectorAll("[data-duration]"), function (b) { b.setAttribute("aria-pressed", b === btn ? "true" : "false"); });
      var dl = $("duration-label"); if (dl) dl.textContent = tr("book.minutes", { n: state.duration });
      loadMonth();
    });
  });

  // ---------------------------------------------------------------- details step
  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return "k" + Date.now().toString(36) + Math.random().toString(36).slice(2);
  }
  function summaryText(s) {
    var endLabel = fmtTime(s.end_at);
    return prettyDate(state.selectedDate) + ", " + slotLabel(s) + (endLabel ? " – " + endLabel : "") + " · " + tr("book.minutes", { n: state.duration }) + " · " + state.tz.replace(/_/g, " ");
  }
  function openDetails() {
    if (!state.slot) return;
    state.idemKey = uuid();
    var se = $("ssr-error"); if (se) se.hidden = true;
    $("selected-summary").textContent = summaryText(state.slot);
    $("step-pick").hidden = true; $("step-details").hidden = false;
    hideError();
    var first = document.querySelector("#details-form input:not([type=hidden]):not([tabindex='-1']), #details-form textarea");
    if (first) first.focus();
    postHeight();
  }
  function backToPick(reload) {
    $("step-details").hidden = true; $("step-pick").hidden = false;
    var wl = $("step-waitlist"); if (wl) wl.hidden = true;
    if (reload) { state.slot = null; loadMonth(); } else { renderSlots(); }
    var sel = document.querySelector("#slots .slot.selected") || $("pick-heading");
    if (sel) sel.focus();
    postHeight();
  }
  var backBtn = $("back-btn");
  if (backBtn) backBtn.addEventListener("click", function (ev) { ev.preventDefault(); backToPick(false); });

  function showError(msg) { var e = $("form-error"); e.textContent = msg; e.hidden = false; e.setAttribute("tabindex", "-1"); e.focus(); postHeight(); }
  function hideError() {
    var e = $("form-error"); if (e) e.hidden = true;
    Array.prototype.forEach.call(document.querySelectorAll(".field-error"), function (x) { x.hidden = true; x.textContent = ""; });
    Array.prototype.forEach.call(document.querySelectorAll("[aria-invalid]"), function (x) { x.removeAttribute("aria-invalid"); });
  }
  function fieldError(qid, msg) {
    var f = document.querySelector('[data-qid="' + qid + '"]');
    if (f) {
      var p = f.querySelector(".field-error"); if (p) { p.textContent = msg; p.hidden = false; }
      var inp = f.querySelector("input,select,textarea"); if (inp) inp.setAttribute("aria-invalid", "true");
    } else {
      var el = $("f-" + qid); if (el) el.setAttribute("aria-invalid", "true");
      var pe = $("err-" + qid); if (pe) { pe.textContent = msg; pe.hidden = false; }
    }
  }
  function collectAnswers() {
    var out = [];
    Array.prototype.forEach.call(document.querySelectorAll("[data-qid]"), function (f) {
      var qid = f.dataset.qid, type = f.dataset.qtype, val;
      var boxes = f.querySelectorAll("input[type=checkbox]"), radios = f.querySelectorAll("input[type=radio]");
      if (radios.length) {
        var c = f.querySelector("input[type=radio]:checked"); val = c ? c.value : "";
      } else if (boxes.length > 1 || (boxes.length === 1 && boxes[0].value !== "on")) {
        val = Array.prototype.filter.call(boxes, function (b) { return b.checked; }).map(function (b) { return b.value; });
      } else if (boxes.length === 1) {
        val = boxes[0].checked;
      } else {
        var inp = f.querySelector("input,select,textarea"); val = inp ? inp.value.trim() : "";
      }
      out.push({ question_id: qid, value: val });
    });
    return out;
  }
  function localValidate() {
    var ok = true;
    var name = $("f-name"), email = $("f-email");
    if (name && !name.value.trim()) { fieldError("name", tr("book.name_required")); ok = false; }
    if (email && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.value.trim())) { fieldError("email", tr("book.invalid_email")); ok = false; }
    Array.prototype.forEach.call(document.querySelectorAll('[data-qid][data-required="true"]'), function (f) {
      var a = collectAnswers().filter(function (x) { return x.question_id === f.dataset.qid; })[0];
      var v = a ? a.value : "";
      if (v === "" || v === false || (Array.isArray(v) && !v.length)) { fieldError(f.dataset.qid, tr("book.field_required")); ok = false; }
    });
    return ok;
  }
  function withEmbed(url) {
    if (!cfg.embed_qs) return url;
    return url + (url.indexOf("?") >= 0 ? "&" : "?") + cfg.embed_qs;
  }

  var form = $("details-form");
  if (form) form.noValidate = true; // JS shows its own inline messages; without JS native validation applies
  if (form) form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    hideError();
    if (!state.slot) { backToPick(false); return; }
    var btn = $("submit-btn");
    var req, url;
    if (cfg.mode === "reschedule") {
      url = "/api/v1/public/bookings/" + encodeURIComponent(cfg.manage_token) + "/reschedule";
      req = { start_at: state.slot.start_at, slot_token: state.slot.slot_token, timezone: state.tz, reason: ($("f-reason") || {}).value || null };
    } else {
      if (!localValidate()) { showError(tr("book.complete_required")); return; }
      url = "/api/v1/public/bookings";
      var loc =document.querySelector('input[name="location"]:checked');
      var guests = ($("f-guests") || {}).value || "";
      req = {
        owner_slug: cfg.owner_slug, event_type_slug: cfg.event_slug, event_type_id: cfg.event_type_id,
        start_at: state.slot.start_at, timezone: state.tz, slot_token: state.slot.slot_token, duration: state.duration,
        invitee: {
          name: $("f-name").value.trim(), email: $("f-email").value.trim(), phone: (($("f-phone") || {}).value || "").trim() || null,
          timezone: state.tz, guests: guests.split(/[\s,;]+/).filter(Boolean)
        },
        answers: collectAnswers(), utm: cfg.utm || {}, location: loc ? loc.value : null,
        website: ($("f-website") || {}).value || "", rsid: cfg.rsid || null, source: cfg.embed ? "embed" : "booking_page",
        consent: { privacy_notice_version: (document.querySelector('meta[name="privacy-version"]') || {}).content || "unknown", accepted_at: new Date().toISOString() },
        verification_code: (($("f-code") || {}).value || "").trim() || null
      };
    }
    btn.disabled = true; var oldTxt = btn.textContent; btn.textContent = tr("book.submitting");
    var opts = { method: "POST", body: req, headers: {} };
    if (cfg.mode !== "reschedule") opts.headers["Idempotency-Key"] = state.idemKey || uuid();
    window.api(url, opts).then(function (res) {
      // Only now (backend-confirmed) do we leave the form; the confirmation page renders the true status.
      window.location.assign(withEmbed(res.next_url || ("/booking/" + res.manage_token + "/confirmed")));
    }).catch(function (err) {
      btn.disabled = false; btn.textContent = oldTxt;
      var code = err.data && err.data.error && err.data.error.code;
      if (code === "SLOT_NO_LONGER_AVAILABLE" || code === "SLOT_TOKEN_INVALID") {
        // stale slot: taken meanwhile, host availability changed, or the selection expired
        backToPick(true);
        setState(err.message, "error");
        var ps = $("pick-state"); if (ps) { ps.setAttribute("tabindex", "-1"); ps.focus(); }
        return;
      }
      var fields = err.data && err.data.error && err.data.error.details && err.data.error.details.fields;
      if (fields) Object.keys(fields).forEach(function (k) { fieldError(k, fields[k]); });
      showError(err.message || tr("common.generic_error"));
    });
  });

  // ---------------------------------------------------------------- email verification code
  var sendCode = $("send-code");
  if (sendCode) sendCode.addEventListener("click", function (ev) {
    ev.preventDefault();
    var email = ($("f-email").value || "").trim();
    if (!email) { $("f-email").focus(); showError(tr("book.enter_email_first")); return; }
    sendCode.disabled = true;
    window.api("/api/v1/public/verify-email", { method: "POST", body: { email: email, event_type_id: cfg.event_type_id } }).then(function () {
      $("code-help").textContent = tr("book.code_sent", { email: email });
      setTimeout(function () { sendCode.disabled = false; sendCode.textContent = tr("book.resend"); }, 20000);
    }).catch(function (err) { sendCode.disabled = false; showError(err.message); });
  });

  // ---------------------------------------------------------------- waitlist
  var wlSlot = null;
  function openWaitlist(s) {
    wlSlot = s;
    $("wl-summary").textContent = tr("book.waitlist_full", { when: prettyDate(state.selectedDate) + ", " + slotLabel(s) });
    $("step-pick").hidden = true; $("step-waitlist").hidden = false; $("wl-msg").textContent = "";
    postHeight();
  }
  var wlBack = $("wl-back");
  if (wlBack) wlBack.addEventListener("click", function () { backToPick(false); });
  var wlForm = $("waitlist-form");
  if (wlForm) wlForm.addEventListener("submit", function (ev) {
    ev.preventDefault();
    window.api("/api/v1/public/waitlist", { method: "POST", body: {
      owner_slug: cfg.owner_slug, event_type_slug: cfg.event_slug, start_at: wlSlot.start_at,
      name: $("wl-name").value.trim(), email: $("wl-email").value.trim()
    } }).then(function (r) {
      var m = $("wl-msg"); m.className = "alert alert-success"; m.textContent = r.message;
    }).catch(function (err) { var m = $("wl-msg"); m.className = "alert alert-error"; m.textContent = err.message; });
  });

  loadMonth();
})();
