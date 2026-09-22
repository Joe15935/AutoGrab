# AutoGrab Provider Closure Report — v0.4.0-alpha

2026-09-22 · **PARTIAL — experimental alpha**

| PROVIDER | LEVEL | REAL-SITE RESULT |
|---|---|---|
| Bandwagon | L5 | PASS — prior signed-in Checkout evidence retained; no repeat live test |
| DMIT | L5 manual | PASS — exact target cart, refresh and signed-in Checkout; automatic adapter still read-only |
| VMISS | L0 partial | BLOCKED — observed Cloudflare Error 1015 / rate limited; no live baseline |
| V.PS | L3 | BOUNDARY PASS — ORDER NOW = REAL ORDER CREATION; button not clicked |
| Apple | L3 | Catalog / wizard / pickup / test-email chain PASS; Bag blocked, formal target pending |

**REAL ORDERS CREATED: 0 · PAYMENTS: 0 · LIVE: OFF · ARM: OFF**

Levels: L0 discovery, L1 baseline, L2 opportunity detection, L3 normal Edge,
L4 cart, L5 checkout. Manual website acceptance and automated adapter capability
are different results. L6/L7 were neither enabled nor tested.

## DMIT

- **Baseline:** 91 public products retained. Earlier 30 selectable / 61 disabled
  is a dated snapshot, not a current guarantee.
- **Configuration:** PASS. A new, explicitly labelled ordinary-product DRY RUN
  used PID **266**, **HKG.AS3.Pro.STARTER**, **USD 79.90 / monthly**. The observed
  add link led to `cart.php?a=confproduct&i=0`.
- **Exact request/result:** the first configuration Continue POST returned 200
  with an explicit invalid-Linux-hostname response. After visibly confirming a
  valid temporary hostname through the normal UI, one corrected submission
  reached `cart.php?a=view`.
- **Cart:** PASS, same product/monthly/price. PID was bound by the observed add
  link and uninterrupted configuration-to-cart sequence; no PID field was
  exposed in the cart itself.
- **Cart survives refresh:** PASS, same product and USD 79.90 monthly summary.
- **Checkout:** PASS at `cart.php?a=checkout`, signed-in controls, personal-info
  and payment sections, total USD 79.90, unchecked terms and **Complete Order**.
  Stopped there. No final submit or payment.
- **Blocker:** none for this manual L5 acceptance. Automated product/cart identity
  and mutation are still disabled. The read-only adapter now distinguishes
  configuration, pending request, validation failure, cart and checkout stages;
  page-stage recognition alone never certifies a cart.
- **Old uncertainty:** the previous PID 265 intent remains locked/uncertain.
  Its two old XHRs had 403 responses; their individual operations and exact cause
  are unknown. It was never retried. The official Continue callback lacks an
  error branch which would restore its spinner after such a failure.

## VMISS

