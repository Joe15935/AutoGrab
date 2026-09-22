# AutoGrab

Multi-provider stock monitor and checkout assistant for limited VPS plans and product launches.

[简体中文](README.zh-CN.md) · [Security](SECURITY.md) · [Research and verification](docs/MULTI_PROVIDER_STATUS.md) · [v0.4 closure report](docs/AUTOGRAB_PROVIDER_CLOSURE_REPORT.md) · [v0.5 Core report](docs/AUTOGRAB_V0_5_PAYMENT_READY_CORE_REPORT.md)

**Experimental alpha. DRY_RUN by default. LIVE OFF. ARM OFF. REAL_ORDER_SMOKE_TEST_ARMED=false. No real orders or payments created.**

AutoGrab keeps a catalogue baseline, detects subsequent product and stock changes, sends email, and uses a companion extension in your ordinary Microsoft Edge profile. A first scan never treats the whole catalogue as new stock. `SPECIAL`, `PROMO`, and annual billing are signals, not purchase triggers. Prices are recorded, never used as budget or value filters.

## Actual support

| Provider | Discovery / stock | Edge checkout assistance |
|---|---|---|
| BandwagonHost — L5 | Official catalogue; existing baseline retained; new-product/restock/group detection | Product → configuration → cart → signed-in checkout verified; stops before final order |
| DMIT — L5 manual flow | 91-product baseline; public catalogue verified | PID 266, USD 79.90 monthly: exact cart survived refresh and signed-in checkout reached; stopped at Complete Order; companion remains read-only |
| VMISS — L0 partial | Official store identified; no live product baseline | Observed Cloudflare Error 1015 (rate limit), not a CAPTCHA; same-session read-only handoff; cart/checkout unverified |
| V.PS — L4 / pre-order boundary | 66 public plan IDs; marketing purchase links do not prove stock | Current one-step Order button is a verified real-order boundary and was not clicked; separate cart/checkout unverified |
| Apple Store — inventory ready; purchase flow experimental | Dynamic official catalogue and target wizard; CN research target pickup verified | Ordinary Edge detects fulfillment HTTP 541/disabled controls and observes product/bag pages read-only; Add to Bag, exact bag contents and checkout unverified |

A provider being present in the registry does **not** mean its checkout is ready. Unknown stock remains unknown. No claim of a reserved item, an order, or an invoice is made from a visible cart.

The v0.5 shared L6/L7 lifecycle and BWH/DMIT order adapters are **IMPLEMENTED — experimental**; **REAL-SITE VERIFIED — NO**. Their no-charge submission contract is demonstrated only by offline fixtures, not by either merchant's current checkout. Real submission therefore remains blocked. Prior BWH/DMIT L5 evidence is retained without another live cart run. V.PS L4 records a pre-order boundary, not a separate cart or an order.

## Quick start

