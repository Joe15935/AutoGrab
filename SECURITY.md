# Security

This experimental alpha supports observation, bounded checkout preparation and
an experimental shared order/invoice/payment-ready lifecycle. Defaults are
DRY_RUN, LIVE OFF, ARM OFF, REAL_ORDER_SMOKE_TEST_ARMED=false. No real order or
payment was created. Merchant login, 2FA, CAPTCHA and challenges belong to the user.

Automatic payment is forbidden, including Pay Now, payment authorization, account
credit application and final Apple purchase confirmation. Complete Order cannot
be assumed to be a no-charge operation: account credit and saved payment methods
may cause immediate charging. Current BWH/DMIT no-charge submission contracts are
offline experimental fixtures, not independently verified merchant contracts.
Real submission therefore remains blocked; fixture markers must never be injected
into a merchant page to obtain authorization.

Normal monitoring ARM cannot grant order-smoke permission. A separately confirmed
one-intent lease expires within 60 seconds, is consumed once, and is never restored
from configuration or SQLite. It still requires fresh session, exact product/quote,
merchant no-charge evidence and preflight checks. Kill/expiry checks run again
after the submission nonce is committed and immediately before dispatch. Explicit
arming does not replace any missing merchant evidence.

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

Order timeouts, crashes and disconnects enter ORDER_UNCERTAIN. Recovery searches
the same provider/product/intent; even strongly verified absence does not trigger
automatic resubmission. A PAYMENT_READY result requires matching order/invoice
identity, unpaid state, exact product/amount and an official HTTPS invoice page.
The email's phone-friendly link is the merchant URL, not a local server. Sending
is claimed once before SMTP; transport uncertainty never permits another order
or automatic duplicate notice. Fixture/simulated evidence cannot become real
payment-ready evidence.

ProviderRateBudget persists cooldown and one-probe claims by provider, region and
endpoint. Honor Retry-After as a minimum, retain the 15-minute conservative fallback
for limited/blocked routes and increase it on repeated limits. Restarting or a
failed probe does not clear the wait. Do not evade limits with alternate profiles,
cookies, IP changes or browsers. Apple 541 and VMISS 1015 are not evidence of stock
absence; failures remain UNKNOWN. Ordinary Edge Apple Bag observation is read-only
and blocked controls are never enabled by AutoGrab.

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
