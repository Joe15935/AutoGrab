# Bounded operational research — 2026-09-22

Four projects were selected after GitHub searches for QingLong purchasing,
stock/Cron monitoring and flash-sale scripts. No upstream task script was run.
The route is a thin extension of existing AutoGrab, without a new runtime or
service. The MVP is one shared one-shot monitor plus five tiny wrappers.

| Project | Maintenance / license observed | Useful pattern | Deployment and reuse decision |
|---|---|---|---|
| [QLScriptPublic](https://github.com/smallfawn/QLScriptPublic) | Not archived; pushed 2026-09-05; no explicit repository LICENSE | Environment-driven flags, shared HTTP/session helpers, notification facade, early start and timed wait | Task scripts fit QingLong; requirements vary by script. No measured upstream RSS. Operational reference only; no code copied. |
| [QingLong](https://github.com/whyour/qinglong) | Not archived; pushed 2026-09-22; Apache-2.0 | Python/JS/Shell execution, environment/config/log management, Cron, notifications | Existing platform can host wrappers. Its panel/container is additional infrastructure; AutoGrab does not fork or embed it. |
| [jd_ql_assistant](https://github.com/SSJACK8582/jd_ql_assistant) | Not archived; last code push 2023-11-18; no declared license | Distinct stock/appointment/timed purchase modes | Cookie-dependent commerce scripts; old maintenance and unrelated merchant contracts make direct reuse unsuitable. No account/cookie or submit implementation adopted. |
| [changedetection.io](https://github.com/dgtlmoon/changedetection.io) | Not archived; pushed 2026-09-21; Apache-2.0 | Change/restock monitoring and notification separation | Maintained general monitor with a resident UI and optional browser fetcher. Useful comparison, but replacing AutoGrab would duplicate its baseline and event core. No code copied. |

## What the four requested QLScriptPublic files actually show

Reviewed repository HEAD: `cf206f0177ad85934770096c8f3d970e9da03631`.
Sources: [README](https://github.com/smallfawn/QLScriptPublic/blob/cf206f0177ad85934770096c8f3d970e9da03631/README.md),
[sendNotify.js](https://github.com/smallfawn/QLScriptPublic/blob/cf206f0177ad85934770096c8f3d970e9da03631/sendNotify.js),
[tools/env.js](https://github.com/smallfawn/QLScriptPublic/blob/cf206f0177ad85934770096c8f3d970e9da03631/tools/env.js),
[chinaUnicom.py](https://github.com/smallfawn/QLScriptPublic/blob/cf206f0177ad85934770096c8f3d970e9da03631/daily/chinaUnicom.py).

- README is principally an index. The two-minute early-start guidance was found
  in the Unicom script's module documentation; it was not assumed from README.
- `run_grab_coupon` is a `globalConfig.sign_config` flag, not a function.
  `sign_grabCoupon` discovers candidate items, filters by configured amount/name,
  chooses a nearby release and waits. `UNICOM_GRAB_AMOUNT` and `UNICOM_GRAB_URL`
  configure the task. The wait may leave its loop up to half a second before
  the target; it is not proof of an exact, never-early launch.
- `UNICOM_ATTEMPT_COUNT` controls a regional monthly draw. The examined grab
  execution loop separately uses five attempts, short sleeps and result queries.
  AutoGrab does **not** adopt repeated mutation submissions. It uses bounded
  public reads, existing dedup/nonce rules and persistent cooldown.
- `UNICOM_TEST_MODE=query` skips tasks and queries account assets. Account
  environment entries can be separated, and shared helpers avoid implementing
  configuration/notification/session handling independently in every task.
- The script uses `requests.Session` and HTTPAdapter retry/backoff for selected
  server failures. Proxy failover, device/token automation and multi-account
  commerce are excluded. AutoGrab uses its existing unauthenticated stdlib
  HTTP readers and schedules a later Cron check instead of immediate retries.
- `tools/env.js` centralizes environment loading, optional stored values, task
  timing, buffered notices and sleep. HTTP session evidence comes from the
  Python script, not from assuming this helper supplies a complete HTTP engine.
- `sendNotify.js` reads channel configuration, exposes one notification call
  and fans out to configured channels. Its SMTP fields are `SMTP_SERVICE`,
  `SMTP_EMAIL`, `SMTP_TO`, `SMTP_PASSWORD`, `SMTP_NAME`; it also has Bark,
  PushPlus, ntfy, Telegram and webhook adapters. AutoGrab retains its existing
  validated TLS SMTP and adds a small facade/recipient alias, without copying
  this dispatcher or integrating extra channels.

The useful unit of reuse is the operational pattern: thin entry point → shared
configuration and durable state → bounded observation → transition → notice.
These repositories do not provide AutoGrab's five-provider normal-Edge checkout
contracts. No claim is made that QLScriptPublic implements those providers.

QLScriptPublic was reviewed as an architectural/operational reference.
No source code was copied due to the absence of an explicit repository license.
