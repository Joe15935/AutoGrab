# AutoGrab working rules

- Extend the existing Core, SQLite baseline/event/PurchaseIntent ledger, SMTP and
  normal Edge Companion. Use small fixed adapters; no replacement framework.
- Scope: BandwagonHost, DMIT, VMISS, V.PS, Apple; optional NodeSeek signals.
- Default DRY_RUN, LIVE OFF, ARM OFF. Final orders and payments stay disabled.
  Never use historical regression helpers to evade the supported CLI boundary.
- Use the user's ordinary Edge profile and official Native Messaging. Never read
  or export credentials, cookies, profiles, security tokens or payment data.
  No CDP, authenticated Playwright, stealth, CAPTCHA solvers or proxy rotation.
- Pause for login, 2FA or challenge. Resume the same intent only after ordinary
  page evidence returns. One blocked provider must not stop the other research.
- Baseline initialization never triggers. Marketing words and annual billing
  are signals only. UNKNOWN is not AVAILABLE. Prices are recorded, not filtered.
- Distinguish public discovery, stock, configuration, cart, checkout, order and
  payment evidence. Never claim live success from fixtures or a passing build.
- Preserve private runtime data and historical tests. Changes must be narrow,
  reviewable and reversible. Add no production dependency without authorization.
- Persist before dispatch. Never replay uncertain mutations. Provider+product
  identities and one-shot notification claims must remain isolated.
- Run relevant tests after changes; run ./test.sh, Node extension tests and
  uv build before a release. Scan final source, artifacts and full public Git
  history including commit metadata. Do not publish private development history.
- Public publishing requires explicit user authorization. For the current
  multi-provider sprint, public repository creation, main push and v0.3.0-alpha
  were explicitly authorized. This is not general permission for future releases.
