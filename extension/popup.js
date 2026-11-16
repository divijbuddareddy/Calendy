import { api, getConfig, hasHostPermission } from "./api.js";

const $ = (id) => document.getElementById(id);

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 1800);
}

async function copy(text, label = "Link copied") {
  await navigator.clipboard.writeText(text);
  toast(label);
}

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "style") n.setAttribute("style", v);
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const c of children) n.append(c instanceof Node ? c : document.createTextNode(c ?? ""));
  return n;
}

function showError(e) {
  const box = $("error");
  box.hidden = false;
  box.textContent = e.status === 401 ? "Your API key was rejected. Update it in Settings."
    : e.status === 403 ? (e.message || "Your plan or role doesn't allow this.")
    : (e.message || "Something went wrong.");
}

async function loadEventTypes() {
  const list = $("event-types");
  const { data } = await api("/api/v1/ext/event-types");
  list.replaceChildren();
  if (!data.length) { list.append(el("li", { class: "muted" }, "No active event types yet.")); return; }
  for (const et of data) {
    const single = el("button", { title: "Create a link that works for one booking", onclick: async (ev) => {
      const b = ev.currentTarget; b.disabled = true;
      try {
        const r = await api(`/api/v1/ext/event-types/${encodeURIComponent(et.id)}/single-use-link`, { method: "POST" });
        await copy(r.url, "Single-use link copied");
        b.textContent = "Copied ✓";
      } catch (e) { showError(e); b.disabled = false; }
    } }, "Single-use");
    list.append(el("li", {},
      el("span", { class: "dot", style: `background:${et.color || "#3b47d6"}` }),
      el("div", { class: "grow" }, el("div", { class: "name", title: et.name }, et.name),
        el("div", { class: "muted" }, `${et.duration} min${et.mine ? "" : " · team"}`)),
      el("button", { class: "primary", onclick: () => copy(et.url) }, "Copy link"),
      single));
  }
}

async function loadUpcoming() {
  const list = $("upcoming");
  const { data, timezone } = await api("/api/v1/ext/upcoming?limit=8");
  $("tz").textContent = `· ${timezone}`;
  list.replaceChildren();
  if (!data.length) { list.append(el("li", { class: "muted" }, "No upcoming meetings.")); return; }
  const { server } = await getConfig();
  for (const m of data) {
    const right = m.join_url
      ? el("a", { href: m.join_url, target: "_blank", rel: "noopener" }, "Join")
      : el("a", { href: `${server}/app/scheduled/${encodeURIComponent(m.id)}`, target: "_blank", rel: "noopener" }, "Open");
    list.append(el("li", {},
      el("div", { class: "grow" },
        el("div", { class: "when" }, m.start_local),
        el("div", { class: "muted" }, [m.event_type, m.invitee && m.invitee.name].filter(Boolean).join(" · "),
          m.status !== "confirmed" ? ` · ${m.status.replace("_", " ")}` : "")),
      right));
  }
}

async function init() {
  $("settings").addEventListener("click", () => chrome.runtime.openOptionsPage());
  $("go-options").addEventListener("click", () => chrome.runtime.openOptionsPage());
  const { server, apiKey } = await getConfig();
  $("open-app").addEventListener("click", (e) => { e.preventDefault(); if (server) chrome.tabs.create({ url: `${server}/app` }); });
  if (!server || !apiKey || !(await hasHostPermission(server))) { $("setup").hidden = false; return; }
  $("main").hidden = false;
  const results = await Promise.allSettled([loadEventTypes(), loadUpcoming()]);
  const failed = results.find((r) => r.status === "rejected");
  if (failed) showError(failed.reason);
}

init();
