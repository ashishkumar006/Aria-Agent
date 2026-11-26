# Channel tool catalogue

A proposed, diverse tool set per channel — what an agent should be able to do
once a channel's credentials are wired. **R** = read-only/safe, **W** = sends or
creates (needs approval), **D** = destructive, **A** = account/admin.

Existing tools are marked ✅. Everything else is proposed.

Legend for status: ✅ alive · ⚠️ configured but credential dead · ➖ not configured.

---

## A. Configured today

### Telegram — ✅
Auth: bot token (`TELEGRAM_BOT_TOKEN`). No OAuth.

| Tool | Kind | What |
|---|---|---|
| `send_telegram` ✅ | W | send text or a document to a chat |
| `telegram_get_me` | R | verify bot identity |
| `telegram_get_chat` | R | chat title/type/member count |
| `telegram_get_chat_member` | R | one member's status |
| `telegram_get_administrators` | R | who admins the chat |
| `telegram_get_updates` | R | poll recent updates (24h) |
| `telegram_get_webhook_info` | R | webhook status / pending count |
| `telegram_set_webhook` | A | register a webhook URL + secret |
| `telegram_delete_webhook` | A | drop the webhook |
| `telegram_send_photo` | W | send an image |
| `telegram_send_media_group` | W | send an album |
| `telegram_send_location` | W | send coordinates |
| `telegram_send_poll` | W | send a poll |
| `telegram_edit_message` | W | edit a bot message (≤48h) |
| `telegram_delete_message` | D | delete a bot message |
| `telegram_set_reaction` | W | react to a message |
| `telegram_pin_message` | W | pin/unpin |
| `telegram_create_invite_link` | W | expiring invite link |
| `telegram_ban_member` | W | ban/unban/restrict |
| `telegram_get_file` | R | resolve a file_id for download |

Gotchas: bot must be admin for moderation; privacy mode hides group text;
`file_id` is bot-specific; 30 msg/s global, 1/s per chat.

### Discord — ✅
Auth: bot token (`DISCORD_BOT_TOKEN`), header `Bot …`.

| Tool | Kind | What |
|---|---|---|
| `discord_message` ✅ | W | send to a channel id |
| `discord_get_me` | R | bot user |
| `discord_list_channels` | R | guild channels |
| `discord_get_channel` | R | channel metadata |
| `discord_read_messages` | R | history (`before/after/limit`) |
| `discord_read_message` | R | one message |
| `discord_send_embed` | W | rich embed |
| `discord_send_file` | W | attachment |
| `discord_edit_message` | W | edit |
| `discord_delete_message` | D | delete |
| `discord_add_reaction` | W | add/remove own reaction |
| `discord_typing` | W | typing indicator |
| `discord_create_thread` | W | thread from channel/message |
| `discord_create_invite` | W | invite link |
| `discord_get_member` | R | member info |
| `discord_list_guilds` | R | guilds the bot is in |

Gotchas: needs `MESSAGE_CONTENT` intent for content; 50 req/s global; 2,000 char
messages; permission overwrites can silently deny.

### Slack — ⚠️ (`invalid_auth`)
Auth: bot token `xoxb-…`.

| Tool | Kind | What |
|---|---|---|
| `slack_message` ✅ | W | post a message |
| `slack_history` ✅ | R | channel history |
| `slack_auth_test` | R | verify token |
| `slack_list_channels` | R | conversations.list |
| `slack_channel_info` | R | conversations.info |
| `slack_thread_replies` | R | conversations.replies |
| `slack_list_members` | R | conversations.members |
| `slack_create_channel` | W | conversations.create |
| `slack_invite` | W | conversations.invite |
| `slack_set_topic` | W | setTopic/setPurpose |
| `slack_open_dm` | W | conversations.open |
| `slack_post_ephemeral` | W | in-channel private message |
| `slack_update_message` | W | chat.update |
| `slack_delete_message` | D | chat.delete |
| `slack_schedule_message` | W | chat.scheduleMessage |
| `slack_list_users` | R | users.list |
| `slack_user_info` | R | users.info |
| `slack_upload_file` | W | files upload |
| `slack_list_files` | R | files.list |
| `slack_add_reaction` | W | reactions.add |
| `slack_pin_message` | W | pins.add |
| `slack_search_messages` | R | search.messages |
| `slack_add_reminder` | W | reminders.add |

Gotchas: bot must be in the channel for history; token rotation can be
mandatory; Tier 4 ~100/min; thread_ts must be the parent.

### Gmail — ⚠️ (401 / `invalid_grant`)
Auth: OAuth2 (`GMAIL_TOKEN` + refresh).

