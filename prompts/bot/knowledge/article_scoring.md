keywords: score,scoring,rank,ranking,trending,hot,click,clicks,predict,decay
---
Article Scoring & Ranking:
- Click prediction via half-life decay: score = 0.95^(hours_since_posted).
  Half-life ~13 hours. Min prediction: 5 clicks.
- Precomputed historical click table (first 60 minutes only) for early-stage matching.
  Match tolerance: ±30%. Lookback limit: 50 articles.
- Hot score combines recency + engagement for trending calculations.
- Predicted lifetime clicks and hot score (T+1h) stored in XQueue records.
- Features for ML prediction: submitter history, tags, votes, yaps, pre-pub clicks.
- Confidence rating 0.0-1.0 based on feature completeness and model maturity.
- Display format: 📊{clicks} 🔥{hot:.1f} {✓/~/? for confidence level.
