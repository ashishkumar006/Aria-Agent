# Tool spec — the 7 configured channels

Status at time of writing: **Telegram ✅ · Discord ✅ (token; 0 guilds) · Notion ✅ · GitHub ✅ · Slack ❌ (token type) · Gmail ❌ (re-auth) · Calendar ❌ (re-auth)**.

> **Implemented 2026-11-24** (live-verified): GitHub `github_get_user, github_list_repos, github_get_repo, github_read_file, github_list_issues, github_create_issue, github_comment_issue, github_list_prs, github_create_pr, github_search_repos`; Notion `notion_search, notion_get_page, notion_get_page_markdown, notion_get_blocks, notion_append_blocks, notion_query_database`; Telegram `telegram_get_me, telegram_get_updates, telegram_get_chat, telegram_send_message, telegram_edit_message, telegram_delete_message`; Discord `discord_get_me, discord_list_guilds, discord_list_channels, discord_invite_url`. All are in `CHANNEL_TOOLS`, so the action skill receives them when the goal names the channel.

Every tool below is `name(args) -> dict`, matching `mcp_server.py` style. **R** = read-only, **W** = sends/creates, **D** = destructive, **A** = admin/config. Tools marked ★ are the first to implement.

---

## Telegram ✅
Auth: `TELEGRAM_BOT_TOKEN`. Base `https://api.telegram.org/bot<token>/`.

| Tool | Kind | Signature | API |
|---|---|---|---|
| `telegram_get_me` ★ | R | `()` | `getMe` |
| `telegram_get_chat` ★ | R | `(chat_id)` | `getChat` |
| `telegram_get_chat_member` | R | `(chat_id, user_id)` | `getChatMember` |
| `telegram_get_administrators` | R | `(chat_id)` | `getChatAdministrators` |
| `telegram_get_updates` | R | `(offset=0, limit=100, timeout=0)` | `getUpdates` |
| `telegram_get_webhook_info` | R | `()` | `getWebhookInfo` |
| `telegram_send_message` ★ | W | `(chat_id, text, parse_mode?, reply_to?, thread_id?)` | `sendMessage` |
| `telegram_send_photo` | W | `(chat_id, photo, caption?)` | `sendPhoto` |
| `telegram_send_document` ★ | W | `(chat_id, document, caption?)` | `sendDocument` |
| `telegram_send_media_group` | W | `(chat_id, media[])` | `sendMediaGroup` |
| `telegram_send_location` | W | `(chat_id, latitude, longitude)` | `sendLocation` |
| `telegram_send_poll` | W | `(chat_id, question, options[], is_anonymous?)` | `sendPoll` |
| `telegram_edit_message` | W | `(chat_id, message_id, text)` | `editMessageText` |
| `telegram_delete_message` | D | `(chat_id, message_id)` | `deleteMessage` |
| `telegram_set_reaction` | W | `(chat_id, message_id, reaction[])` | `setMessageReaction` |
| `telegram_pin_message` | W | `(chat_id, message_id, disable_notification?)` | `pinChatMessage` |
| `telegram_create_invite_link` | W | `(chat_id, name?, expire_date?, member_limit?)` | `createChatInviteLink` |
| `telegram_ban_member` | W | `(chat_id, user_id, until_date?)` | `banChatMember` |
| `telegram_get_file` | R | `(file_id)` | `getFile` |
| `telegram_set_webhook` | A | `(url, secret_token?)` | `setWebhook` |

Gotchas: 30 msg/s global, 1/s per chat; bot must be admin for moderation; `file_id` is bot-specific.

---

## Discord ✅
Auth: `DISCORD_BOT_TOKEN` (header `Bot …`). Base `https://discord.com/api/v10`.

