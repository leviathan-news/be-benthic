keywords: yap,comment,inkling,reply,thread,sticky,engagement
---
Yaps (Comments) System:
- Yaps are comments on articles. /yap_<newsid> or /inkling_<newsid> to post.
- For unpublished articles: users can only have ONE yap (new yap overwrites old).
  Empty message deletes the yap entirely.
- For published articles: multiple yaps allowed per user.
- Yaps have engagement voting (upvotes, downvotes, net_votes).
- Tree shushing: deleting a yap also deletes all descendant replies.
- Duplicate detection via regex. Rate limiting per-user per-article.
- Sticky yap flag: operator can pin a yap to appear on all article dispatches.
- ln-agent (our news bot) writes TL;DR yaps and analysis comments on articles.
