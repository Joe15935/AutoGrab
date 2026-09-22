# Provider capabilities: implementation, evidence and limits

Evidence retained from 2026-09-22; updated for the v0.5 alpha implementation.
**PARTIAL. Experimental alpha. Real orders: 0. Payments: 0.** DRY_RUN by default,
LIVE OFF, ARM OFF, REAL_ORDER_SMOKE_TEST_ARMED=false. Prior BWH/DMIT L5 evidence is
frozen; this sprint did not repeat their ordinary cart/checkout acceptance flows.

| Provider | IMPLEMENTED | REAL-SITE VERIFIED |
|---|---|---|
| BandwagonHost | L5 assistance; shared L6/L7 experimental adapter | L5 signed-in checkout PASS; original 48 ordinary products retained; L6/L7 NO |
| DMIT | Public discovery and read-only L5 stages; shared L6/L7 experimental adapter | L5 manual flow: exact PID 266 cart survived refresh, signed-in checkout PASS; L6/L7 NO |
| VMISS | Experimental parser/read-only adapter and persisted rate budget | Official entry only; BLOCKED by Error 1015; no reliable live product baseline or checkout |
| V.PS | Discovery/read-only pre-order configuration | L4 REAL ORDER BOUNDARY VERIFIED; 66 public plans; Order Now was not clicked |
| Apple | Catalogue, target wizard, pickup, request budgets; partial read-only Edge purchase observation | Inventory/catalogue/wizard/research email PASS; Bag BLOCKED; exact bag contents, Add to Bag and checkout NO |

L0 discovery, L1 baseline, L2 opportunity detection, L3 normal Edge opening,
L4 cart / pre-order boundary, L5 checkout, L6 real order created, L7 payment ready.
Implemented L6/L7 code does not raise a provider's verified level.
VMISS L0 partial means its official entry is known, not that live inventory was
retrieved. L2 transitions are regression-verified; no artificial restock or launch
is described as a real opportunity.

## Shared L6/L7 Core — IMPLEMENTED, experimental

The existing PurchaseRunner, PurchaseIntent, SQLite, Native Messaging, SMTP and
guards now support order precheck, persisted nonce, ORDER_UNCERTAIN, reconciliation,
order/invoice/payment-link search, PAYMENT_READY and notification claims. BWH and
DMIT share this lifecycle; provider-specific Edge code supplies page contracts.
No second order engine or new production dependency was added.

**BWH/DMIT L6/L7 REAL-SITE VERIFIED: NO.** Their exact no-charge submit contract
currently exists only in offline experimental DOM fixtures. Neither current
merchant checkout has independently demonstrated that contract. A visible Complete
Order button, login or unchecked terms cannot prove no charge; account credit or
saved payment methods may debit funds while creating an order. Real submission is
therefore blocked, including when an operator selects the separate smoke entry
point. Test markers must not be added to live pages to manufacture proof.

Ordinary monitoring ARM grants no smoke permission. Explicit confirmation for one
intent creates an ephemeral lease of at most 60 seconds, consumed once and never
restored from storage. It additionally requires fresh product/amount/billing,
session, no-charge and SMTP preflight evidence. The nonce, intent and submission
time are committed before dispatch; kill/expiry is checked again immediately
before the action. No real order smoke test was performed.

Crash/disconnect/timeout after that marker preserves ORDER_UNCERTAIN. Recovery
only looks up the same provider/product/intent, then validates matching order and
invoice evidence. Strong NO_ORDER_FOUND needs a complete matching search scope
and time window; even that does not authorize automatic replacement submission.
Unknown account state never releases the uncertain intent or fabricates absence.

PAYMENT_READY needs actual matching order/invoice IDs, an unpaid official invoice
page and exact product/amount. A URL alone is insufficient. The implemented future
email uses **🚨🚨 [PAY NOW] AutoGrab Payment Ready**, includes billing and lifecycle
times, and links to the merchant HTTPS invoice for phone use with login guidance.
Its once-only claim is persisted before SMTP; ambiguous delivery does not resend
automatically. Offline email/receipt fixtures remain labelled simulated, and no
real L7 notification is claimed.

## DMIT

- Baseline: 91 existing public PID records; the prior 30-selectable/61-disabled
  observation remains a dated snapshot, not a current stock guarantee.
- Configuration: real `dmit_cart_2020` page, selected PID, monthly billing and
  price observed. The old uncertain request remains preserved and was not retried.
- A new explicitly authorized ordinary-product DRY RUN used a separate intent.
  Its configuration POST to `/cart.php` returned HTTP 200 with an explicit
  invalid-Linux-hostname response. Auto-generated form data is not automatically
  valid. A corrected research hostname was entered through the normal UI, visibly
  confirmed, then the corrected configuration was submitted once.
- Cart **PASS**: the observed `cart.php?a=add&pid=266` link led to configuration
  index 0, then the cart contained HKG.AS3.Pro.STARTER, USD 79.90, monthly. The
  same product and price remained after an explicit cart refresh. PID identity
  is bound by this observed sequence; the cart itself did not expose a PID field.
