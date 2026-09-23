# Script Mode and QingLong

Each command calls the same Core, SQLite baseline, event classifier and SMTP
implementation. No browser, web server, dashboard or account session is needed
for public monitoring. A first baseline is silent. Ordinary annual plans do not
become opportunities merely because they exist.

```sh
./start.sh bandwagon
./start.sh dmit --json
./start.sh vmiss
./start.sh vps
./start.sh apple
./start.sh all --json
```

The installed console entry is `autograb`; activate `.venv/bin/activate` or use
its absolute path for Cron. `--root PATH` or `AUTOGRAB_ROOT` selects persistent
configuration and data. Without either, the current directory is used. Keep the
same root between runs. No-op runs and cooldown skips exit 0 silently; `--json`
shows status, attempted HTTP count, elapsed time and approximate peak RSS. A
merchant block or failed notification exits 2; local config/storage errors exit
1. Ctrl-C exits 130. A concurrently running controller is skipped, not duplicated.
The legacy `monitor` loop remains available; do not run competing controllers.

## Modes

`--mode` takes precedence over `AUTOGRAB_MODE` (default `MONITOR`). These are
observation modes, independent of the existing transaction safety gates:

| Mode | Behavior |
|---|---|
| QUERY | Reads the most recent saved public observation and baseline projection; prints timestamps and `fresh=false`. No HTTP, SQLite connection, email, Edge or filesystem writes. Until the first Script MONITOR run it reports `NO_SAVED_OBSERVATION`. |
| MONITOR | Public HTTP, durable budgets, baseline comparison and opportunity email. No browser action by default. |
| DRY_RUN | Same monitoring; on a Mac, `--prepare-checkout` optionally queues an opportunity to the existing normal Edge assistant after SMTP acceptance. Current automatic preparation is BWH only; other adapters remain read-only. No real order. |

QUERY is intentionally a **saved-state query**, not a fresh HTTP check. Refresh
with MONITOR. `data/public-snapshot-*.json` is an atomic, disposable read view;
SQLite remains the only authority for baselines, events, cooldown and intents.
A last-known AVAILABLE product in a saved view is not proof of current stock.
Failed Apple reads retain UNKNOWN and never create a false sold-out/restock.

```sh
autograb bandwagon --mode QUERY
autograb bandwagon --mode DRY_RUN --prepare-checkout
```

Normal commands always report LIVE OFF / ARM OFF. The separate experimental
order smoke-test gate is unchanged and not reachable through these wrappers.
Notification claims are saved before SMTP; an uncertain send is not replayed.

## Conservative request pacing

| Provider | NORMAL scan interval | BURST minimum interval | Requests per admitted run |
|---|---:|---:|---|
| Bandwagon | 300 s | 30 s | 1 catalogue GET; optional local preparation adds a fresh recheck |
| DMIT | 900 s | 300 s | One bounded catalogue traversal, up to 60 observed groups; a denial stops at that page |
| VMISS | 900 s | 900 s | 1 public page; Core persists unfinished traversal across Cron runs |
| V.PS | 1800 s | 300 s | Index + observed product families; 7 GETs in this verification |
| Apple | 60 s | 60 s | 1 pickup request, batched SKUs for one store; store rotation persists |

These are conservative client defaults, not a claim about merchant-approved
quotas. Existing provider `interval_seconds` in TOML can lengthen an interval.
Retry-After and persisted blocks always take precedence. 429, VMISS 1015 and
Apple 541 stop work and impose at least the existing 15-minute fallback, which
increases on repeated blocks. Only one later probe is admitted. No HTTP retry
loop, proxy failover or challenge bypass is used. A crash retains its lease;
the next invocation does not assume the interrupted request was safe to repeat.

DMIT public HTTP may require human/Edge review despite prior manual L5 checkout
evidence. This command will not fall back to Playwright. VMISS's live parser and
complete baseline are still unverified while its public page is blocked.

For Apple, run `autograb apple-configure` on the Mac first. Copy only the target
TOML overlay to your private QingLong state directory if desired. The ordinary
script polls pickup only; `apple-catalog-refresh` remains the explicit separate
catalogue operation. No target, account or device session is chosen/exported
automatically. Full catalogue refresh is not performed on every Cron tick.

## Known launch

```sh
autograb bandwagon --launch-at "2026-11-27T00:00:00-08:00"
autograb bandwagon --launch-at "2026-11-27T00:00:00-08:00" --pace BURST --burst-seconds 120
```

