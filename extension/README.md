# AAL Scheduler browser extension (Chrome / Edge, Manifest V3)

Toolbar popup for hosts:

- **Event types:** copy a booking link, or create and copy a **single-use link** in one click.
- **Upcoming meetings:** the next 8 meetings in your timezone, with Join/Open links.
- **Open app:** jumps to the web dashboard.

## Install (unpacked)

1. In the web app, open **Integrations → API keys** and create a key with the `read` and `write` scopes.
   (`read` alone is enough if you only want to copy links and see meetings.)
2. Open `chrome://extensions` (or `edge://extensions`), turn on **Developer mode** and click **Load unpacked**.
   Choose this `extension/` folder.
3. Click the toolbar icon, then **Set up**. Enter your server URL (for example `https://aal-scheduler.onrender.com`,
   or `http://127.0.0.1:8000` for local development) and the API key, then **Save & test**. The browser asks you
   to allow access to that one site.

To ship it through the Chrome Web Store or Edge Add-ons, zip the folder's contents (`manifest.json` at the root of the zip)
and upload it.

## How it works

| Popup action | API call |
|---|---|
| Verify the key | `GET /api/v1/me` |
| List event types | `GET /api/v1/ext/event-types` |
| Single-use link | `POST /api/v1/ext/event-types/{id}/single-use-link` (needs the `write` scope) |
| Upcoming meetings | `GET /api/v1/ext/upcoming?limit=8` |

The `/api/v1/ext/*` endpoints live in `app/routers/api_ext.py`. They use the same API-key authentication, plan check,
rate limits and workspace scoping as the rest of `/api/v1`. Members see only the meetings they host; admins see the
whole workspace.

## Security notes

- The API key is kept in `chrome.storage.local` for this browser profile. It is never synced and never sent anywhere
  except the server you configured.
- The extension has no blanket host access. It requests permission for the single origin you enter
  (`optional_host_permissions`), and **Disconnect** removes both the key and that permission.
- Only `https://` servers are accepted, except `localhost` and `127.0.0.1` for development.
- Pages are built with DOM APIs (no `innerHTML`), and the extension CSP allows only bundled scripts.
- Revoke a lost key in **Integrations → API keys**. It stops working immediately.

## Files

`manifest.json`, `popup.html/css/js` (toolbar UI), `options.html/js` (setup), `api.js` (shared client), `icons/`
(generated from the same artwork as the PWA icons by `app/routers/pwa.py`).
