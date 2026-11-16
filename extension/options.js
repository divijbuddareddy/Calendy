import { api, getConfig, originPattern, saveConfig } from "./api.js";

const $ = (id) => document.getElementById(id);
const status = (msg, ok = false) => { const s = $("status"); s.textContent = msg; s.className = ok ? "ok" : "error"; };

(async () => {
  const { server, apiKey } = await getConfig();
  $("server").value = server;
  $("key").value = apiKey;
})();

$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  let server = $("server").value.trim();
  try {
    const u = new URL(server);
    if (u.protocol !== "https:" && !["127.0.0.1", "localhost"].includes(u.hostname)) {
      status("Use an https:// URL (plain http is allowed only for localhost).");
      return;
    }
    server = u.origin;
  } catch (err) {
    status("Enter a valid URL, e.g. https://aal-scheduler.onrender.com");
    return;
  }
  // Ask for access to just this one origin (the extension holds no blanket host access).
  const granted = await chrome.permissions.request({ origins: [originPattern(server)] });
  if (!granted) { status("Permission to contact the server was declined."); return; }
  await saveConfig(server, $("key").value);
  try {
    const me = await api("/api/v1/me");
    status(`Connected as ${me.user.name || me.user.email} · ${me.workspace.name} (scopes: ${me.auth.scopes.join(", ")})`, true);
  } catch (err) {
    status(err.status === 401 ? "The API key was rejected." : err.message);
  }
});

$("clear").addEventListener("click", async () => {
  const { server } = await getConfig();
  await chrome.storage.local.clear();
  if (server) { try { await chrome.permissions.remove({ origins: [originPattern(server)] }); } catch (e) { /* ignore */ } }
  $("server").value = ""; $("key").value = "";
  status("Disconnected.", true);
});
