# AutoGrab working rules

- Extend the existing Core, SQLite baseline/event/PurchaseIntent ledger, SMTP and
  normal Edge Companion. Use small fixed adapters; no replacement framework.
- Scope: BandwagonHost, DMIT, VMISS, V.PS, Apple; optional NodeSeek signals.
- Default DRY_RUN, LIVE OFF, ARM OFF, REAL_ORDER_SMOKE_TEST_ARMED=false.
  Ordinary ARM does not authorize an order smoke test. The separate process-local
  permission is for one intent/nonce, expires within 60 seconds, and is never
  restored from config or storage. Automatic payment is always forbidden.
  Current BWH/DMIT no-charge submit contracts are offline experimental fixtures,
  not verified merchant contracts: real submission remains blocked. Never use
  fixture markers or historical helpers to bypass that boundary.
- Use the user's ordinary Edge profile and official Native Messaging. Never read
  or export credentials, cookies, profiles, security tokens or payment data.
  No CDP, authenticated Playwright, stealth, CAPTCHA solvers or proxy rotation.
- Pause for login, 2FA or challenge. Resume the same intent only after ordinary
  page evidence returns. One blocked provider must not stop the other research.
- Baseline initialization never triggers. Marketing words and annual billing
  are signals only. UNKNOWN is not AVAILABLE. Prices are recorded, not filtered.
- Distinguish public discovery, stock, configuration, cart, checkout, order and
  payment evidence. Never claim live success from fixtures or a passing build.
- Freeze prior BWH/DMIT L5 evidence unless an adapter change requires a rerun.
  L4 includes a verified pre-order boundary: V.PS Order Now creates a real order
  and must not be clicked to manufacture a cart pass. Apple Bag remains blocked
  and read-only; a matching URL or visible bag heading does not verify its SKU.
- Preserve private runtime data and historical tests. Changes must be narrow,
  reviewable and reversible. Add no production dependency without authorization.
- Persist before dispatch. Never replay uncertain mutations. Provider+product
  identities and one-shot notification claims must remain isolated.
- L6/L7 implementation is experimental; REAL-SITE VERIFIED remains NO. Require
  exact quote, session, merchant and no-charge proof before submission. Unknown
  results enter ORDER_UNCERTAIN and read-only reconciliation; even strong absence
  does not automatically authorize a replacement submit. PAYMENT_READY requires
  a verified unpaid merchant invoice with exact identity/amount and official HTTPS
  URL; checkout alone is insufficient. Do not touch saved cards or account credit.
- Respect persisted ProviderRateBudget scopes and Retry-After. Limited/blocked
  routes use at least 15 minutes of fallback cooldown, increasing on repeated
  limits; allow only one admitted probe after cooldown. Do not clear budgets to
  force retries. VMISS still has no reliable live baseline; Apple errors remain
  UNKNOWN and do not become sold-out observations.
- Run relevant tests after changes; run ./test.sh, Node extension tests and
  uv build before a release. Scan final source, artifacts and full public Git
  history including commit metadata. Do not publish private development history.
- Public publishing requires explicit user authorization. For this Script Mode
  sprint, public docs/main synchronization and v0.6.0-alpha are explicitly
  authorized. This grants no real-order smoke test and
  no general permission for future releases. Preserve historical phase reports.
- Script commands stay one-shot and dependency-free; QingLong wrappers permit
  MONITOR/QUERY only. QUERY reads a labelled saved projection without network,
  SQLite access or file writes. Projection files are disposable; never use them
  to authorize a request, an opportunity, Edge action or an order.
