# The signup endpoint

The viewer's welcome card asks for an email. When someone gives one, the local
server sends it to a signup endpoint with one `POST` (see
[`python/src/agentplay/account.py`](../python/src/agentplay/account.py)). The
built-in endpoint is `DEFAULT_SIGNUP_URL`, a Google Apps Script web app that
appends a row to Entelligence's signup Sheet.

## What is sent

```json
{"event": "signup", "email": "you@company.com", "installId": "3f9c…", "version": "0.1.0", "at": "2026-10-05T23:10:00Z"}
```

`event` is `signup` or `disconnect`. `installId` is a random id created on first
use and kept in `~/.agentplay/account.json`, so a disconnect can be matched to
its signup. Nothing about any session is ever sent.

## A Google Sheet as the endpoint

1. Create a Google Sheet, then open **Extensions → Apps Script** from inside
   it, so the script is attached to that Sheet. A script made from
   script.google.com is attached to a different file.
2. Replace the editor's contents with this. It creates a `signups` tab with
   headers the first time it runs:

   ```js
   function doPost(e) {
     const d = JSON.parse(e.postData.contents);
     const event = d.event === 'disconnect' ? 'disconnect' : 'signup';
     const email = String(d.email || '').trim().slice(0, 254);
     if (!/^[^@\s\ufeff]{1,64}@[^@\s\ufeff]+\.[^@\s\ufeff]{2,}$/.test(email)) {  // the same check as account.py
       return reply({ ok: false });
     }
     // A cell starting with = + - @ (or tab, CR) would become a formula.
     const cell = (v, n) => String(v || '').slice(0, n).replace(/^[=+\-@\t\r]/, "'$&");
     const ss = SpreadsheetApp.getActive();
     const sheet = ss.getSheetByName('signups') ||
       ss.insertSheet('signups').appendRow(['received', 'event', 'email', 'installId', 'version', 'at']);
     sheet.appendRow([new Date(), event, cell(email, 254), cell(d.installId, 64), cell(d.version, 32), cell(d.at, 32)]);
     return reply({ ok: true });
   }

   function reply(body) {
     return ContentService.createTextOutput(JSON.stringify(body))
       .setMimeType(ContentService.MimeType.JSON);
   }
   ```

3. **Deploy → New deployment → Web app.** Execute as: *Me*. Who has access:
   *Anyone*. Copy the web app URL (it ends in `/exec`).
4. Try it locally:

   ```bash
   AGENTPLAY_SIGNUP_URL='https://script.google.com/macros/s/…/exec' agentplay view
   ```

5. To point released builds at it, set `DEFAULT_SIGNUP_URL` in `account.py`.

   To change the script later, keep the URL: **Deploy → Manage deployments →
   edit → Version: New version**. A *new deployment* gets a new URL, and
   released copies of agentplay would keep posting to the old one.

   The script checks the email and stops any cell from being read as a
   formula.

The URL must be `https` to a trusted host (`script.google.com`, its
`script.googleusercontent.com` redirect, or `entelligence.ai`), or `http` to this
machine for testing. Redirects are checked the same way. To send to another
host, add it to `TRUSTED_HOSTS` in `account.py`; any other URL turns the card off.

The endpoint must answer `{"ok": true}` once the row is written. Anything else,
including Apps Script's HTML error page (which it serves with status 200 when the
script throws, for example when the `signups` tab is missing), counts as a failed
signup: the viewer shows the retry message and nothing is stored locally.

A `disconnect` row is a deletion request: remove that email's `signup` rows
when you see one.

## Testing without a sheet

```bash
python3 scripts/fake_signup_endpoint.py          # prints every event it receives
AGENTPLAY_SIGNUP_URL=http://127.0.0.1:8787/exec agentplay view
```

## Turning it off

`DO_NOT_TRACK=1` or `AGENTPLAY_NO_NETWORK=1` hides the card and blocks the
request, whatever endpoint is set.
