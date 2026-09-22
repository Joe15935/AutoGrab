# PROVIDER CAPABILITIES

Evidence date: 2026-09-22. Release target: **v0.5.0-alpha**.

| Provider | IMPLEMENTED | REAL-SITE VERIFIED |
|---|---|---|
| BandwagonHost | Shared L6/L7 Core and experimental order/receipt contract | **L5** retained. L6/L7 **NO**; current no-charge contract unverified |
| DMIT | Same shared Core; thin fixed order/receipt contract | **L5 manual** retained, PID 266 / USD 79.90 monthly. L6/L7 **NO** |
| VMISS | Discovery reader, normal Edge diagnostics, durable rate budget | **L0 partial / RATE_LIMITED**. Prior Error 1015; no reliable live baseline |
| V.PS | **L4** pre-order configuration / REAL_ORDER_BOUNDARY | **L4**. Order Now creates an order; correctly not clicked |
| Apple | Official catalogue, generic target wizard, pickup inventory, scoped cooldown, read-only normal Edge Bag diagnostics | Inventory verified previously. **Bag BLOCKED by 541**, checkout unverified; formal purchase target not selected |

Bandwagon / DMIT implemented level: L7 experimental contracts. Verified level:
L5 only. Their real merchant order adapters remain **PARTIAL** because no-charge,
order and invoice selectors have not been validated on actual orders. The
experimental fixtures do not establish usable live selectors. No fixture markers
may be injected into real pages to manufacture verification.

VMISS rate-limit state: prior normal Edge 1015 observation retained; no live
probe this sprint. V.PS real-order boundary: verified from the ordinary page,
official frontend and HostBill contract. Apple rate-limit state: prior ordinary
Edge fulfillment HTTP 541 retained; no new Bag retry. Public pickup and protected
fulfillment are separate routes; a pickup signal never proves Bag availability.

## Acceptance

| Item | Result | Evidence limit |
|---|---|---|
| L6/L7 Core | **IMPLEMENTED — experimental** | Shared ledger, guards, fixed Edge commands and invoice evidence; live adapters PARTIAL |
| Idempotency | **PASS** | Offline concurrent claims, persistent nonce, cancellation/crash/reconnect tests |
| Reconciliation | **PASS, offline** | Matching identity/quote/time; missing scope is UNKNOWN; actual account/receipt layout unverified |
| Payment-ready email | **PASS, offline** | Exact subject and official unpaid invoice evidence; no real L7 email sent |
| VMISS Retry-After | **PASS, offline** | Seconds/date, restart, one probe, increasing cooldown, competing processes |
| Apple 541 cooldown | **PASS, offline** | Region/endpoint isolation, blocked/error/schema responses remain UNKNOWN |
| Apple real Edge Bag | **BLOCKED** | Existing ordinary Edge 541 evidence; Add to Bag not executed |
| Orders | **0** | No order ID, invoice ID or submission marker in the existing local ledger |
| Payments | **0** | No payment operation or real smoke test performed |
| LIVE / ARM | **OFF / OFF** | Local companion status; no foreground smoke authority issued |
| REAL_ORDER_SMOKE_TEST_ARMED | **false** | Separate explicit opt-in; public defaults remain off |

## Scope and reuse decision

This sprint extends the existing PurchaseIntent, SQLite, PurchaseRunner, Native
Messaging, normal Edge, email, ARM and kill switch. There is one lifecycle for
BWH and DMIT, not a separate engine per merchant. No provider, production
dependency, UI framework, service or NodeSeek feature was added.

| Candidate | Maintenance / reuse | Deployment and resources | Decision |
|---|---|---|---|
| Existing AutoGrab v0.4 | Current public baseline, reusable ledger/Edge/email/guards | Existing Python + normal Edge; no new daemon/service | Extend with fixed adapters and a small budget table |
| WHMCS official standard cart | Reviewed upstream checkout template, recent commit; credit/card behavior reference | Cannot replace AutoGrab or prove merchant-specific safety | Read-only reference; no code copied |
| apple-pickup-watcher | Previously reviewed active GPL watcher; public pickup design reference | Separate watcher adds overlapping runtime | Preserve independent public-endpoint implementation; no GPL code copied |
| DMIT monitor reference | Public monitor reviewed; no license located | Does not provide this project's durable order lifecycle | Research only; no source copied |

