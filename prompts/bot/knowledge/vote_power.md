keywords: vote,voting,power,vp,sybil,weight,multiplier,dilution,stake
---
Vote Power (VP) System — Sybil-Resistant Weighted Voting:
- VP is token-weighted: your onchain SQUID balance determines your vote multiplier.
- Convex logarithmic curve: 1K SQUID → 1.0x, 1M SQUID → 5.0x max multiplier.
- Formula: multiplier = 1.0 + 4.0 × ln(balance/1000) / ln(1000000/1000).
  Below 1K SQUID threshold = 1.0x base. Above 1M = capped at 5.0x.
- Trailing dilution penalizes rapid-fire voting: 15-day half-life decay.
  Voting weight = raw_vp / (dilution_score × time_decay).
  Vote infrequently = retain full power. Vote rapidly = diminishing returns.
- Concentrated holdings are rewarded — splitting across wallets loses value.
- This means whale votes count more, but they can't spam without dilution.