| Tool | Kind | Signature | API |
|---|---|---|---|
| `discord_get_me` ★ | R | `()` | `GET /users/@me` |
| `discord_list_channels` ★ | R | `(guild_id)` | `GET /guilds/{id}/channels` |
| `discord_get_channel` | R | `(channel_id)` | `GET /channels/{id}` |
| `discord_read_messages` ★ | R | `(channel_id, limit=50, before?, after?)` | `GET /channels/{id}/messages` |
| `discord_send_message` ★ | W | `(channel_id, content, embed?, reply_to?)` | `POST /channels/{id}/messages` |
| `discord_send_embed` | W | `(channel_id, title, description, color?, fields?)` | `POST …/messages` (embed) |
| `discord_send_file` | W | `(channel_id, content?, file)` | `POST …/messages` (multipart) |
| `discord_edit_message` | W | `(channel_id, message_id, content)` | `PATCH …/messages/{id}` |
| `discord_delete_message` | D | `(channel_id, message_id)` | `DELETE …/messages/{id}` |
| `discord_add_reaction` | W | `(channel_id, message_id, emoji)` | `PUT …/reactions/{emoji}/@me` |
| `discord_typing` | W | `(channel_id)` | `POST …/typing` |
| `discord_create_thread` | W | `(channel_id, name, message_id?)` | `POST …/threads` |
| `discord_create_invite` | W | `(channel_id, max_age?, max_uses?)` | `POST …/invites` |
| `discord_get_member` | R | `(guild_id, user_id)` | `GET /guilds/{id}/members/{uid}` |

Gotchas: `MESSAGE_CONTENT` intent for content; 50 req/s; 2,000 chars.

---

## Slack ❌ (fix token first)
Auth: `SLACK_BOT_TOKEN` must be `xoxb-…` (or `xoxe-…` access). Base `https://slack.com/api`.

| Tool | Kind | Signature | API |
|---|---|---|---|
| `slack_auth_test` ★ | R | `()` | `auth.test` |
| `slack_list_channels` ★ | R | `(types="public_channel,private_channel", limit=100)` | `conversations.list` |
| `slack_channel_info` | R | `(channel)` | `conversations.info` |
| `slack_history` ★ | R | `(channel, limit=50, latest?, oldest?)` | `conversations.history` |
| `slack_thread_replies` | R | `(channel, ts)` | `conversations.replies` |
| `slack_message` ★ | W | `(channel, text, thread_ts?, blocks?)` | `chat.postMessage` |
| `slack_post_ephemeral` | W | `(channel, user, text)` | `chat.postEphemeral` |
| `slack_update_message` | W | `(channel, ts, text)` | `chat.update` |
| `slack_delete_message` | D | `(channel, ts)` | `chat.delete` |
| `slack_schedule_message` | W | `(channel, post_at, text)` | `chat.scheduleMessage` |
| `slack_list_users` | R | `(limit=100)` | `users.list` |
| `slack_user_info` | R | `(user)` | `users.info` |
| `slack_upload_file` | W | `(channel, file, filename?)` | `files.getUploadURLExternal` + complete |
| `slack_add_reaction` | W | `(channel, timestamp, name)` | `reactions.add` |
| `slack_pin_message` | W | `(channel, timestamp)` | `pins.add` |
| `slack_search_messages` | R | `(query, count=20)` | `search.messages` |
| `slack_create_channel` | W | `(name, is_private=False)` | `conversations.create` |
| `slack_set_topic` | W | `(channel, topic?)` | `conversations.setTopic` |
| `slack_add_reminder` | W | `(text, time, user?)` | `reminders.add` |

Gotchas: bot must be in the channel for history; token rotation can be mandatory.

---

## Gmail ❌ (re-authorize)
Auth: OAuth2 (`GMAIL_TOKEN` + refresh).