- **Official store:** [app.vmiss.com/store](https://app.vmiss.com/store), confirmed
  from the official website.
- **Challenge:** current task tab displayed **Error 1015 / You are being rate
  limited**, a temporary site-owner denial. This is not a clickable CAPTCHA.
- **Baseline:** NOT ESTABLISHED. **Cart / Checkout:** UNVERIFIED.
- **Blocker:** site access must recover before live validation. Same ordinary
  Edge session/tab retained; no repeated request, bypass, cookie export or
  alternative authenticated browser.
- **Offline completion:** current store entry, provider-scoped identity,
  conservative unknown-period reads, parser/adapter, trusted structural fixture,
  baseline persistence and resume guards are in place. Plain 403 remains HTTP_403;
  explicit 1015 or HTTP 429 becomes RATE_LIMITED. Neither qualifies as a normal
  page for automatic recovery.
- **Live validation:** USER ACTION REQUIRED after access recovers. If the site
  then requests login/human verification, complete it in that same Edge session,
  then explicitly resume the existing intent. No live readiness claim yet.

## V.PS

- **Baseline:** 66 ordinary public plan IDs retained. Existing Nano/Mini yearly
  offers are not new launches. Later IDs, location/plan combinations, families
  and yearly variants can be compared; public purchase links alone do not prove
  stock or restock.
- **Order Now semantic:** **ORDER NOW = REAL ORDER CREATION** on the observed
  current Amsterdam one-step portal.
- **Evidence:** normal Edge showed `#cartdetails`, POST to the current cart URL,
  hidden `make=order`, no form submit listener and native `form.submit()` intact.
  The current official script's `submitOrder()` appends `make=order` and invokes
  that native submit. [HostBill developer docs](https://dev.hostbillapp.com/orderpages/)
  explicitly define that one-step POST as immediate order creation; its
  [official feature description](https://hostbillapp.com/feature/2019-onestep-orderpage/)
  agrees. This resolves the mismatch with the older/general
  [V.PS guide](https://v.ps/docs/order-a-vps/).
- **Cart / Checkout:** separate stages UNVERIFIED.
- **Blocker:** the current boundary would create an order. Button not clicked;
  no request, invoice or payment generated. A submit-event `preventDefault`
  would not reliably stop native `form.submit()`, so no unsafe interception probe
  was attempted. Automatic mutation remains off.

## Apple

- **Catalog discovery:** PASS for complete **CN / iPhone** scope: official live
  purchase pages yielded **6 families / 73 exact SKUs**, plus 49 current CN store
  choices. No hardcoded future iPhone name, date or inferred SKU list. iPad Pro
  and Mac mini samples parsed; other complete categories/regions unverified.
- **Target configure:** PASS through the real `apple-configure --region cn`
  wizard: category → model → capacity → colour → carrier where applicable →
  store. Research target: iPhone 16, 128 GB, black, **MYEV3CH/A**, store **R359**.
  The user need not type a SKU.
- **Formal monitoring target:** NOT CONFIGURED. The sample lives only in a
  separate local acceptance root; user region/model/store preferences remain
  pending. Monitoring code is available, but no ongoing user monitor was started.
- **Pickup inventory:** PASS; exact research SKU/store returned AVAILABLE and
  fresh=true from the official `/shop/retail/pickup-message` endpoint.
- **Three-state inventory:** explicit available/sold-out map to AVAILABLE and
  SOLD_OUT. Denied, limited, failed, stale or changed-schema results stay UNKNOWN;
  they do not invent sold-out transitions.
- **Events / email:** first full scope baseline created 73 products and **zero
  opportunities**. Later new official SKUs can emit NEW_SKU; fresh same-store
  sold-out→available emits RESTOCK with PICKUP_AVAILABLE details. Adding a target
  or store is not a launch. A separate clearly labelled **DRY RUN** email was
  SMTP_ACCEPTED through shared SMTP; inbox receipt is unverified. No ordinary
  test SKU was advertised as a real opportunity.
- **PREORDER_OPEN / ORDER_OPEN / DELIVERY_AVAILABLE:** UNKNOWN / unverified.
  `comingSoon:false` and pickup availability alone are insufficient evidence.
- **Bag:** BLOCKED. Normal Edge showed the exact product/colour/capacity/price.
  After no-trade-in selection, Apple's fulfillment request returned HTTP 541;
  AppleCare and Add to Bag remained disabled. No forced enabling or Bag click.
- **Checkout:** UNVERIFIED, since Bag did not pass.
- **Blocker:** current website fulfillment failure and formal target selection.
  Watch case/band combinations remain experimental.

## Open-source reuse

| Reference | License / reuse decision |
|---|---|
| [ENCHIGO/apple-pickup-watcher](https://github.com/ENCHIGO/apple-pickup-watcher) | GPL-3.0-or-later; code copied into MIT AutoGrab: **NO**; protocol/design research: **YES**; independently implemented against current official Apple data |
| [iponkan/dmit](https://github.com/iponkan/dmit) | No LICENSE found during bounded research; PID URL, stock and page-marker reference only; copied code: **NO**; no Playwright/Cloudflare scheme adopted |
| Official Apple, DMIT and V.PS/HostBill pages/scripts | Current page/request semantics only; no merchant account data or full source snapshots packaged |

AutoGrab remains **MIT**. Existing Core, SQLite, shared SMTP, Native Messaging,
Edge Companion and Bandwagon adapter are retained. No production dependency,
provider, protocol redesign, database redesign or NodeSeek expansion was added.

## Changes and validation

Changed areas: Apple catalogue discovery and target wizard; narrow Apple config,
baseline/event/email wiring; DMIT read-only page diagnostics; VMISS/V.PS denial
and unknown-period handling; controller pause handling; release metadata/docs.
Regression fixtures contain only the public structure needed for observed bugs.

Python: `./test.sh` — **570 passed**. Extension:
`node --test edge-extension/tests/*.test.mjs` — **61 passed**.
`git diff --check` — **PASS**. Independent candidate-source privacy review —
**PASS**; exact extension public-key matches were verified as nonsecrets.
Final `uv build`, working-tree/history gitleaks and independent source ZIP /
wheel / sdist scan results are recorded in the published release notes.
Fixture/build success does not upgrade any real-site result above.

Public packages exclude SMTP configuration/secrets, cookies, Edge profiles,
private SQLite databases, account details, personal paths and private development
history. Local database integrity, original 48/91/66 baselines, old uncertain
intent and unchanged SMTP settings were checked. Final order/invoice/submit
references remain zero.
