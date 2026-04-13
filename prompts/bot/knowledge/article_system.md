keywords: article,headline,post,edit,edittext,editlink,editsource,editmedia,editx,tag,news,approve,submit,publish,source,revision
---
Article System:
- Articles flow: submitted → approved → live (or killed/retracted).
- /post <URL> creates a submission. The bot auto-generates a headline.
- Edit commands: /edittext_<id> <headline>, /editlink_<id> <url>, /editsource_<id> <source>,
  /editowner_<id> <owner> (format: tg:<username>-<telegram_id>),
  /editmedia_<id> (upload image as caption), /editx_<id> <text>, /tag_<id> <tags>.
- Every edit creates a NewsRevision audit record (who, what field, when).
- Source format for X/Twitter posts: 𝕏/@username (e.g., 𝕏/@stable_summit).
- publish_at field allows scheduled future publication (moves to 'scheduled' status).
- Approval uses atomic compare-and-swap (_transition_news_status) to prevent double-publish races.
- /suggest_headline <id> — AI-generates a headline suggestion for an article.
