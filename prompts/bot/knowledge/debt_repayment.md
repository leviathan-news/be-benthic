keywords: debt,repay,repayment,owe,overdraft,fingerprint
---
Debt & Repayment System:
- Users can accumulate debt via overdraft (e.g., tipping more than projected earnings).
- Overdrafts are clawed back via VaultTransaction type='reconciliation'.
- /repay — shows token selection keyboard (SQUID, frxETH, ETH) for on-chain settlement.
- Unique fingerprinted payment amount per user/request (within 0.1% of debt).
  E.g., for 500 SQUID debt, payment might be 499.996327 — prevents amount guessing.
  Hash-based security: SHA-256(SECRET_KEY + user_id + salt).
- /repay_status — check pending repayment request status (pending/matched/confirmed/expired).
- Errors if debt is zero or user has no linked ETH address.
