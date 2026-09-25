# eBay sales loop — operator runbook

The governed pipeline (draft → clarify → review → exact-approval
publish) is code-complete and drilled. What only the operator can do is
**eBay authentication** — the eBay user token lives in Capitol's org
credential bundle (referenced by `ebay_account_ref`), not in conch, and
the sandbox token expired in September with no refresh token stored.

## A. Resume sandbox validation (do this first)

1. **eBay developer console** (developer.ebay.com → your account →
   Sandbox): create/log into your sandbox test user, then mint a fresh
   **user access token** for the sandbox keyset (Sell APIs: Inventory,
   Account, Fulfillment). The quick path is "User Tokens (eBay Sign-In)"
   → Sandbox → sign in as the test user → copy the OAuth user token.
   If you use the OAuth consent flow instead, capture the **refresh
   token** too — that's what was missing last time and why the token
   expiring parked publishing.
2. **Update Capitol's org credential**: Capitol platform
   (http://localhost:5173 → org `7d577196…` → Organization Settings →
   credentials/connections) — replace the token in the bundle named by
   `ebay_account_ref` (`org-ebay-sandbox`). Paste tokens only into
   Capitol's credential UI — never into conch config, chat, or files.
3. **Sanity-check the publish path** with a throwaway listing (see the
   checklist below); the exact-approval challenge protects you — deny
   it if you only want to prove the draft works.

## B. First sale checklist (sandbox)

1. Stack up: Capitol gateway :8300 / platform :8811 running;
   `conch-edge` daemon running **on the merged edge build** (restart it
   after updating: `conch-edge uninstall && conch-edge install`, or run
   `conch-edge` in a foreground terminal).
2. Config present (already wired on this machine):
   `ebay_agent = d96b9401-6a5b-5f45-809e-165edf41ebed` (eBay Sales
   Operator), the watched-folder binding
   (`folder_watch_ebay = ~/EbayDrop` +
   `folder_watch_ebay_handler = pack:ebay-listing` — the general
   folder-watch pattern, eBay is just the first binding), and
   `status_page_url = https://conch-status.vercel.app` (write token in
   `~/.config/conch/env`).
3. **Drop**: copy 1–12 item photos into `~/EbayDrop`, optionally with a
   `notes.txt` ("pristine, original box, size 11"). Within ~15s the
   daemon quarantines them, moves originals to `processed/<drop-id>/`,
   and starts the draft.
4. **Watch**: `/ebay drops` in the shell, or the status page
   (https://conch-status.vercel.app — read token in
   `~/.config/conch/status-read-token`). Notifications also ride
   `notify_channel` when you configure Slack/Matrix.
5. **Answer** a clarifying question: `/ebay answer <drop-id> <text>`.
6. **Publish gate**: the revision summary arrives with an approval id
   and the exact challenge. `/ebay approve <id>` publishes (sandbox);
   `/ebay deny <id>` drops it. A folder drop can never auto-publish.
7. **Verify**: the listing URL lands in `/ebay drops`, the status page,
   and sandbox.ebay.com.

## C. Going live (production) — deliberate switch, in order

1. eBay production keyset + OAuth consent for your real seller account
   (business policies opted in: fulfillment/payment/return policy IDs,
   merchant location). Store the production user+refresh token in a
   **new** Capitol org credential bundle (e.g. `org-ebay-prod`).
2. Point the config at production deliberately:
   `ebay_account_ref=org-ebay-prod`, real
   `ebay_fulfillment_policy_id`/`ebay_payment_policy_id`/
   `ebay_return_policy_id`/`ebay_merchant_location_key`.
3. **Set caps before the first live listing**: `ebay_max_price_usd` and
   `ebay_allowed_category_ids` — the clamp decides auto vs. explicit
   approval on channel surfaces; folder drops always require approval
   regardless.
4. Leave `ebay_channel_auto_publish=false` until you've published a few
   listings by hand and trust the caps.
5. First live listing: something cheap and expendable. Approve the
   challenge, verify on ebay.com, check fees/policies rendered as
   expected — then scale up.

## D. Troubleshooting

- Drop ignored → `/ebay drops` empty: the daemon isn't running the new
  build, or the `folder_watch_ebay` /
  `folder_watch_ebay_handler` pair is unset. Files in `rejected/`
  carry a `.reason.txt`.
- `Capitol credential needed` in a session: the gateway bearer is fine
  (drafting worked) but the **eBay** credential inside Capitol failed —
  redo section A.
- Status page stale: exporter pushes only on change every ~30s; check
  the daemon log for `status export` lines. The page can never act —
  approvals only exist in the shell/thread.