| Tool | Kind | Signature | API |
|---|---|---|---|
| `gmail_get_profile` ★ | R | `()` | `users.getProfile` |
| `gmail_query` ★ | R | `(api_method, query="", max_results=10)` | `messages.list` / `messages.get` |
| `gmail_get_message` | R | `(message_id, format="full")` | `messages.get` |
| `gmail_get_attachment` | R | `(message_id, attachment_id)` | `messages.attachments.get` |
| `send_email` ★ | W | `(to, subject, body, document?)` | `messages.send` |
| `gmail_list_threads` | R | `(query="", max_results=10)` | `threads.list` |
| `gmail_get_thread` | R | `(thread_id)` | `threads.get` |
| `gmail_list_labels` | R | `()` | `labels.list` |
| `gmail_create_label` | W | `(name)` | `labels.create` |
| `gmail_modify_labels` | W | `(message_id, add[], remove[])` | `messages.modify` |
| `gmail_trash_message` | W | `(message_id)` | `messages.trash` |
| `gmail_untrash_message` | W | `(message_id)` | `messages.untrash` |
| `gmail_delete_message` | D | `(message_id)` | `messages.delete` |
| `gmail_create_draft` | W | `(to, subject, body)` | `drafts.create` |
| `gmail_send_draft` | W | `(draft_id)` | `drafts.send` |
| `gmail_set_vacation` | W | `(enable, response_body, start?, end?)` | `settings.updateVacation` |
| `gmail_watch` | A | `(topic_name)` | `users.watch` |

Gotchas: unit quota (send=100, list=5); `watch` expires in 7 days.

---

## Google Calendar ❌ (re-authorize)
Auth: OAuth2.

| Tool | Kind | Signature | API |
|---|---|---|---|
| `calendar_list_calendars` ★ | R | `()` | `calendarList.list` |
| `calendar_query` ★ | R | `(time_min?, time_max?, max_results=20)` | `events.list` |
| `calendar_get_event` | R | `(event_id, calendar_id="primary")` | `events.get` |
| `calendar_get_recurring_instances` | R | `(event_id, time_min?, time_max?)` | `events.instances` |
| `create_calendar_event` ★ | W | `(summary, start, end, description?, location?, timezone?)` | `events.insert` |
| `calendar_update_event` | W | `(event_id, summary?, start?, end?)` | `events.update` |
| `calendar_delete_event` | D | `(event_id)` | `events.delete` |
| `calendar_move_event` | W | `(event_id, destination)` | `events.move` |
| `calendar_quick_add` | W | `(text)` | `events.quickAdd` |
| `calendar_freebusy` | R | `(time_min, time_max)` | `freebusy.query` |
| `calendar_list_acl` | R | `(calendar_id="primary")` | `acl.list` |
| `calendar_share` | W | `(calendar_id, scope, role)` | `acl.insert` |

Gotchas: `timeZone` for recurring; set `sendUpdates` explicitly.

---

## GitHub ❌ (fix PAT)
Auth: `GITHUB_ACCESS_TOKEN` / `GITHUB_TOKEN`.

