```text
Script Mode: PASS
QingLong Mode: PASS
Bandwagon lightweight monitor: PASS
DMIT lightweight monitor: FAIL
VMISS cooldown-aware monitor: PASS
V.PS lightweight monitor: PASS
Apple lightweight monitor: PASS
Email: PASS
Edge Companion regression: PASS
```

```text
Average request count/provider: 2.20 attempts
Average single-run runtime: 7.40 seconds (provider work); 7.53 seconds (whole process)
```

## Acceptance scope — v0.6.0-alpha

Verified 2026-09-22 America/New_York (2026-09-23 UTC). This release adds the
script interface to the existing system; it does not replace its architecture
or claim new L6/L7 merchant evidence.

- **Script Mode PASS:** five provider commands and `all`, MONITOR/QUERY/DRY_RUN,
  quiet exit, conservative persistent budgets, SMTP-first opportunity dispatch,
  bounded scheduled launch and BURST. No new required runtime dependency.
- **QingLong Mode PASS is the script/package contract:** all five wrappers ran
  from an isolated environment containing only the AutoGrab wheel; QUERY wrote
  nothing. Two wrappers also exercised actual persistent cooldown. **A running
  QingLong panel/Linux container was not deployed or verified.** Docker's local
  daemon was unavailable; no new Docker service or scheduler was installed.
- **DMIT live public discovery FAIL:** one unauthenticated request encountered
  `HUMAN_CHALLENGE_REQUIRED`, exit 2. Its 91-product baseline and prior manual
  L5 checkout evidence remain intact. No browser fallback, challenge workaround
  or retry was attempted; a subsequent wrapper run exited 0 with zero requests.
- **VMISS cooldown PASS is not catalogue readiness:** the admitted public probe
  required human verification; the next process read the persisted cooldown and
  sent zero HTTP requests. The earlier Edge 1015 observation remains historical
  evidence. No complete live VMISS baseline or checkout is claimed.
- **Apple PASS uses an isolated, previously verified research target:** CN
  `MYEV3CH/A`, store `R359`, fresh pickup AVAILABLE. The user's formal target
  configuration remains absent. Ordinary `autograb apple` correctly reports
  `APPLE_TARGETS_NOT_CONFIGURED` and makes zero requests. The research target was
  not installed as a purchase preference or copied into public example config.
- **Email PASS means SMTP_ACCEPTED**, through the new facade using the existing
  TLS/Keychain backend. Inbox delivery was not independently checked. This was
  one clearly labelled test email, not an invented opportunity or PAY NOW notice.
- **Edge PASS:** 65 extension tests passed. The idle installed native host was
  refreshed; the unchanged 0.5.0 companion reconnected with a fresh heartbeat,
  READY, no active intent, no uncertain mutation, LIVE OFF and disarmed. Protocol
  and extension code were unchanged. No merchant tab was driven or cart rerun.

## Actual single-run measurements

| Provider | Result | HTTP attempts | Startup ms | Provider seconds | Process seconds | Peak RSS MiB |
|---|---|---:|---:|---:|---:|---:|
| Bandwagon | 48 observed; 0 opportunities | 1 | 84.82 | 2.690 | 2.820 | 38.5 |
| DMIT | Human check required; stopped | 1 | 81.20 | 10.482 | 10.612 | 38.8 |
| V.PS | 66 observed; 0 opportunities | 7 | 78.57 | 19.824 | 19.944 | 40.6 |
| VMISS | Human check required; cooldown saved | 1 | 78.27 | 1.601 | 1.729 | 38.9 |
| Apple | Isolated target; fresh AVAILABLE; silent first baseline | 1 | 76.90 | 2.420 | 2.539 | 39.5 |

