# Multi-provider alpha: evidence and limitations

Observed 2026-09-22. **Integration PARTIAL; no claim that all five checkouts are ready.**
All real operations stopped before final order submission. LIVE OFF, ARM OFF.

| Provider | Discovery | Baseline | Opportunity classification | Normal Edge | Cart | Checkout |
|---|---|---|---|---|---|---|
| BandwagonHost | PASS, official catalogue | PASS, 48 existing products | PASS, regression-verified transitions | PASS, prior same-profile companion flow | PASS, prior real flow | PASS, prior signed-in real flow |
| DMIT | PASS in ordinary Edge; HTTP challenge | PASS, 91 public PID records | PASS in regression; automatic HTTP monitoring blocked | Real product/configuration reached; companion read-only | UNVERIFIED, configuration Continue outcome uncertain | UNVERIFIED |
| VMISS | BLOCKED, official store security verification | NOT ESTABLISHED | Parser/classifier tests only | USER ACTION REQUIRED | UNVERIFIED | UNVERIFIED |
| V.PS | PASS, six official marketing categories | PASS, 66 unique plans | PASS in regression; marketing stock UNKNOWN | PASS: shared companion read-only handoff; real HostBill plan UI observed | UNVERIFIED | UNVERIFIED |
| Apple | PASS for official configured pickup research sample | User targets NOT CONFIGURED | Pickup transition regression PASS | Experimental read-only routing | UNVERIFIED | UNVERIFIED |

“Opportunity PASS” above is code-transition validation, not evidence that a new
commercial opportunity occurred during this run. Baseline initialization sent
no opportunity or purchase trigger. The Bandwagon catalogue's current 48 items
remain ordinary baseline inventory despite promotional words or annual billing.

## Actual site differences

- Bandwagon uses its existing custom catalogue and classic WHMCS cart. Its prior
  observed Product → Configuration → Cart → signed-in Checkout path is retained.
  No additional ordinary-product purchase was made in this sprint.
- DMIT `/cart.php` uses `dmit_cart_2020`, with `.cart-products-item[gid]`,
  `.cart-products-box[pid]`, `.cart-products-title`, `.cart-products-price`, and
  `.none-stock`. The normal page contains 91 products across filtered regions;
  61 had explicit disabled/out-of-stock markers and 30 were selectable. A first
  scan recorded all 91. Official public JavaScript confirms that `.none-stock`
  excludes selection and only a selected priced product enables Continue.
  A test item reached `cart.php?a=confproduct&i=0`; `frmConfigureProduct` submits
  through the merchant's AJAX configuration handler. Continue was clicked once
  and remained pending. A fresh read-only Cart page then explicitly showed an
  empty basket. Its uncertain configuration request is preserved, never retried.
- VMISS's current official website links `app.vmiss.com/store`. Anonymous HTTP
  received 403; ordinary Edge also encountered security verification. The
  Lagom-style fixture remains explicitly synthetic pending current live evidence.
- V.PS links to `vps.hosting`, whose routes are HostBill-style `cmd=cart`,
  `/products/` and `/cart/<category>/`, not `cart.php`. Six marketing categories
  yielded 66 IDs. Ordinary Edge showed all six Tokyo cloud plans out of stock;
  Amsterdam displayed configurable plans and a billing selector. The observed
  Order button calls `submitOrder()`; the linked official `onestep_cloud_2019`
  script appends `make=order` and submits `#cartdetails`. That is an order boundary,
  not a verified Add to Cart action, so it was deliberately not clicked. These limited
  observations do not overwrite the complete marketing baseline's UNKNOWN stock.
- Apple CN's current public `shop/retail/pickup-message` endpoint returned an
  exact SKU/store match with `pickupDisplay=available`. The research SKU and store
  were first obtained from official Apple pages. They were not installed as a
  user target. A delivery endpoint request returned 541; that route was stopped.
  No future product, release date or preorder opening was inferred.
  Adding an existing SKU to local configuration is recorded as a monitored
  target, not a new launch. Dynamic Apple SKU discovery and preorder/order-open
  detection are not implemented in this alpha.

## Scope of the alpha

The five providers share Core, SQLite, durable events, email and Native Messaging.
The installed ordinary-Edge extension and Core both reported version 0.3.0.
A real V.PS OPEN_PRODUCT command passed through Native Messaging, opened its
public Cloud page, and returned OPENED without a cart action. After an explicit
local cancellation, Core received the cancellation acknowledgement and safely
released only this proven read-only intent. All older cart/uncertainty records
were retained.
Provider+product identity prevents cross-merchant collisions. Explicit Edge tests
are not restock opportunities. Unknown prices are preserved and allow read-only
page handoff, not guessed amounts. Only the previously verified Bandwagon adapter
has enabled DOM mutations; all new adapters currently reject them in both the
content script and controller. This is a material remaining implementation limit.

SQLite migration retains old rows, extra columns, indexes, triggers, views and
foreign-key links. The original local data and SMTP setup are private and are
not part of an installation or public release. Public releases start with an
empty user-owned database.

NodeSeek is optional auxiliary intelligence: public RSS → unverified signal →
allowlisted official URL candidate. It cannot create an event or order directly.
Automatic RSS-to-provider scheduling is not yet enabled.

## Targeted reuse decision

The maintained Apple watcher helps identify the current endpoint, but adding its
Rust/Tauri application would duplicate the existing runtime. Standalone WHMCS
monitors supply URL/stock behavior but not the verified normal-Edge lifecycle.
The selected route is independent thin provider adapters, preserving upstream
interfaces and existing Core. No production dependency was added. See
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) for licenses and decisions.

## Acceptance commands

```sh
./test.sh
node --test edge-extension/tests/*.test.mjs
uv build
gitleaks dir --config .gitleaks.toml --redact
gitleaks git --config .gitleaks.toml --redact
```

Run source and history scans on the exact public staging repository, not the
private runtime checkout. Check commit author metadata separately; secret
scanners are not personal-data scanners. Only the exact public extension key is
allowlisted. Archive/wheel contents also require review.
