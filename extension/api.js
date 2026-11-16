// Shared API client for the popup and options page.
// Talks to the AAL Scheduler REST API with a workspace API key (Integrations > API keys).
const DEFAULTS = { server: "", apiKey: "" };

export async function getConfig() {
  const c = await chrome.storage.local.get(DEFAULTS);
  return { server: (c.server || "").replace(/\/+$/, ""), apiKey: c.apiKey || "" };
}

export async function saveConfig(server, apiKey) {
  await chrome.storage.local.set({ server: server.replace(/\/+$/, ""), apiKey: apiKey.trim() });
}

export function originPattern(server) {
  const u = new URL(server);
  return `${u.protocol}//${u.hostname}/*`;
}

export async function hasHostPermission(server) {
  try { return await chrome.permissions.contains({ origins: [originPattern(server)] }); } catch (e) { return false; }
}

export class ApiError extends Error {
  constructor(message, status, code) { super(message); this.status = status; this.code = code; }
}

export async function api(path, { method = "GET", body } = {}) {
  const { server, apiKey } = await getConfig();
  if (!server || !apiKey) throw new ApiError("Not configured", 0, "NOT_CONFIGURED");
  const headers = { Authorization: `Bearer ${apiKey}`, Accept: "application/json" };
  if (body) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["Idempotency-Key"] = crypto.randomUUID();
  let res;
  try {
    res = await fetch(server + path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  } catch (e) {
    throw new ApiError("Can't reach the server. Check the URL and your connection.", 0, "NETWORK");
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) {
    const err = (data && data.error) || {};
    throw new ApiError(err.message || res.statusText || "Request failed", res.status, err.code);
  }
  return data;
}
