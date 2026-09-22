# AutoGrab

Multi-provider stock monitor and checkout assistant for limited VPS plans and product launches.

[简体中文](README.zh-CN.md) · [Security](SECURITY.md) · [Research and verification](docs/MULTI_PROVIDER_STATUS.md) · [v0.4 closure report](docs/AUTOGRAB_PROVIDER_CLOSURE_REPORT.md)

**Experimental alpha. DRY_RUN by default. LIVE OFF. ARM OFF. No final order submission or automatic payment.**

AutoGrab keeps a catalogue baseline, detects subsequent product and stock changes, sends email, and uses a companion extension in your ordinary Microsoft Edge profile. A first scan never treats the whole catalogue as new stock. `SPECIAL`, `PROMO`, and annual billing are signals, not purchase triggers. Prices are recorded, never used as budget or value filters.

## Actual support

| Provider | Discovery / stock | Edge checkout assistance |
|---|---|---|
| BandwagonHost — L5 | Official catalogue; existing baseline retained; new-product/restock/group detection | Product → configuration → cart → signed-in checkout verified; stops before final order |
| DMIT — L5 manual flow | 91-product baseline; public catalogue verified | PID 266, USD 79.90 monthly: exact cart survived refresh and signed-in checkout reached; stopped at Complete Order; companion remains read-only |
| VMISS — L0 partial | Official store identified; no live product baseline | Observed Cloudflare Error 1015 (rate limit), not a CAPTCHA; same-session read-only handoff; cart/checkout unverified |
| V.PS — L3 | 66 public plan IDs; marketing purchase links do not prove stock | Current one-step Order button creates a real order; submission stays disabled; separate cart/checkout unverified |
| Apple Store — L3 | Dynamic official catalogue and target wizard; CN research target pickup verified | Bag blocked by the current page's fulfillment HTTP 541 and disabled controls; delivery/preorder/order-open unverified |

A provider being present in the registry does **not** mean its checkout is ready. Unknown stock remains unknown. No claim of a reserved item, an order, or an invoice is made from a visible cart.

## Quick start

Requirements: Python 3.12–3.14, [uv](https://docs.astral.sh/uv/), and Node.js for extension tests. The native companion installer currently supports **macOS + Microsoft Edge**. Monitoring itself uses standard Python HTTP and SQLite. Existing Playwright is retained for offline DOM tests and historical regression helpers; it is not used to log in to merchants.

```sh
uv sync --locked --python 3.12
cp config/config.example.toml config/config.toml
./start.sh baseline --provider all
./start.sh status
./start.sh monitor --once --provider all
```

Use `--provider bandwagon`, `dmit`, `vmiss`, `vps`, or `apple` to inspect one provider. All commands share the existing database. HTTP blocking pauses that provider rather than launching a bypass. A paused process must be explicitly restarted after access is restored; HTTP denial and rate limits do not trigger automatic request retries. An Edge intent is resumed in its existing tab/session only after fresh normal-page evidence.

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

## Email and auxiliary signals

`./start.sh configure-email` configures TLS SMTP. Secrets go to the designated macOS Keychain item or temporary environment variables, never a tracked config. `./start.sh email-test` tests SMTP acceptance; it is not proof of inbox delivery. Public examples use `you@example.com`.

NodeSeek support is an optional read-only RSS signal extractor (`autograb.intelligence`). Forum claims and keywords never create purchase events. Its official URL candidates require a fresh merchant check through the normal discovery/baseline pipeline. Automatic RSS-to-monitor scheduling is not enabled in this alpha.

## Safety boundaries

AutoGrab does not bypass CAPTCHA, Cloudflare challenges, waiting rooms, 2FA, or merchant security controls. It does not clone cookies, export profiles, rotate proxies, spoof fingerprints, or use stealth login automation.

**AutoGrab never performs final payment automatically.** This alpha also disables final order creation. Do not enable legacy order helpers as a substitute for an independently verified Edge order adapter.

## Development

```sh
uv run --locked python -m playwright install chromium
./test.sh
node --test edge-extension/tests/*.test.mjs
uv build
```

Fixture tests validate code contracts, not current live stock or checkout readiness. Live observations and remaining limitations are listed in [the provider status document](docs/MULTI_PROVIDER_STATUS.md). No new production dependency was added for the multi-provider expansion.

MIT licensed. [Third-party research and notices](THIRD_PARTY_NOTICES.md). Public releases exclude local databases, private logs, account configuration, profiles and original private development history.