The minimum delivery is the shared one-shot order protocol, honest capability
matrix, durable request admission and normal Edge block diagnostics. Current
merchant limits determine the live acceptance boundary; no throughput or resource
benchmark is claimed. Research and provenance are in [THIRD_PARTY_NOTICES](../THIRD_PARTY_NOTICES.md).

## Order lifecycle and authorization

`ORDER_PRECHECK → ORDER_SUBMITTING → ORDER_UNCERTAIN → RECONCILING`
feeds the existing order/invoice ledger. An observed receipt leads to
`ORDER_CREATED → INVOICE_SEARCHING → INVOICE_CREATED → PAYMENT_LINK_SEARCHING → PAYMENT_READY → WAITING_FOR_USER`.
Explicit merchant terminal evidence alone may close an order as expired/cancelled.
Local cancellation does not mean merchant cancellation.

Before any submission, SQLite atomically binds the provider/product/opportunity,
unique nonce, exact quote, precheck ID and timestamp. Only one active intent for
a provider/product and one submission per intent are admitted. The native host
rereads these fields, the current primary state, permit expiry, kill switch and
nonce-specific kernel lock held by the foreground authorizer. Process death
releases that lock. A leftover lock file cannot authorize dispatch.

The extension binds the connection/tab/document and persists its own consumed
nonce before one normal submit click. It checks session, visible product/amount,
form ownership, payment controls, expiry and explicit no-charge evidence again
immediately before that click. Hidden duplicate payment fields, cross-form
buttons and a product changed during asynchronous inspection are rejected.

`order-smoke` requires both explicit LIVE mode and the independent armed flag,
a valid unsubmitted REAL intent, positive precheck, fresh SMTP proof and a permit
lasting at most 60 seconds. Normal monitoring ARM, a config file, reconnect or
historical simulated intent cannot grant that permission. Current real checkouts
lack the verified no-charge contract, so they fail closed even with the flag.
The usual checkout assistance remains DRY_RUN. This sprint did not arm a smoke test.

Cancellation/timeout stops the local queue. A possibly dispatched order is never
blindly submitted again. Reconciliation is read-only and bound to the stored
nonce/time, exact product/price and immutable known IDs. A visible observed
invoice link permits one official invoice GET; a subsequent explicit
`order-reconcile` inspects that page. No account crawler or guessed invoice URL
is implemented. Missing selectors, authentication or search scope yield UNKNOWN
or a human/login pause. Strong NO_ORDER_FOUND requires complete scope and time
proof, and still does not authorize automatic retry.

## Payment boundary and email

PAYMENT_READY needs a matching order and invoice, the current official HTTPS
invoice page, visible exact product/amount and explicit unpaid status. Checkout,
cart, URL shape and an order ID alone do not qualify. The final payment button is
observed but never clicked. Account credit, saved-card authorization and PayPal
approval are payment operations and remain forbidden.

The future subject is **🚨🚨 [PAY NOW] AutoGrab Payment Ready**. It includes provider,
product, price, billing, IDs, detection/order/payment-ready times and elapsed
measurement. Observation times are labelled; missing evidence stays UNKNOWN,
and wall-clock estimates are distinguished from monotonic measurements. Its
primary URL is the official merchant invoice usable from a phone; login may be
required. Notification is claimed durably before SMTP. A crash or uncertain
acceptance does not resend automatically. Inbox delivery is not established by
SMTP acceptance. Test messages remain distinct.

## Rate limits and Apple

ProviderRateBudget is an additive private SQLite table keyed by provider, region
and endpoint. It records blocked_until, normalized Retry-After, consecutive
limits, last success/failure, next allowed time and an expiring request claim.
It stores no raw headers, URLs, account data or cookies. Valid server delays are
minimum waits. Local limited-route fallback begins at 15 minutes and doubles to
a 24-hour fallback; longer server delays win. Exactly one process can claim the
post-cooldown probe. A crashed request waits again; late results cannot clear a
newer block. Normal Edge limit observations share this persistence with HTTP
monitoring; Apple fulfillment and pickup have separate route scopes.

VMISS uses one catalogue request slot per at least 15 minutes. It accumulates a
complete catalogue across slots when multiple pages are required; partial scans
never initialize or replace the baseline. Scratch accumulation is in memory,
so a restart may lengthen completion while preserving the request budget.