| Tool | Kind | Signature | API |
|---|---|---|---|
| `github_get_user` ★ | R | `()` | `GET /user` |
| `github_list_repos` ★ | R | `(limit=30)` | `GET /user/repos` |
| `github_get_repo` | R | `(owner, repo)` | `GET /repos/{o}/{r}` |
| `github_read_file` | R | `(owner, repo, path, ref?)` | `GET …/contents/{path}` |
| `github_write_file` | W | `(owner, repo, path, content, message, sha?)` | `PUT …/contents/{path}` |
| `github_delete_file` | D | `(owner, repo, path, sha, message)` | `DELETE …/contents/{path}` |
| `github_list_issues` ★ | R | `(owner, repo, state="open", limit=30)` | `GET …/issues` |
| `github_get_issue` | R | `(owner, repo, number)` | `GET …/issues/{n}` |
| `github_create_issue` ★ | W | `(owner, repo, title, body?, labels?)` | `POST …/issues` |
| `github_update_issue` | W | `(owner, repo, number, title?, body?, state?)` | `PATCH …/issues/{n}` |
| `github_comment_issue` | W | `(owner, repo, number, body)` | `POST …/issues/{n}/comments` |
| `github_list_prs` | R | `(owner, repo, state="open")` | `GET …/pulls` |
| `github_get_pr` | R | `(owner, repo, number)` | `GET …/pulls/{n}` |
| `github_create_pr` | W | `(owner, repo, title, head, base, body?)` | `POST …/pulls` |
| `github_review_pr` | W | `(owner, repo, number, event, body?)` | `POST …/pulls/{n}/reviews` |
| `github_merge_pr` | W | `(owner, repo, number, method="merge")` | `PUT …/pulls/{n}/merge` |
| `github_list_workflows` | R | `(owner, repo)` | `GET …/actions/runs` |
| `github_dispatch_workflow` | W | `(owner, repo, workflow_id, ref, inputs?)` | `POST …/actions/workflows/{id}/dispatches` |
| `github_create_release` | W | `(owner, repo, tag_name, name?, body?)` | `POST …/releases` |
| `github_search_repos` | R | `(query, limit=20)` | `GET /search/repositories` |

Gotchas: file update/delete needs blob `sha`; search 30/min.

---

## Notion ✅
Auth: `NOTION_TOKEN` (internal integration).

| Tool | Kind | Signature | API |
|---|---|---|---|
| `notion_search` ★ | R | `(query="", page_size=20)` | `POST /v1/search` |
| `notion_get_page` ★ | R | `(page_id)` | `GET /v1/pages/{id}` |
| `notion_get_page_markdown` | R | `(page_id)` | `GET /v1/pages/{id}/markdown` |
| `notion_list_pages` ★ | R | `()` | `POST /v1/search` (page filter) |
| `notion_create_page` ★ | W | `(parent, properties, children?)` | `POST /v1/pages` |
| `notion_update_page` | W | `(page_id, properties?, archived?)` | `PATCH /v1/pages/{id}` |
| `notion_get_blocks` | R | `(block_id, page_size=100)` | `GET /v1/blocks/{id}/children` |
| `notion_append_blocks` ★ | W | `(block_id, children[])` | `PATCH /v1/blocks/{id}/children` |
| `notion_update_block` | W | `(block_id, content)` | `PATCH /v1/blocks/{id}` |
| `notion_delete_block` | D | `(block_id)` | `DELETE /v1/blocks/{id}` |
| `notion_query_database` ★ | R | `(database_id, filter?, sorts?)` | `POST /v1/databases/{id}/query` |
| `notion_create_database` | W | `(parent, properties)` | `POST /v1/databases` |
| `notion_update_database` | W | `(database_id, properties?)` | `PATCH /v1/databases/{id}` |
| `notion_comment` | W | `(parent, text)` | `POST /v1/comments` |
| `notion_upload_file` | W | `(filename, content_type, data)` | `POST /v1/file_uploads` |

Gotchas: page must be shared with the integration; 3 req/s.

---

## Cross-cutting rules
- **Three approval tiers**: R auto · W confirm · D/A explicit + audit.
- **Idempotency** where supported (Matrix `txnId`, Slack `client_msg_id`, Twilio `MessageSid`).
- **Pagination** is part of every read tool (`limit`, `cursor`/`page_token`).
- **Discover capabilities at runtime** (`getMe`, `auth.test`, token scopes) rather than assuming admin.
- **Never log tokens**; refresh OAuth via `refresh_tokens.py` / the gateway UI.
- Existing tools to keep: `send_telegram`, `discord_message`, `slack_message`, `slack_history`, `send_email`, `gmail_query`, `github_query`, `notion_query`, `calendar_query`, `create_calendar_event`, `web_search`, `fetch_url`, `search_knowledge`, `read_document`, `save_as_document`, `schedule_task`, `whats_new`, `list_channels`, `check_channel`.