keywords: tip,tipping,undo,decline,unclaimed,pending,expire,expiry
---
Tipping System:
- /tip @username <amount> — tip SQUID to a user. /tip <amount> as reply also works.
- /tip with no amount defaults to 1 SQUID.
- Max tip: 10,000 SQUID per transaction.
- /undo — sender reverses their most recent tip within 60 seconds.
- /decline — recipient rejects a received tip within 10 minutes.
- Tip statuses: active (applied at next drop), pending (recipient has no ETH address, escrowed),
  reversed (undo/decline), expired (pending tip expired after 3 months, SQUID returned to sender).
- Vault-funded tips: the vault_funded field tracks how much came from vault vs projected earnings.
  At drop time, only the earnings-funded portion is deducted from the monthly allocation.
- Ledger entries: sender gets VaultTransaction direction='out' type='tip_sent',
  recipient gets direction='in' type='tip_received'. Undo creates type='tip_refund'.
