# BandwagonCheckoutBoundary

Evidence date: 2026-09-22. Source: current anonymous official site observed through the in-app browser. No account session, order submission or payment was used. Selected public metadata only; no raw page snapshots, tokens or personal data are retained here.

| Boundary | Observed evidence | Result |
|---|---|---|
| Public product | PID 87, SPECIAL 20G KVM PROMO V5 - CN2 GIA ECOMMERCE; 1 Year, $169.99 USD; USCA_9 | REAL SITE |
| Product configuration | `https://bandwagonhost.com/cart.php?a=confproduct&i=0`; Add to Cart belongs to POST form at that URL | REAL SITE |
| Cart | One controlled configuration POST reached `https://bandwagonhost.com/cart.php?a=view`, Order Summary, matching public name and annual amount | REAL SITE |
| Checkout navigation | Checkout is type=button with exact onclick `window.location='cart.php?a=checkout'` | REAL SITE; navigation only |
| Checkout | `https://bandwagonhost.com/cart.php?a=checkout`; heading Checkout, empty account fields and login link | REAL SITE, anonymous |
| Final submit candidate | Complete Order belongs to POST form at `https://bandwagonhost.com/cart.php?a=checkout` | OBSERVED; NOT CLICKED |
| Login requirement | Existing-account login offered at `/cart.php?a=login`; unauthenticated checkout has account/contact fields | Existing-account login required for intended account flow; guest/new-account submission not tested |
| Terms | Checkbox `accepttos` visible and unchecked | Observed; left unchecked; server-side mandatory validation not tested |
| Gateway | PayPal (`paypal`), Credit Card (Stripe) (`roudiappstripecheckout`), Unionpay (`payssionunionpaycn`), Alipay (`payssionalipaycn`) | Anonymous page only; logged-in account availability UNKNOWN |
| Default gateway | Website selected PayPal initially | No gateway was changed by the agent |
| Creation semantics | Whether Complete Order creates an order/invoice without any immediate debit, balance use, mandate or saved-card authorization | UNKNOWN; authenticated review required |
| Order/Invoice IDs, result URL | No submission | NOT OBTAINED |
| Official Invoice URL | Program accepts only exact merchant `viewinvoice.php?id=…` plus full matched receipt evidence | Conservative validation contract; actual account path not yet verified |
| Actual payment page / status / amount | No actual Invoice | NOT TESTED |
| Cart inventory lock | No authoritative indication observed | UNKNOWN |
| Unpaid order inventory lock | No real order | UNKNOWN |
| Payment allocation / stock guarantee | No payment | UNKNOWN |
| Invoice/cart/order expiry | No explicit usable deadline observed | UNKNOWN |
| Mobile/browser link portability | No real Invoice to test | UNKNOWN |

Observed public sequence:

```text
Product → Configuration → Add to Cart POST → Cart → Checkout GET
                                                        ↓
                                             USER ACTION REQUIRED
```

Proposed but unverified continuation:

```text
Authenticated Checkout → Complete Order? → Order? → Invoice? → official payment page?
```

Do not convert the second sequence into a confirmed claim. The final button could have different effects for an account with credit or a saved payment method. No authenticated action is implemented on that assumption.

## Code enforcement

- Original Phase 1 policy and commands stop before configuration POST.
- `CheckoutPolicy` permits a separately issued, exact, single-use configuration POST ticket, optional official Checkout GET and narrowly scoped account-page GETs.
- A durable public-checkout dispatch marker precedes the cart mutation. An uncertain or previous dispatch is not replayed automatically.
- `prepare_checkout` and `submit_unpaid_order` intentionally raise ORDER_BOUNDARY_UNVERIFIED. Configuration flags cannot enable final submission.
- `reconcile_intent` returns UNKNOWN until actual account/order/invoice matching is established. A missing local ID never proves absence on the server.
- Session check requires successful official client-area response, exact location, visible unique logout link, no password form and no detected challenge. The selector is a conservative unverified contract until tested in the user's own session.
- Unit/integration fixtures validate these refusal rules; they do not prove an actual unpaid-order workflow.

## Required continuation

The user enters SMTP settings and app password through the hidden-input Keychain wizard, sends the real test, and logs in personally in the dedicated persistent browser. Then inspect the authenticated account and checkout without billing, implement verified order/Invoice identity matching, test it offline, and consider at most one controlled unpaid ordinary-product smoke only when every guard and no-charge boundary has passed. Never execute final payment or automatically cancel any order.