Five samples, one per provider; blocked probes are included. The separate
unconfigured Apple run and subsequent cooldown skips are excluded from these
means. Mean measured startup is **79.95 ms** from package entry through config
loading, excluding interpreter bootstrap. RSS is process peak from `getrusage`,
not an incremental allocation measurement or a long-term resource guarantee.
Counts are actual public HTTP attempts, including failed responses; no redirects
or implicit retry loops are followed. V.PS traversed its public index plus six
families, not one request per product.

Actual restarted QingLong wrappers: DMIT **0 requests / 0.119 s**;
VMISS **0 requests / 0.117 s**, both exit 0 before their saved deadlines.
Production run outcomes remain in private SQLite and disposable query views.
No raw merchant responses, account data or SMTP settings are release assets.

## Implementation and preservation

The thin scripts call public `run_provider`. `ProviderSnapshot` supplies a
consistent observation to the existing ingest/classifier/dedup pipeline.
SQLite remains authoritative for baselines, events, intents and cooldown.
Core persists VMISS traversal scratch and Apple store rotation across Cron
invocations. Apple script checks pickup only; the existing explicit catalogue
refresh and target wizard are retained.

QUERY intentionally reads **last saved state**, with timestamps and
`fresh=false`; it does not make a new merchant check. An atomic public projection
avoids even SQLite WAL/SHM file creation. It is never used to authorize network,
notifications, browser actions or orders. MONITOR refreshes observations.

`notify(title, body, url=None, priority=None)` uses existing Email. BWH's optional
Mac preparation is after a durable notification claim and SMTP acceptance;
QingLong wrappers reject DRY_RUN and Edge preparation. Existing payment-ready
mail remains separately guarded by verified official HTTPS invoice evidence.

Scheduled launch checks configuration and email completeness, warms at T−30,
sleeps efficiently and starts no earlier than T0. BURST needs an explicit known
launch, cannot shorten existing reservations/cooldown, and starts no new public
HTTP attempt after its deadline. The current in-flight request may finish later.
Offline clock/transport tests verify the boundary; no real flash sale was run.

Preservation checks after installation and host reconnect:

- BWH **48**, DMIT **91**, V.PS **66** product baselines retained.
- All **4** existing PurchaseIntent rows unchanged, including prior locks and
  uncertainty; no submission markers, order IDs or invoice IDs added.
- Existing private email settings matched their pre-change SHA-256.
- The main config and formal Apple preferences were not overwritten.
- Mac lightweight installer completed; browser packages became optional.
  No real Cron job, always-on monitor, public webhook or web server was started.

## Validation and release

| Command / check | Result |
|---|---|
| `python -m unittest discover -s tests -p test_script_mode.py -q` | 19 passed: quiet/no-write mode, once-only notices, cooldown, restart traversal, store cursor, launch/deadline, wrapper boundaries and official email links |
| `./test.sh` | 634 Python tests passed, 45.039 s on final implementation |
| `node --test edge-extension/tests/*.test.mjs` | 65 passed |
| `python -S -m autograb --help` | Passed without site packages / Playwright |
| Isolated wheel + five wrapper processes | Passed; only `autograb` installed; query root remained empty |
| `安装 AutoGrab.command` | Completed; private settings/baselines retained |
| `sh -n` for changed launch/install scripts; `git diff --check` | Passed |
| `uv build` | Source distribution and wheel built |

Source, final archives and public-only Git history are scanned before publishing.
Release assets are source ZIP, wheel, source distribution and SHA256SUMS; the
downloaded release copies are checked against local hashes. The final local
`VERIFICATION.json` records the published commit and completed scans/download
checks. No private development refs are pushed.

Operational research was limited to four projects. QLScriptPublic was reviewed
as an architectural/operational reference. No source code was copied due to the
absence of an explicit repository license. See [the bounded comparison and exact
file observations](SCRIPT_MODE_RESEARCH.md), [usage and Cron examples](SCRIPT_MODE.md)
and [third-party notices](../THIRD_PARTY_NOTICES.md).

```text
REAL ORDERS = 0
PAYMENTS = 0
LIVE = OFF
ARM = OFF
```
