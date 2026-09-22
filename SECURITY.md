# Security

This experimental alpha supports observation and bounded checkout preparation.
Final order submission and payment are disabled. Defaults are DRY_RUN, LIVE OFF,
ARM OFF. Merchant login, 2FA, CAPTCHA and other challenges belong to the user.

Do not send passwords, cookies, session tokens, browser profiles, card data,
private databases, raw authenticated HTML or unredacted logs in an issue.
For a suspected vulnerability, use a private GitHub security advisory when
available. If private reporting is unavailable, open a generic issue requesting
a private channel without including exploit details or sensitive data.

The companion uses fixed, allowlisted provider adapters and exact official
origins. Native Messaging frames are bounded and validate provider, product,
intent and command identities. No arbitrary code or selectors are accepted.
A dispatched mutation is persisted first; uncertain actions are not replayed.
Browser security permission prompts and extension installation are not bypassed.

Private runtime files stay outside Git: configuration, SMTP secrets, SQLite
state, profiles, cookies, sessions, artifacts and logs. SMTP credentials are
read only from the designated local Keychain item or temporary environment.
The RSA key in the extension manifest is a public extension-identity key,
not a signing secret. No private key is distributed.

Public releases must scan the exact exported tree and its complete reachable
Git history, including commit metadata and non-secret personal data. Original
private development history must not be pushed merely because the latest tree
is clean. Archives and wheels must be inspected separately before attaching.

Website changes can invalidate a parser. UNKNOWN is not AVAILABLE. A cart or
checkout page does not prove reservation, order creation or payment. Independent
real-site evidence is required before enabling another provider's mutations.
