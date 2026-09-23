# Conch browser-capture satellite

A Chrome (MV3) extension plus a Python native-messaging host that turn
browser work into capture events for the local Conch kernel — so
`/compile suggestions` can say "you've done this same web procedure 14
times — compile it?" and `/compile from-browser` can draft a card from
what you actually did.

The extension is plain JS — no node_modules, no bundler, no build step —
and ships **inside the conch-shell distribution** as package data
(`conch/satellites/browser_capture/extension`). `/install capture
browser` copies it to a stable per-user location
(`~/.local/share/conch/browser-capture/extension`, honoring
`XDG_DATA_HOME`) so the browser's load-unpacked reference survives pip
upgrades and venv reinstalls; re-run the install step after upgrading
conch to refresh the copy.

## What is captured — and what is never captured

Captured, **only on origins you explicitly allowlist**:

| Event   | What travels                                                  |
| ------- | ------------------------------------------------------------- |
| nav     | the path of the page (no query string, no fragment)            |
| click   | semantic target only: role + accessible label (never coordinates) |
| submit  | the form's name and its field **names** (never values)         |
| copy    | the fact of a copy and the source element's role/label (**never** the copied text) |

Never captured, by construction:

- **Anything on an origin you did not add.** There is no `<all_urls>`
  permission; the content script is registered per allowlisted origin,
  and the background worker drops events from anywhere else as a second
  check.
- **Input values.** Field names only, ever. The Python host rejects any
  submit event whose field list carries a non-string entry.
- **Password and secret fields.** `type=password` and secret-looking
  names (`pass`, `token`, `otp`, `cvv`, …) are skipped at the source —
  a click on a password field sends nothing at all. The host drops
  secret-named fields again as defense in depth.
- **Clipboard contents, keystrokes, page text, screenshots.**

Every event then passes the authoritative Conch `secretguard` scrub in
the native host: an event carrying credential-shaped bytes is rejected
whole (label only reported), never sanitized-and-forwarded.

## Where the data lives

Locally, and nowhere else. The extension talks to `conch-capture-host`
over Chrome native messaging (stdio — the browser-sanctioned transport;
no listening ports, no cloud). The host forwards accepted events to the
edge kernel's control socket (`event.post`), falling back to the kernel
database directly when the daemon is down, and to a small size-capped
local spool (oldest dropped) as the last resort. Events are journaled
as `inbox_received` rows with `source="browser"` in
`~/.local/state/conch/kernel/kernel.db`.

Nothing is captured unless **both** are true:

1. `capture_enabled=true` and `capture_browser=true` in the conch
   config (`/install capture browser` sets both), and
2. you added the origin in the extension's Options page.

The action badge shows the live state: **ON** (capturing), **OFF**
(no origins, host down, or capture disabled), **II** (paused). Click
the toolbar icon to pause/resume.

## Setup (Chrome on macOS is the proven path; Linux paths ship too)

1. In the conch shell: `/install capture browser` — copies the
   extension to the stable per-user directory, writes the
   NativeMessagingHosts manifest for installed Chromium-family browsers
   (Chrome required; Brave/Chromium/Edge covered on macOS and Linux),
   generates the `/bin/sh` host launcher (space-safe, reinstall-proof),
   and enables the config gates. Firefox is a documented follow-up.
2. The one gesture conch cannot do for you — Chrome never allows
   silent extension installs: `chrome://extensions` → Developer mode →
   **Load unpacked** → pick the printed path
   (`~/.local/share/conch/browser-capture/extension`). The id must read
   `hgmjnpkpdnaeekckabogikdcpdfeeghh` (pinned by the manifest `key`; the
   matching private key was generated and discarded — the public key
   only pins the unpacked id and cannot sign anything).
3. Open the extension options, add an origin (e.g. `https://github.com`)
   and approve the per-origin permission prompt — the second
   user-consent gesture, also never silent.
4. Verify: `/install` in conch shows the capture line with the last
   handshake, or run `conch-capture-host status`.

Then: `/compile suggestions` mines recurring browser procedures, and
`/compile from-browser github.com "automate the release dance"` drafts
a card from the captured events.

## Manual test checklist (Chrome)

The Python side carries the invariants under test; this checklist
verifies the thin extension half by hand:

- [ ] Load unpacked; the id is `hgmjnpkpdnaeekckabogikdcpdfeeghh`; the
      badge reads **OFF**.
- [ ] Before adding any origin, browse somewhere: `conch-capture-host
      status` shows no accepted events (nothing captured by default).
- [ ] Add `https://example.com` in Options; the permission prompt names
      exactly that origin; badge flips to **ON** (with
      `capture_browser=true` and the host installed).
- [ ] Visit example.com, click a link: `/compile from-browser
      example.com` sees a nav and a click with role/label — no
      coordinates, no page text.
- [ ] On a page with a login form on an allowlisted origin, type into
      the password field and submit: the journaled submit event lists
      field names only, with the password field absent.
- [ ] Copy some page text: a copy event appears **without** the copied
      text.
- [ ] Visit a non-allowlisted site and click around: nothing is
      journaled.
- [ ] Click the toolbar icon: badge shows **II**; events stop; click
      again to resume.
- [ ] Remove the origin in Options: capture stops immediately and the
      site permission is revoked (check `chrome://extensions` → Details).
- [ ] Stop the edge daemon; keep clicking: events still land (direct
      kernel write) or spool; restart and confirm the spool drains on
      the next handshake (`conch-capture-host status`).
- [ ] Set `capture_browser=false` in the conch config: the badge goes
      **OFF** on the next handshake and the host rejects events.
