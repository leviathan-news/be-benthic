keywords: twitter,tweet,x queue,x posting,autopost,cross-post,x.com,dispatch to x
---
X/Twitter Cross-Posting:
- Approved articles are queued for X posting via the X Queue system.
- Smart posting with rolling performance baseline: hot-score threshold = avg - 0.5×stddev.
- Capacity-aware: 12+ posts in queue = lenient threshold, <4 = strict.
- Fallback: after 3 consecutive skips, posts anyway to prevent account silence.
- X_AUTOPOST_ENABLED=1: posts immediately on approval. =0: 2-hour batch cron.
- Rolling window of 10 recent posts for baseline. Decay factor 0.95^hours.
- Predicted clicks and hot score drive queue priority.
- Source format for X articles: 𝕏/@username (e.g., 𝕏/@stable_summit).
