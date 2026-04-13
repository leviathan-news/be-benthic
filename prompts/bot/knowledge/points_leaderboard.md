keywords: points,leaderboard,ranking,score,category,poster,editor,voter,yapper,monthly,drop,earnings
---
Points & Leaderboard System:
- Four categories: Posters (article submissions), Editors (tag/approve actions),
  Voters (weighted by VP), Yappers (comment engagement).
- Each month's leaderboard is independent. Points accumulate within the calendar month.
- Monthly drops distribute SQUID based on leaderboard ranking per category.
- /points shows your current standings. /points <category> <month> <year> for specifics.
- Public leaderboard URL: /leaderboard?month=M&year=Y
- SQUID drops are staged via MonthlyDropManifest (draft → approved → executed).
  Drop batch runs monthly. Each recipient gets allocated SQUID via DropHistory records.
- Projected earnings during the month contribute to available balance (for tips/trades)
  with a safety margin that scales with day of month (5% early → 80% late).
- CONTRIBUTOR_FLOOR=20: caps any single user's share at 5% for projection calculations.
