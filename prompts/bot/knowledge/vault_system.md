keywords: vault,allocation,withdraw,claim,set,recurring,one-time
---
Vault System — SQUID Savings & Withdrawal:
- /vault — show vault balance and allocation settings.
- /vault set <amount> — set a recurring monthly target (diverted from monthly drops).
- /vault add <amount> — one-time allocation from next drop.
- /vault off — disable recurring allocation.
- /claim — withdraw vault balance. Requires linked Ethereum address (/ethereum first).
- Balance = sum of VaultTransactions (append-only ledger, direction in/out).
- vault_funded field on tips tracks how much came from vault vs projected earnings.
- Errors: "Invalid amount", "You need to set your wallet first" (if no ETH address).
