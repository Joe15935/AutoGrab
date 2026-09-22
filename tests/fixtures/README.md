# Public and synthetic fixtures

- `order-checkout.experimental.html` and `order-receipt.experimental.html` are
  invented, offline order contracts. They are not snapshots of real checkout or
  invoice pages and do not prove either merchant's no-charge behavior. Tests
  intercept every request locally. Do not add their markers to real sites.
- `edge-apple-product-disabled.public.html` reconstructs the observed disabled
  Add to Bag condition without session data. `edge-apple-bag.synthetic.html` is
  only a synthetic heading/route test; it does not establish exact Bag contents.

- `catalog.small.json` is a synthetic, minimal fixture created for parser safety tests. Its products, prices and stock states are test inputs, not current offers.
- `catalog.phase0.public.json` is the saved public catalog response from the Phase 0 investigation dated 2026-09-22. Source: `https://bandwagonhost.com/order/get-data`, observed through normal browser traffic with HTTP 200. It contains 4 tiers, 11 locations and 48 products. Only public catalog data is included; no browser session, cookies or account data is represented. Evidence is recorded in `../../../outputs/autograb-phase-0/AUTOGRAB_PHASE_0_REPORT.md`.
- `configuration.public.sanitized.html` reconstructs the sanitized public configuration structure observed during Phase 1 on 2026-09-22, at the session-bound `https://bandwagonhost.com/cart.php?a=confproduct&i=0` page. It is **not raw logged HTML** or a copied response. The observed structure includes the Product Configuration heading, strong product name, billingcycle select with price/cycle text, and an Add to Cart POST form. Product name and price are synthetic test values, not a fresh offer. No hidden PID is invented: production identity evidence combines the observed order link and exact visible product name. The fixture contains no cookies, tokens, session values, account information, or merchant scripts. Offline browser tests insert their own controlled script or mutate fields to test mismatch, expiry, and safety failures; every admitted request is fulfilled locally.

The historical fixture validates parser compatibility with the Phase 0 response. It does **not** prove fresh stock, present endpoint availability, an executable order URL, or successful cart access. Its 48 `outOfStock: false` values must not be reported as a new live inventory check.