Apple pickup keeps the existing public `/shop/retail/pickup-message` route,
batches configured SKUs for a store and rotates store groups, with a minimum
60-second request interval. Catalogue/store reads have their own scope and
budget. A 541/429/network/schema failure is UNKNOWN, never sold out. No bypass,
proxy/IP rotation, cookie cloning, browser replacement or private purchase API
reconstruction was added.

Apple normal Edge work is **PARTIAL/read-only**: existing response metadata can
identify fulfillment 541, disabled Add to Bag controls stop the intent, and a Bag
heading is only a page observation. Exact SKU selection, native Add to Bag,
verified Bag contents and Checkout are not yet enabled. The previous ordinary
Edge blocker was not retried. The wizard uses current official catalogue choices;
pickup configuration is supported, delivery/preorder/order-open remain unverified.
No formal user purchase target or ongoing monitor was selected automatically.

## Validation and release

Validation is recorded against the final public source. Tests exercise temporary
databases, mocked HTTP/SMTP and intercepted offline DOM fixtures; none creates a
merchant order. Frozen BWH/DMIT L5 evidence was not rerun. Real ordinary Edge
loaded extension 0.5.0 and native heartbeat confirmed connected / LIVE OFF /
disarmed. Existing 48 BWH, 91 DMIT and 66 V.PS product records, prior purchase
fields and private SMTP settings were preserved; database integrity and foreign
keys passed. No private database, settings, browser profile or account page is
part of the release.

Final test/build results are recorded below for the release tree. Source ZIP, wheel, sdist, public Git ancestry and commit metadata must
all pass privacy review before upload. Private development branches/history are
excluded. A reviewed public extension RSA key is not a credential.

Final commands and observed results:

- `./test.sh`: **615 PASS**, including all retained regression modules.
- `node --test edge-extension/tests/*.test.mjs`: **65 PASS**.
- `git diff --check`: **PASS**.
- `uv build`: **PASS**, Python wheel and source distribution.
- `./start.sh edge-status`: **0.5.0, connected, LIVE OFF, disarmed** after native reconnect.
- `./start.sh provider-capabilities` and `./start.sh order-status`: **PASS**,
  experimental/verified capabilities separated and independent smoke flag false.
- Initial full run found two outdated assertions: migration expected no added
  columns, and human-pause email assumed every context was DRY RUN. Assertions
  now check preservation of every old field plus null new fields, and accurate
  uncertain-order/no-payment wording. Both passed the final full run.
- Final source and unpacked wheel/sdist/source ZIP: secret scan and privacy review
  required before publication; final scan evidence accompanies the release output.
- Public Git ancestry and author/committer metadata: reviewed separately from
  private development refs. Only public `main` and `v0.5.0-alpha` may be pushed.

Changes by area (all paths repository-relative):

| Area | Files |
|---|---|
| Shared lifecycle / guards | `autograb/core/purchase.py`, `purchase_state.py`, `live.py`, `lock.py`, new `capabilities.py` |
| Existing ledger | `autograb/storage/intents.py` |
| Fixed Edge protocol / queue | `autograb/edge/protocol.py`, `broker.py`, new `order_protocol.py` |
| Thin adapter / CLI | new `autograb/providers/edge_order.py`, `autograb/order_cli.py`; `autograb/main.py` |
| Shared rate budget | new `autograb/core/rate_budget.py`; `providers/vmiss.py`, `apple.py`, `apple_catalog.py`, `registry.py`; `multi_cli.py`, `apple_cli.py` |
| Email | `autograb/notifications/email.py` |
| Companion | new `edge-extension/order-flow.js`, `order-adapter.js`; protocol/content/worker/manifest; Apple/V.PS readers; existing popup wording |
| Focused tests | order bridge/CLI/Core/DOM; rate budget; Apple DOM; payment email; migration and native notice expectations; explicit experimental fixtures |
| Release / docs | READMEs, AGENTS, SECURITY, provider status, protocol contract, fixture provenance, third-party notices, this report, example config, package/extension versions and lockfile |

The unchanged BWH product/config/cart adapter preserves the prior L5 flow.
No repeat live cart test was needed. This release is an experimental **alpha**;
real L6/L7 and Apple Bag/Checkout acceptance remain pending their specific gates.