Start early. Preflight checks provider/configuration and email completeness;
this is not an SMTP-delivery test. Sleep until T−30 seconds, recheck email and
optional local Edge readiness, then wait for T0 without spinning. Warmup makes
no merchant request and sends no email. Edge must already be connected when
preparation is requested; the command does not launch or log into a browser.
Timezone offsets are mandatory. A launch over 60 seconds late is rejected.
BURST requires an explicit known launch and ends after at most 300 seconds;
an in-flight HTTP request can finish after the deadline. A block ends the window.
Existing NORMAL reservations are not shortened; avoid overlapping ordinary
jobs near a known launch, and never delete cooldown records to force a retry.

## QingLong installation

Requires an existing QingLong runtime with **Python 3.12–3.14** and venv/pip.
No Node dependencies, browser downloads, Docker sidecars or web deployment are
required by AutoGrab. The repository wrappers were tested; deployment inside a
running QingLong panel is separately reported in the release verification.

In the panel's terminal (adjust repository paths for your installation):

```sh
git clone --branch v0.6.0-alpha https://github.com/Joe15935/AutoGrab.git /ql/data/repo/AutoGrab
python3 -m venv /ql/data/autograb-venv
/ql/data/autograb-venv/bin/pip install /ql/data/repo/AutoGrab
mkdir -p /ql/data/autograb-state/config
```

Alternatively subscribe to the repository in QingLong and select the five
`scripts/qinglong/autograb_*.py` files. Install AutoGrab into the interpreter
used by that task; merely downloading the wrappers does not install the package.
Use a persistent root outside the repository checkout so updates preserve data.

Set these private environment values in the panel:

| Variable | Meaning |
|---|---|
| AUTOGRAB_ROOT | `/ql/data/autograb-state` (same persistent path for every run) |
| AUTOGRAB_EMAIL | Recipient |
| AUTOGRAB_SMTP_HOST | TLS SMTP host |
| AUTOGRAB_SMTP_USER | SMTP user and default sender |
| AUTOGRAB_SMTP_PASSWORD | Private password/app password; never commit it |
| AUTOGRAB_MODE | `MONITOR` or `QUERY`; wrappers reject DRY_RUN |
| AUTOGRAB_BANDWAGON_ENABLED | `true` / `false` (also accepts 1 / 0) |
| AUTOGRAB_DMIT_ENABLED | Same |
| AUTOGRAB_VMISS_ENABLED | Same |
| AUTOGRAB_VPS_ENABLED | Same |
| AUTOGRAB_APPLE_ENABLED | Same; disable until targets are configured |

Defaults use implicit TLS on port 465. For STARTTLS/587 or a different sender,
put non-secret settings in the existing `[smtp]` TOML table. Existing SMTP env
aliases remain supported. No secret is stored by AutoGrab's configuration loader.
Configuration persists under `AUTOGRAB_ROOT/config`, SQLite under `data`.

A task's command can be:

```sh
/ql/data/autograb-venv/bin/python /ql/data/repo/AutoGrab/scripts/qinglong/autograb_bandwagon.py
```

Conservative five-field Cron examples, in the scheduler's configured timezone:

```cron
*/5 * * * * /ql/data/autograb-venv/bin/autograb bandwagon
2,17,32,47 * * * * /ql/data/autograb-venv/bin/autograb dmit
3,18,33,48 * * * * /ql/data/autograb-venv/bin/autograb vmiss
4,34 * * * * /ql/data/autograb-venv/bin/autograb vps
* * * * * /ql/data/autograb-venv/bin/python /ql/data/repo/AutoGrab/scripts/qinglong/autograb_apple.py
```

For the strictly monitor-only boundary, use the corresponding Python wrapper in
each Cron command. Some panels store Cron and command in separate fields.
Overlapping invocations skip with exit 0; stagger jobs or use one `autograb all`
task to avoid starvation. Apple errors remain visible and use persistent cooldown.
For a UTC scheduler, `58 7 27 11 *` starts two minutes before the example launch
above; replace the date and full ISO timestamp for each announced event.

The first release supports **monitor + email only**. QingLong does not use your
Mac's session, create carts or connect to Edge. A future Opportunity Webhook can
consume the existing event ID/provider/product/details envelope after its
durable claim. No remote endpoint, token, listener, queue or webhook delivery is
implemented or enabled in this release. You may keep your own QingLong notice
layer separately; AutoGrab currently implements Email only.

## Simple notification API

```python
from autograb.notifications.adapter import notify
await notify("AutoGrab test", "No order was created", priority="normal")
```

The async facade uses the existing SMTP TLS, private settings and optional Mac
Keychain. Official public product URLs are accepted; this is not a payment-ready
shortcut. The verified invoice notifier still owns future PAY NOW messages and
requires official HTTPS invoice/order evidence. Other channels are extension
points only, not implemented integrations.
