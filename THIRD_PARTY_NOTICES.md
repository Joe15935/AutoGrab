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
or CPU benchmark was performed; no numerical resource guarantee is claimed.

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
