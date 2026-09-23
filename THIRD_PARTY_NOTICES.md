# Third-party notices and reuse research

AutoGrab's new provider implementations were written independently from observed
public interfaces. No upstream monitor implementation was copied or vendored.
The application source is MIT licensed. Installed dependencies retain their own
licenses; see `pyproject.toml` and `uv.lock` (Playwright: Apache-2.0; keyring: MIT;
hatchling build backend: MIT).

| Project | License / status observed 2026-09-22 | Use and decision |
|---|---|---|
| [iponkan/dmit](https://github.com/iponkan/dmit) | No LICENSE found; pushed 2026-02-12 | PID/URL and stock behavior reference only; no copied code or Playwright challenge approach |
| [ENCHIGO/apple-pickup-watcher](https://github.com/ENCHIGO/apple-pickup-watcher) | GPL-3.0-or-later; pushed 2026-09-20 | Current pickup endpoint and SKU/store identity facts; no code reused, no bundled GPL app/runtime or bypass techniques |
| [LennonChin/AppleStore-Monitor](https://github.com/LennonChin/AppleStore-Monitor) | MIT; pushed 2026-07-03 | Batching/notification research; older fulfillment behavior not assumed current |
| [SourDurian/checkstock](https://github.com/SourDurian/checkstock) | No LICENSE found; pushed 2026-08-17 | VMISS path/theme reference, no copied implementation |
| [aakk007/vmiss-stock-monitor](https://github.com/aakk007/vmiss-stock-monitor) | No LICENSE found; pushed 2026-06-15 | README behavior reference only |
| [tomoyo233/WhmcsMonitor](https://github.com/tomoyo233/WhmcsMonitor) | Apache-2.0; pushed 2021-01-29 | Old WHMCS monitor comparison; duplicates Core, not integrated |
| [shali10/vps-monitor](https://github.com/shali10/vps-monitor) | MIT; pushed 2026-08-01 | Active stock-monitor comparison; current sources target different sites and permissive unknown-stock behavior is unsuitable; no code copied |
| [zcpgb3/bandwagonhost-stock-monitor](https://github.com/zcpgb3/bandwagonhost-stock-monitor) | No LICENSE; README only | Rejected as an implementation candidate; no code to reuse |
| [WHMCS/orderforms-standard_cart](https://github.com/WHMCS/orderforms-standard_cart) | No LICENSE found at reviewed release | Official template contract research only; Bandwagon and DMIT custom themes require their own evidence |
| [GoogleChrome/chrome-extensions-samples](https://github.com/GoogleChrome/chrome-extensions-samples) | Apache-2.0; some sample headers BSD | Official native messaging lifecycle reference; no vendored implementation |
| [microsoft/MicrosoftEdge-Extensions](https://github.com/microsoft/MicrosoftEdge-Extensions) | MIT | Official extension example reference; no vendored implementation |
| [lanxuewsr/nodeseek-rss](https://github.com/lanxuewsr/nodeseek-rss) | No LICENSE reported; pushed 2026-05-26 | RSS availability reference only; no Telegram/Resend/database stack reused |

The thin-adapter route preserves the existing database, email, event lifecycle
and extension. The reviewed standalone monitors would duplicate those pieces
and do not supply a verified five-provider normal-Edge checkout path. No new
production service or dependency is required. Resource use remains one Python
monitor, its local SQLite state, and the user's ordinary Edge process. No memory
or CPU benchmark was performed during the original expansion; v0.6 measurements
are listed separately in its Script Mode report, with no resource guarantee.

Minimal public-response/DOM fixtures are labelled as actual-schema excerpts or
synthetic contracts. They contain no authentication state or copied application
implementations. Full downloaded upstream source and full merchant pages are
research scratch and are excluded from releases.

## v0.4.0-alpha closure research

The current Apple watcher remains a protocol/design reference only:
GPL-3.0-or-later; **code copied into MIT AutoGrab: NO**. AutoGrab independently
parses official purchase-page JSON and store-list data with its existing Python
standard-library runtime. Installing the separate Rust/Tauri watcher would
duplicate monitoring, configuration and notifications; no such runtime was added.

DMIT's small monitor supplies PID/stock markers, not the current configuration
response semantics. Current official frontend code and ordinary Edge responses
were therefore checked directly. The website's implementation was not vendored.

For V.PS, the decisive reuse source is the official HostBill one-step order-page
contract at https://dev.hostbillapp.com/orderpages/ and current provider frontend
code. It verifies that the observed submission creates an order, so AutoGrab
keeps that mutation disabled. VMISS's older monitor references cannot certify
a currently rate-limited live store. The minimal route remains fixed adapters
with existing Core/SMTP/SQLite/Edge; no added production dependencies or services.

## v0.5.0-alpha order and request-budget research

The shared order lifecycle, SQLite nonce/notification claims, fixed Edge order
commands and ProviderRateBudget are small independent extensions of AutoGrab's
existing Core. No additional order engine, monitor runtime, rate-limit package or
production dependency was imported. Experimental DOM contracts are explicitly
labelled fixtures, not copied merchant implementations or live acceptance evidence.

Two official WHMCS references establish why a checkout submit cannot be assumed
to create only an unpaid order:

- [Official checkout template, reviewed commit a77d9d8](https://github.com/WHMCS/orderforms-standard_cart/blob/a77d9d8f2b8d0a010aebf8f9d36c85cf68f3d470/checkout.tpl#L575)
  (2026-07-08): account-credit and stored-card controls coexist with the final
  checkout submission. No LICENSE was found at the reviewed revision; the
  template is a behavior reference only and was not copied or vendored.
- [WHMCS credit balances: checkout and new orders](https://docs.whmcs.com/8-0-9/payments/credit-balances/#checkout-and-new-orders)
  explains application of account credit to new invoices. Applying credit is a
  financial action, not a harmless unpaid-order lookup. AutoGrab does not do it.

Current BWH/DMIT themes require their own independently verified no-charge
contract; the upstream template is not a substitute. The implemented experimental
contract has only offline fixture evidence, so current live submission is blocked.
No merchant account data, card state, full frontend bundle or real order response
was copied into these fixtures.

Apple work continues to use public catalogue/pickup behavior and ordinary Edge
page observations only. GPL watcher code remains uncopied. HTTP 541 handling reads
already-present response metadata and stops; it includes no Shield workaround,
fingerprint spoofing or private purchase API reconstruction. VMISS 1015 handling
uses persisted cooldown and a single later probe, without adopting challenge or
proxy approaches from third-party monitors.

## v0.6.0-alpha operational reference

QLScriptPublic — https://github.com/smallfawn/QLScriptPublic

Reviewed as an architectural and operational reference for:
- cron-driven automation
- environment-variable configuration
- notification abstraction
- timed execution patterns

No source code copied.
Repository had no explicit license at time of review.

QLScriptPublic was reviewed as an architectural/operational reference.
No source code was copied due to the absence of an explicit repository license.

The reviewed source revision, four bounded comparisons and precise observations
are documented in [Script Mode research](docs/SCRIPT_MODE_RESEARCH.md). The
upstream files remain outside the repository and release archives. AutoGrab's
new facade, scheduling and wrappers are independently written extensions of
its existing Core. No notification channel code or account automation was
imported. QingLong and changedetection.io (Apache-2.0) were comparison references,
not runtime dependencies; jd_ql_assistant (no declared license, last push 2023)
was rejected for direct reuse.