| Tool | Kind | What |
|---|---|---|
| `gmail_query` ✅ | R | list/read messages (`is:unread`, etc.) |
| `send_email` ✅ | W | send (optionally with a document) |
| `gmail_refresh_token` ✅ | A | renew the access token |
| `gmail_get_profile` | R | address, historyId, message count |
| `gmail_get_message` | R | one message (full/raw/metadata) |
| `gmail_get_attachment` | R | download an attachment |
| `gmail_list_threads` | R | thread list |
| `gmail_get_thread` | R | one thread |
| `gmail_list_labels` | R | labels |
| `gmail_create_label` | W | create a label |
   / `gmail_modify_labels` | W | add/remove labels |
| `gmail_trash_message` | W | move to trash (recoverable) |
| `gmail_untrash_message` | W | restore |
| `gmail_delete_message` | D | permanent delete |
| `gmail_create_draft` | W | draft |
| `gmail_send_draft` | W | send a draft |
| `gmail_list_drafts` | R | drafts |
| `gmail_set_vacation` | W | auto-reply |
| `gmail_list_filters` | R | filters |
| `gmail_create_filter` | W | filter rule |
| `gmail_watch` | A | push notifications (7-day expiry) |

Gotchas: unit-based quota (send=100, list=5); `messages.delete` is irreversible;
`watch` expires in 7 days.

### Google Calendar — ⚠️ (401 / `invalid_grant`)
Auth: OAuth2.

| Tool | Kind | What |
|---|---|---|
| `calendar_query` ✅ | R | list events in a window |
| `create_calendar_event` ✅ | W | create an event |
| `calendar_refresh_token` ✅ | A | renew the access token |
| `calendar_list_calendars` | R | calendarList.list |
| `calendar_get_event` | R | one event |
| `calendar_get_recurring_instances` | R | expanded recurrence |
| `calendar_update_event` | W | update (reschedule) |
| `calendar_delete_event` | D | delete |
| `calendar_move_event` | W | move to another calendar |
| `calendar_quick_add` | W | create from text ("lunch with Bob") |
| `calendar_freebusy` | R | availability |
| `calendar_list_acl` | R | sharing permissions |
| `calendar_share` | W | grant access |
| `calendar_settings` | R | user settings |

Gotchas: `timeZone` required for recurring; `sendUpdates` defaults can notify
everyone; `events.patch` costs 3 units.

### GitHub — ⚠️ (401)
Auth: PAT (`GITHUB_ACCESS_TOKEN`).

| Tool | Kind | What |
|---|---|---|
| `github_query` ✅ | R/W | list_repos, list_issues, get_issue, create_issue, search_code |
| `github_get_user` | R | authenticated user |
| `github_get_repo` | R | repo metadata |
| `github_read_file` | R | contents API |
| `github_write_file` | W | create/update (needs blob sha) |
| `github_delete_file` | D | delete (needs sha) |
| `github_list_issues` | R | issues (state/labels) |
| `github_get_issue` | R | issue + comments |
| `github_create_issue` | W | new issue |
| `github_update_issue` | W | edit/close |
| `github_comment_issue` | W | comment |
| `github_list_prs` | R | pull requests |
| `github_get_pr` | R | PR detail |
| `github_create_pr` | W | open a PR |
| `github_review_pr` | W | approve/request changes |
| `github_merge_pr` | W | merge |
| `github_list_workflows` | R | Actions runs |
| `github_dispatch_workflow` | W | trigger a workflow |
| `github_create_release` | W | release |
| `github_search_repos` | R | search |

Gotchas: updating a file needs the current blob `sha`; search has a 30/min
limit; GitHub App tokens expire in 1h.

### Notion — ✅
Auth: internal integration token (`NOTION_TOKEN`).

| Tool | Kind | What |
|---|---|---|
| `notion_query` ✅ | R/W | list_pages, get_page, create_page, query_database, append_text |
| `notion_search` | R | search by title |
| `notion_get_page` | R | page + properties |
| `notion_get_page_markdown` | R | page as markdown |
| `notion_create_page` | W | page under a parent |
| `notion_update_page` | W | properties/icon/archive |
| `notion_append_blocks` | W | append children |
| `notion_get_blocks` | R | child blocks |
| `notion_update_block` | W | edit a block |
| `notion_delete_block` | D | trash a block |
| `notion_query_database` | R | filter/sort rows |
| `notion_create_database` | W | database |
| `notion_update_database` | W | schema |
| `notion_list_users` | R | users/bot |
| `notion_comment` | W | comment |
| `notion_upload_file` | W | file upload |

Gotchas: page must be shared with the integration; 3 req/s; `in_trash` replaces
`archived` in newer API versions; 503 on write may mean committed.

---

## B. Not configured (wire credentials to enable)

### Twilio SMS — ➖ (`TWILIO_SID/AUTH/FROM`)
| Tool | Kind | What |
|---|---|---|
| `twilio_send_sms` | W | send SMS/MMS |
| `twilio_get_message` | R | status |
| `twilio_list_messages` | R | history |
| `twilio_list_numbers` | R | incoming numbers |
| `twilio_list_content_templates` | R | templates |
| `twilio_usage` | R | usage records |

