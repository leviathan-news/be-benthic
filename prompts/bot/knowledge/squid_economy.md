keywords: squid,balance,vault,drop,earnings,points,economy,token,onchain,offchain,ledger,projected
---
SQUID Economy:
- SQUID balances are tracked OFFCHAIN as an append-only ledger (VaultTransaction table).
- Balance = SUM(amount WHERE direction='in') - SUM(amount WHERE direction='out').
- Max precision: 12 digits, 2 decimal places (max 9,999,999,999.99 SQUID).
- SQUID is also an ERC-20 onchain (Fraxtal, 0x6e58...97Fe, 18 decimals, 38M supply).
  The onchain token and offchain ledger are separate systems.
- Sources of SQUID: monthly drops (leaderboard category earnings), tips received, prediction market payouts.
- Available balance for trading/tipping includes projected earnings × safety margin.
  Safety margin scales with day of month: 5% (days 1-3) → 80% (days 29-31).
  This means available balance grows through the month as earnings become more certain.
- VaultTransaction types: tip_sent/received/refund, drop_allocation, claim,
  prediction_buy/sell/payout/refund, reconciliation (overdraft clawback).
- Vault allocation: users can set recurring or one-time SQUID allocations from their monthly drop.
