# Source findings (Phase 1)

Verified against live Helldivers 2 (app 553850) data on **2026-10-14**. Re-run `python -m steam_analyzer smoke` to check these still hold; Steam can change undocumented endpoints without notice.

## Steam reviews: `IUserReviewsService/GetAppReviews/v1`

- Works anonymously. Parameters go in a single `input_json` query parameter, with enum values as integers.
- The response is wrapped in `response`. Top-level keys: `query_summary`, `reviews`, `cursor`, `total_matching`. There is no `success` field, so check the HTTP status.
- **Sort by Recent (`filter=1`) to enumerate everything.** The default Helpful sort keeps widening its day range, so it never ends with an empty page.
- `filter_offtopic_activity` defaults to *excluding* review-bomb periods. We pass `false` so nothing is silently dropped.
- `purchase_type` defaults to Steam purchases only. We pass `1` (all purchase types).
- Cursor paging returned no overlapping reviews across two consecutive pages, newest first.
- `date_range_start`/`date_range_end` (Unix seconds) work. A one-day window from 2024 returned only reviews inside it, and `total_matching` reports the window's size. We still re-check timestamps locally.
- Review fields: `recommendationid`, `author.steamid`, `language`, `review`, `timestamp_created`, `timestamp_updated`, `voted_up`, `votes_up`, `votes_funny`, `weighted_vote_score` (a number), `comment_count`, `steam_purchase`, `received_for_free`, `written_during_early_access`, `primarily_steam_deck`, `refunded`, plus author playtime fields.
- Anonymous responses may be cached for up to 10 minutes. Rate limiting shows up as HTTP 429.

## Release catalog: `store.steampowered.com/events/ajaxgetadjacentpartnerevents/`

This endpoint is undocumented, but it is what the Steam store's own news page uses. It is a better catalog source than `ISteamNews/GetNewsForApp` because it carries the event type, the comment-thread ID and edit times.

- Params: `appid`, `count_before=0`, `count_after=N`, and `gidevent=<gid>` to page further back. The anchor event is repeated in the next page, so de-duplicate.
- The **full history is reachable**: 164 events from 2023-09-22 to 2026-10-13 in 3 requests. That matches `GetNewsForApp`'s count of 164 for `steam_community_announcements`.
- **`event_type`**: `12` small update / patch notes (68), `13` regular update (18), `14` major update (7), `28` news (69). One event each of types 11 and 35 also appears.
- Real edge cases the catalog logic must handle:
  - Steam's `patchnotes` tag in GetNewsForApp is **unreliable**. 7.1.0, 6.2.5 and 6.0.1 lack it, so use `event_type`, not tags.
  - **Duplicate posts for one release**: "Devoid of Liberty: 7.0.0" and "HELLDIVERS 2 Devoid of Liberty Update Out Now" are both type 14 on 2026-08-12.
  - **Advance announcements** that must not create a boundary, e.g. "Optimizing Liberty - Incoming Patch: 27th May", "Patch Delay Update" and "Scheduled Maintenance".
- Each event has three distinct IDs: event `gid`, `announcement_body.gid` (the news gid differs again), and **`announcement_body.forum_topic_id`, which is the comment thread**. None of them are interchangeable.
- Time fields include `rtime32_start_time` (when the event starts), `announcement_body.posttime` and `announcement_body.updatetime`. A later `updatetime` means the patch notes were edited after posting.
- The comment count is in **`announcement_body.commentcount`**. The event-level `comment_count` is always 0.

## Announcement comments: `steamcommunity.com/app/{appid}/eventcomments/{forum_topic_id}/?ctp={page}`

- The public HTML page is reachable anonymously. It shows 15 comments per page, **oldest first**, and the embedded `InitializeCommentThread(...)` JSON has `total_count`.
- The 7.1.1 thread collected **118 of 118** comments over 8 pages with no duplicates. Page 9 is empty, which is a reliable stop signal.
- Per comment we get `id="comment_<id>"`, the author's 32-bit account ID in `data-miniprofile` (steamid64 = 76561197960265728 + id), and the creation time in the `commentthread_comment_timestamp` element's `data-timestamp`.
- **Edits are visible**: a `forum_audit` "Last edited by …" block carries its own `data-timestamp` (8 of 118 comments).
- Replies can quote other comments in a `<blockquote>`. Quoted text is stripped so it isn't attributed to the replier.
- The JSON render endpoint `/comment/ForumTopic/render/...` returns `"This profile is private."` to anonymous requests. **We do not work around this**; the public HTML pages are enough.
- Comments keep arriving long after the window ends (the 7.1.1 thread had one 17 days later), so the window filter matters.

## Local AI: Ollama

- Ollama 0.40.4 with `qwen3:8b` (Q4_K_M) runs **fully on the GPU**: 5.6 GB of the RTX 3080's 10 GB VRAM.
- Throughput is about 80 output tokens/s. A short classification takes about 2.5 s warm; the first call takes about 48 s while the model loads.
- `"think": false` and `format=<JSON schema>` produce valid schema-shaped JSON.
- ⚠️ **Not deterministic**: the same review got `mixed` on one run and `negative` on the next, even at temperature 0. Pin `seed`, cache results per input version, and validate outputs.

## Release catalog (Phase 2)

Checked on the full history (168 announcements, 2023-09-22 to 2026-10-29) on **2026-11-02**. A trimmed real snapshot is in `tests/fixtures/helldivers2_events_2026-11-02.json`.

- **95 releases and 72 other announcements.** All 96 update-type events (12/13/14) are real patches; one is a duplicate post.
- The news API's announcement count (168) matched the events feed (168), so the count works as a coverage cross-check.
- **Store page links need the event gid**: `store.steampowered.com/news/app/553850/view/<event gid>`. The announcement gid opens a generic news hub. Community links (`/announcements/detail/<announcement gid>`) take the announcement gid.
- Version strings are unreliable as identifiers. "01.002.203" titles two releases four months apart, and "HOT FIX 01.001.001" follows 1.001.100.
- "Into the Unjust - Out Now!" (type 14, no version) is the only post for that release, so version-less release posts cannot simply be dropped.
- **Late patch-note edits are common.** 35 of the 96 release posts were edited after posting, and 4 were edited after the next release shipped (e.g. 4.1.2 after 5.0.0). 7.0.0 was edited three weeks after release.
- `rtime32_start_time` differs from the post time by more than 5 minutes on 3 release posts, by up to 135 minutes (6.3.0). Neither is documented as the moment the patch went live; the post time is used and labeled, and `config/release_overrides.json` takes corrections.
- PostgreSQL returns `timestamptz` values in the session's local time zone. All display code converts to UTC explicitly.
