# AutoGrab

Multi-provider stock monitor and checkout assistant for limited VPS plans and product launches.

[简体中文](README.zh-CN.md) · [Security](SECURITY.md) · [Research and verification](docs/MULTI_PROVIDER_STATUS.md)

**Experimental alpha. DRY_RUN by default. LIVE OFF. ARM OFF. No final order submission or automatic payment.**

AutoGrab keeps a catalogue baseline, detects subsequent product and stock changes, sends email, and uses a companion extension in your ordinary Microsoft Edge profile. A first scan never treats the whole catalogue as new stock. `SPECIAL`, `PROMO`, and annual billing are signals, not purchase triggers. Prices are recorded, never used as budget or value filters.

## Actual support

| Provider | Discovery / stock | Edge checkout assistance |
|---|---|---|
| BandwagonHost | Official catalogue; existing baseline retained; new-product/restock/group detection | Product → configuration → cart → signed-in checkout verified; stops before final order |
| DMIT | Current custom catalogue parser; normal Edge public snapshot verified; anonymous HTTP may require a human challenge | Experimental read-only companion; manual configuration reached; cart/checkout unverified |
| VMISS | Experimental parser; current official store requires human verification | Read-only handoff; cart/checkout unverified |
| V.PS | 66 public plan IDs observed; marketing purchase links do not prove stock | HostBill-style portal, not WHMCS; read-only handoff; cart/checkout unverified |
| Apple Store | Configured SKU + store pickup inventory; official CN endpoint tested | Read-only handoff; delivery, preorder and bag/checkout unverified |

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

Use `--provider bandwagon`, `dmit`, `vmiss`, `vps`, or `apple` to inspect one provider. All commands share the existing database. HTTP blocking pauses that provider rather than launching a bypass. A paused process must be explicitly restarted after the human issue is resolved; it does not retry a challenge forever.

Runtime configuration is **TOML**. [config.example.yaml](config.example.yaml) is a readable configuration outline, not an alternative parser. Apple is intentionally unconfigured until you supply a real region, exact SKU, and store IDs. An example research SKU is not a default purchase target. Family/model/storage/color/carrier fields describe a configured SKU; automatic variant expansion is not implemented.

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
