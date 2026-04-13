keywords: buy,sell,market,position,shares,probability,price,lmsr,amm,trade,trading,bet,predict,resolution,resolve,frozen,payout,cap,limit
---
Prediction Markets (LMSR AMM):
- Cost function: b * ln(e^(q_yes/b) + e^(q_no/b)). Higher b = deeper liquidity, flatter price curve.
- Price formula: price_yes = e^(q_yes/b) / (e^(q_yes/b) + e^(q_no/b)). Prices clamped to [1%, 99%].
- All amounts are Decimal with 2 decimal places (50.00 SQUID, not wei). No floating point.
- Each market has its OWN per-user cap (default 5000 SQUID per side) and min trade (default 10 SQUID).
  These are set per market by the creator — not a global constant. Check the error message for the actual cap.
- Market lifecycle: open → frozen (trading halted, no new trades) → resolved (yes/no outcome) or refunded (cancelled, everyone gets cost_basis back).
- Resolution: winning side shares convert to SQUID payout. Losing side gets nothing.
- Position = cumulative shares + cost_basis per user per side per market.
- /position shows current value using cost_to_sell (slippage-accurate), NOT marginal_price × shares.
  For large positions the actual exit value is lower than the naive calculation.
- Shares bought = binary search for max shares that fit within budget (shares_for_budget).
- Each trade creates atomic Trade + Position + VaultTransaction records in a single DB transaction.
- Trade records are immutable audit trail: price_before, price_after, shares, cost all captured.
- /markets shows top 10 open markets with YES/NO percentages and expiry dates.
- /leaderboard shows top 10 traders by realized P&L.