- Checkout **PASS**: the observed Checkout link opened `cart.php?a=checkout`,
  displaying signed-in account controls, Personal Information, Payment Details,
  total USD 79.90, an unchecked terms box and **Complete Order**. No terms were
  accepted and no final order was submitted. Account/form values were not saved
  in any public artifact.
- Historical failed XHRs returned 403. Their individual operations cannot be
  reconstructed from retained resource metadata, and 403 alone does not prove
  Cloudflare or a human challenge.
- Official Continue code prevents native submit, sends `ajax=1&a=confproduct`
  through WHMCS's AJAX client, displays a nonempty validation response, or follows
  `cart.php?a=confdomains` on empty success. It lacks a failure callback to restore
  the Continue spinner. Configuration result uncertainty is not permission to retry.
- The adapter now distinguishes ordinary configuration, pending Continue and
  visible validation failure. Fixed codes contain no form values or response body.
  These pauses cannot passively become a successful resume while still blocked.
- This is a normal Edge **manual acceptance flow**, not a certified automatic
  checkout adapter. Read-only page-stage detection does not prove cart identity.
  Automatic cart/configuration/order mutation stays off; the old PID 265
  uncertain intent remains separate and was never retried.

## VMISS

- Official store: [app.vmiss.com/store](https://app.vmiss.com/store), linked by
  the current official website.
- The existing normal Edge task page displayed **Error 1015 / You are being rate
  limited**. It described a temporary denial by the site owner. The page timestamp
  was 10:30 UTC; it was inspected later without refreshing the blocked page.
- This is a rate limit, not a CAPTCHA which the agent or user can simply click.
  No bypass, repeated request, cookie export or alternate browser was attempted.
- Parser and read-only adapter remain experimental. `/store` entry and unknown
  billing-period handling were fixed. HTTP 429 / observed 1015 are RATE_LIMITED;
  plain 403 remains HTTP_403; actual human-challenge markers retain their own code.
- The existing Edge tab/session and intent-resume controls are retained. Resume
  needs fresh normal-page evidence and an explicit operator action after access
  is restored. A denied/limited page never qualifies as normal-page evidence.
- Baseline, product configuration, cart and checkout remain UNVERIFIED.
- VMISS now uses the shared persisted catalogue budget, at least 15 minutes
  between admitted requests. One request can parse every product on its page;
  additional required catalogue pages share the same budget. Incomplete scans
  cannot initialize or replace a trusted baseline. This implementation does not
  establish a reliable live baseline while the store remains rate-limited.

## V.PS: exact order boundary

**ORDER NOW = REAL ORDER CREATION** for the observed current one-step portal.
This is a verified request semantic, not evidence that a request was sent.

The [V.PS ordering guide](https://v.ps/docs/order-a-vps/) describes an intermediate
cart. The current Amsterdam portal instead loads the official
[`onestep_cloud_2019` script](https://vps.hosting/templates/orderpages/onestep_cloud_2019/js/script.js).
Its `submitOrder()` appends `make=order` and calls native form submission.
Current normal Edge inspection found `#cartdetails` method POST, empty action
(current cart URL), `make=order`, no inline submit handler, no registered submit
listener and an unmodified native `HTMLFormElement.submit`. The Order button's
handler was `submitOrder();return false;`.

The [HostBill developer documentation](https://dev.hostbillapp.com/orderpages/)
explicitly defines this one-step submission as immediate order creation. The
[official order-page description](https://hostbillapp.com/feature/2019-onestep-orderpage/)
also describes checkout within that single flow. These combined facts establish
the boundary; the function name alone would not.

This is **L4 / PRE_ORDER_CONFIGURATION → REAL_ORDER_BOUNDARY**, the highest safe
verified stage with zero orders; it is not a failed attempt to reach L5.
The button was not clicked, intercepted or submitted. No response, invoice or
payment was generated to test this conclusion. A submit-event preventDefault
alone would not reliably block native `form.submit()`, so no such unsafe probe
was used. The adapter stays read-only. Separate cart/checkout remain UNVERIFIED.

The existing 66-plan marketing baseline remains normal inventory, including
existing Nano/Mini annual plans. Public prices and purchase links do not prove
stock. New IDs, families, locations and yearly variants are compared after that
baseline; restock requires an explicit inventory observation.

## Apple

- **Catalog discovery PASS for complete CN/iPhone scope:** current `/store` and
  purchase pages yielded 6 families and 73 exact SKUs. No future model-name table
  or inferred SKU expansion is used. Minimal iPad Pro and Mac mini samples were
  also parsed; complete other categories/regions are not live-certified.
- **Target wizard PASS:** a real `apple-configure --region cn` invocation selected
  iPhone 16 / 128 GB / black / no carrier / Nanjing East store R359 from official
  choices. The user never needed to supply SKU `MYEV3CH/A`.
- **Pickup inventory PASS:** that exact research target returned AVAILABLE and
  fresh=true from the current official `/shop/retail/pickup-message` endpoint.
- **First baseline PASS:** 73 catalogue products, zero opportunities. The research
  target was saved only in a separate local acceptance root, not installed as the
  user's formal preferences. A clearly marked DRY RUN test email was SMTP_ACCEPTED;
  inbox delivery is not verified.
- **Three-state behavior:** explicit available / sold-out observations map to
  AVAILABLE / SOLD_OUT; blocked, rate-limited, failed, stale or changed-schema
  observations stay UNKNOWN. Last trusted store history is preserved for later
  comparison, not presented as a fresh successful observation.
- First complete region/category scope is silent. A later genuinely new official
  SKU can emit NEW_SKU. A new configured target/store is not a launch or restock.
  Same-SKU/same-store SOLD_OUT→fresh AVAILABLE records RESTOCK with
  PICKUP_AVAILABLE details. Apple email includes SKU, region, store, state and
  freshness through the shared notifier.
- **Bag/Checkout BLOCKED:** the normal Edge research page showed the exact SKU,
  name, capacity, colour and price. Its own fulfillment GET returned 541;
  AppleCare choices and Add to Bag stayed disabled after selecting no trade-in.
  No button was forced enabled; no cart mutation was attempted.
- **Edge implementation PARTIAL/read-only:** the adapter recognizes the existing
  same-origin fulfillment resource's HTTP 541 metadata without exporting query
  values or creating another request, and reports APPLE_BAG_BLOCKED. Disabled
  Add to Bag reports unavailability without inventing a 541 response. Product and
  Bag page stages can be observed, but URL matching or a Bag heading never proves
  selected SKU or cart identity. There is no verified live Bag fixture; Add to Bag
  and checkout actions remain disabled. No Python protected-purchase API imitation
  or retries of the blocked page were added.
- PREORDER_OPEN, ORDER_OPEN and DELIVERY_AVAILABLE remain unverified. The current
  catalogue's `comingSoon:false` is insufficient proof of orderability. Neither
  titles nor pickup availability create those signals. Watch case/band combinations
  are not enabled as verified configurations.

## Durable request budgets

ProviderRateBudget is a small addition to private SQLite, scoped by provider,
region and endpoint. It records blocked_until, normalized retry_after,
consecutive_limits, last_success and last_failure, and atomically claims one
request. Valid delay-seconds or HTTP-date Retry-After is a minimum: longer server
waits take precedence and the local conservative floor is not shortened. Limited
or blocked routes start with a 15-minute fallback, doubling after repeated limits
up to a 24-hour fallback. After cooldown only one probe is admitted; an interrupted
probe enters cooldown and restarting preserves the wait.

Apple pickup uses a minimum 60-second interval, batches relevant SKUs per store,
rotates store groups and separates budgets by region/endpoint. 541/429, failed
requests and schema errors remain UNKNOWN and preserve trusted comparison history
without calling it fresh stock. VMISS uses its slower shared catalogue budget.
These are public HTTP request budgets, not authorization to retry Edge cart/order
actions; blocked Edge pages require normal-page evidence and explicit same-intent
resumption. No brute force, alternate browser, IP change or cookie technique is used.

## Reuse and implementation boundary

[apple-pickup-watcher](https://github.com/ENCHIGO/apple-pickup-watcher):
**GPL-3.0-or-later; code copied into MIT AutoGrab: NO; protocol/design research: YES.**
[iponkan/dmit](https://github.com/iponkan/dmit): no LICENSE found; URL, stock and
page-marker reference only; code copied: NO; its browser/challenge approach was
not adopted. See [third-party notices](../THIRD_PARTY_NOTICES.md).

The existing Core, SQLite, SMTP, protocol, Native Messaging and Bandwagon adapter
remain in use. There is no new dependency or replacement order engine. BWH/DMIT
experimental order methods require the unverified merchant no-charge contract;
DMIT cart mutations and other new provider purchase actions remain disabled.
The controller preserves precise pause codes and submission uncertainty. Budgeted
public reads admit only a cooldown probe; other latched HTTP denials stay paused.

## Verification and publication

Targeted regressions preserve observed invalid/pending DMIT configuration, VMISS
1015, unknown-period reads and Apple official catalogue/configuration semantics.
New offline checks cover L6/L7 nonce/quote identity, crash recovery and no replay,
separate smoke authority, payment email, persisted cooldown/Retry-After and Apple
UNKNOWN/blocked Bag behavior. Their results do not certify real merchant orders.
Fixture success is not real checkout evidence. Release checks are `./test.sh`,
`node --test edge-extension/tests/*.test.mjs`, `uv build`, source/history gitleaks
and independent scans of the source ZIP, wheel and source distribution.

Public artifacts exclude local config, databases, browser/profile data, research
HTML, logs, email identities and original private development history. The
original SMTP settings, earlier cart evidence and uncertain intents are retained
locally. Orders created: **0**. Payments: **0**. LIVE/ARM: **OFF**.