### WhatsApp (Meta Cloud) — ➖ (`WA_TOKEN/PHONE_ID/VERIFY_TOKEN`)
| Tool | Kind | What |
|---|---|---|
| `whatsapp_send` | W | text/template/media |
| `whatsapp_send_template` | W | approved template |
| `whatsapp_list_templates` | R | template status |
| `whatsapp_create_template` | W | create |
| `whatsapp_get_phone` | R | quality/throughput |
| `whatsapp_upload_media` | W | media |
| `whatsapp_mark_read` | W | read receipt |

### Matrix — ➖ (`MATRIX_HOMESERVER/USER/PASSWORD`)
| Tool | Kind | What |
|---|---|---|
| `matrix_send` | W | send to a room |
| `matrix_join_room` | W | join |
| `matrix_list_rooms` | R | joined rooms |
| `matrix_read_messages` | R | room history |
| `matrix_room_state` | R | state events |
| `matrix_members` | R | members |
| `matrix_create_room` | W | create |
| `matrix_invite` | W | invite |
| `matrix_react` | W | reaction |
| `matrix_redact` | D | redact |
| `matrix_typing` | W | typing |
| `matrix_upload_media` | W | media |

### Teams / Graph — ➖ (`TEAMS_APP_ID/PASSWORD/TENANT`)
| Tool | Kind | What |
|---|---|---|
| `teams_list_chats` | R | chats |
| `teams_read_messages` | R | chat messages |
| `teams_send_message` | W | chat/channel message |
| `teams_reply` | W | reply |
| `teams_list_channels` | R | channels |
| `teams_list_teams` | R | joined teams |
| `teams_list_members` | R | members |
| `teams_send_mail` | W | send mail |

### LINE — ➖ (`LINE_CHANNEL_SECRET/ACCESS_TOKEN`)
| Tool | Kind | What |
|---|---|---|
| `line_push` | W | push to user/group/room |
| `line_reply` | W | reply to a webhook |
| `line_multicast` | W | up to 500 recipients |
| `line_broadcast` | W | all followers |
| `line_get_profile` | R | user profile |
| `line_list_followers` | R | follower ids |
| `line_quota` | R | monthly quota |
| `line_get_message_content` | R | download content |

### Signal (signal-cli) — ➖ (`SIGNAL_NUMBER`)
| Tool | Kind | What |
|---|---|---|
| `signal_send` | W | message/attachment |
| `signal_react` | W | reaction |
| `signal_list_contacts` | R | contacts |
| `signal_list_groups` | R | groups |
| `signal_receive` | R | fetch messages |
| `signal_typing` | W | typing |
| `signal_create_poll` | W | poll |
| `signal_remote_delete` | D | delete sent |

### IMAP/SMTP — ➖ (`IMAP_*`/`SMTP_*`)
| Tool | Kind | What |
|---|---|---|
| `imap_list_folders` | R | folders |
| `imap_search` | R | search |
| `imap_read_message` | R | read |
| `imap_set_flags` | W | seen/flagged |
| `imap_move` | W | move |
| `imap_append` | W | upload |
| `imap_expunge` | D | permanent remove |
| `smtp_send` | W | send mail |

---

## C. Cross-cutting (already present)

| Tool | Kind | What |
|---|---|---|
| `web_search` ✅ | R | Tavily search |
| `fetch_url` ✅ | R | fetch a page as markdown |
| `search_knowledge` ✅ | R | indexed Documents |
| `read_document` ✅ | R | one document |
| `read_artifact` ✅ | R | `art:` handle |
| `save_as_document` ✅ | W | file into Documents |
| `schedule_task` ✅ | W | reminder/scheduled run |
| `list_scheduled` / `cancel_scheduled` / `enable_scheduled` ✅ | R/W | manage |
| `whats_new` ✅ | R | change feed |
| `schedule_wake` ✅ | A | self-poll |
| `list_channels` ✅ | R | configured channels |
| `check_channel` ✅ | R | live credential test |

---

## Design rules for the agent surface

1. **Three approval tiers**: read (auto) · send (confirm) · destructive/admin (explicit + audited).
2. **Normalize targets**: each send tool takes a channel-specific target (`chat_id`, `channel_id`, `room_id`, `to`, `email`).
4. **Idempotency** where the platform supports it (Matrix `txnId`, Slack `client_msg_id`, Twilio `MessageSid`).
5. **Verify webhooks** with the platform's signature scheme; dedupe by event id.
6. **Never put tokens in URLs or logs**; refresh OAuth tokens via `refresh_tokens.py`.
7. **Pagination** is part of the contract (Slack cursor, Notion start_cursor, GitHub page, Gmail pageToken).
8. **Discover capabilities at runtime** (`getMe`, `auth.test`, `CAPABILITY`, token scopes) rather than assuming admin.