Requirements: Python 3.12–3.14, [uv](https://docs.astral.sh/uv/), and Node.js for extension tests. The native companion installer currently supports **macOS + Microsoft Edge**. Monitoring itself uses standard Python HTTP and SQLite. Existing Playwright is retained for offline DOM tests and historical regression helpers; it is not used to log in to merchants.

```sh
uv sync --locked --python 3.12
cp config/config.example.toml config/config.toml
./start.sh baseline --provider all
./start.sh status
./start.sh monitor --once --provider all
```

Use `--provider bandwagon`, `dmit`, `vmiss`, `vps`, or `apple` to inspect one provider. All commands share the existing database. HTTP blocking pauses the affected provider/route. Budgeted VMISS and Apple public reads permit one probe after their persisted cooldown; other latched HTTP denials need an explicit restart after access is restored. An Edge intent is resumed in its existing tab/session only after fresh normal-page evidence.

Runtime configuration is **TOML**. [config.example.yaml](config.example.yaml) is a readable configuration outline, not an alternative parser. Configure Apple using current official choices; no SKU entry is required:

```sh
./start.sh apple-configure
./start.sh apple-catalog-refresh
./start.sh monitor --once --provider apple
```

The wizard selects region → official category → model → capacity → color → carrier (where applicable) → store(s), then writes a private `config/apple.local.toml` overlay. Existing Apple settings are backed up; the main config and SMTP settings remain intact. A research target is not installed automatically. The first complete scan of each region/category is silent; later newly observed official SKUs can emit `NEW_SKU`. Configuring an existing SKU is not a launch. Known sold-out → fresh available store inventory produces a `RESTOCK` event with `PICKUP_AVAILABLE` details. Errors and stale observations remain `UNKNOWN`. Preorder, order-open and delivery states remain unverified until a supported official source proves them. Apple Watch combinations remain experimental.

## Normal Edge companion

```sh
./start.sh edge-install
./start.sh edge-status
./start.sh edge-dry-run --provider bandwagon --product-id YOUR_BASELINED_PID
```

In your ordinary Edge profile, open `edge://extensions`, enable Developer mode, and load the project's `edge-extension` folder. Open AutoGrab's popup and connect. The stable extension ID is `eddoiocaihhammnclkhmmnafjhjilfnc`, derived from a **public** manifest key. The native host uses stdio, not a local TCP port or CDP.

Explicit dry runs are labelled tests, not invented stock opportunities. Experimental providers open a read-only official page. Their mutation adapters remain disabled until separately verified. `monitor --prepare-checkout` can queue supported, freshly rechecked opportunities to Edge and still stops before final order. Without this flag, monitoring records changes and sends configured email only.

On login or challenge, use the ordinary site UI yourself, then resume the **same** intent:

```sh
./start.sh edge-resume --intent-id YOUR_EXISTING_INTENT_ID
./start.sh edge-cancel --intent-id YOUR_EXISTING_INTENT_ID
./start.sh stop-all
```

Cancellation stops local work; it does not clear a merchant cart. An uncertain dispatched action is never blindly repeated. Product identities include the provider, so equal numeric PIDs from different merchants cannot share an intent. Local database migrations preserve existing baselines, intents and safety records.

## Experimental order and payment-ready Core

The existing PurchaseRunner and PurchaseIntent ledger now share order precheck, persisted submission nonce, uncertain-result reconciliation, order/invoice lookup and payment-ready notification. A fresh signed-in page, exact product/price/billing and independent no-charge evidence are prerequisites. `Complete Order` is not inherently safe: account credit or a saved payment method can cause funds to move. Current merchant pages have not met this no-charge contract.

`provider-capabilities` and `order-status` report implementation and evidence separately. `order-precheck` only inspects an existing, explicitly identified Edge task tab. The separate `order-smoke` entry point requires explicit LIVE selection and `REAL_ORDER_SMOKE_TEST_ARMED`, a fresh precheck, SMTP evidence and a one-intent lease of at most 60 seconds. Normal monitoring ARM grants none of this authority; even explicit arming cannot replace the missing merchant proof. No real smoke test was run.

The nonce is committed before dispatch. A timeout, disconnect or crash enters `ORDER_UNCERTAIN`; recovery only searches for the same order. Even confirmed absence does not automatically retry. `PAYMENT_READY` requires matching order/invoice IDs, an unpaid official invoice page, exact product and amount. A cart or checkout URL does not qualify. Notification is claimed once before sending; an ambiguous SMTP failure is not retried automatically.

## Request budgets

`ProviderRateBudget` stores provider/region/endpoint cooldown, consecutive limits and last outcomes in private SQLite. Valid `Retry-After` delay or HTTP-date is a minimum: a longer server delay takes precedence, and the conservative local floor is never shortened. Limited/blocked routes start with a 15-minute fallback, double on repeated limits up to a 24-hour fallback, and admit one probe after cooldown. Restarting does not erase the budget; an interrupted probe also waits.

VMISS public reads have a minimum 15-minute interval and share one catalogue request slot, not one request per product. Incomplete catalogue scans cannot establish a baseline. Apple pickup reads have a minimum 60-second interval, batch configured SKUs for one store and rotate store groups; budgets are scoped by region and endpoint. HTTP 541/429, network and schema failures produce UNKNOWN. These limits do not authorize refreshing blocked Edge purchase pages or retrying Add to Bag.

## Email and auxiliary signals

`./start.sh configure-email` configures TLS SMTP. Secrets go to the designated macOS Keychain item or temporary environment variables, never a tracked config. `./start.sh email-test` tests SMTP acceptance; it is not proof of inbox delivery. Public examples use `you@example.com`.

A future verified L7 notification uses **🚨🚨 [PAY NOW] AutoGrab Payment Ready** with provider, product, exact price/billing, order/invoice IDs, detection/order/payment-ready times and elapsed time. Its primary link is an official merchant HTTPS invoice URL usable from a phone, with any login requirement stated. DRY RUN and fixture messages remain labelled as tests; no real PAYMENT_READY email has been demonstrated.

NodeSeek support is an optional read-only RSS signal extractor (`autograb.intelligence`). Forum claims and keywords never create purchase events. Its official URL candidates require a fresh merchant check through the normal discovery/baseline pipeline. Automatic RSS-to-monitor scheduling is not enabled in this alpha.

## Safety boundaries

AutoGrab does not bypass CAPTCHA, Cloudflare challenges, waiting rooms, 2FA, or merchant security controls. It does not clone cookies, export profiles, rotate proxies, spoof fingerprints, or use stealth login automation.

**AutoGrab never performs final payment automatically.** Final order creation is experimental, separately gated and blocked on current merchant evidence. Do not enable legacy helpers, inject test contract markers or alter payment/credit settings to bypass these checks.

## Development

```sh
uv run --locked python -m playwright install chromium
./test.sh
node --test edge-extension/tests/*.test.mjs
uv build
```

Fixture tests validate code contracts, not current live stock or checkout readiness. Live observations and remaining limitations are listed in [the provider status document](docs/MULTI_PROVIDER_STATUS.md). No new production dependency was added for the multi-provider expansion.

MIT licensed. [Third-party research and notices](THIRD_PARTY_NOTICES.md). Public releases exclude local databases, private logs, account configuration, profiles and original private development history.
