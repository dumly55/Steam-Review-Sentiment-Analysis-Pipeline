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

## Collection and local analysis (Phase 3)

Checked on **2026-11-03** with a run on 7.1.1 (window 2026-09-24 09:00 UTC to 2026-10-20 09:01 UTC), capped at 150 items per source. The cap makes the reviews source partial by design, so this run checks the pipeline. It is **not** a finding about the update.

- **Comments, live**: the 7.1.1 thread was read over 9 requests (8 pages of 15, then an empty page). It collected 120 of the 120 comments Steam reported, posted from 2026-09-24 to 2026-10-23. The 2 posted after 7.2.0 shipped were excluded from 7.1.1's window, and 2 more were excluded as not English.
- **Reviews, live**: Steam reported 2,854 English reviews created in the window. Other windows: 3,195 for 7.2.1 and 1,345 so far for 7.2.2. Reviews are returned newest first, so the 150 capped reviews all came from the window's last 18 hours. That is a strongly biased sample, which is one more reason a capped source never gets a label.
- **Historical edits are real**: 10 of those 150 reviews had been edited after 7.2.0 released. They were excluded, because no earlier text had been captured.
- **Throughput**: `qwen3:8b` averaged about 1.7 s per review and 2.0 s per comment, warm, on the RTX 3080. The whole run (collection plus 255 analyses) took 10.5 minutes.
- **Validation failures**: 11 of 255 items (4.3%) failed after 3 attempts. Every failure was evidence that paraphrased or stitched together the text instead of quoting it, mostly in long items with many observations. They stay in the coverage denominator (95.7% for both sources).
- **Model behaviour seen in the outputs** (spot checks, not a measured accuracy):
  - Prompt v1 marked a review that said only "love it" as a reaction to the update, and marked bug reports and complaints about the developer as unrelated. Prompt v2 states these cases explicitly.
  - Prompt v2 still marks some wishlist comments on the announcement thread ("Add more planet types", "Remove intrusive fog") as explicit update feedback, and occasionally links an observation to the wrong change. On a rerun of the 7.1.1 comments it labeled 60 of 116 as explicit update feedback and only 2 as about the game in general.
  - Prompt v3 (2026-11-08) turns the relevance definition into an ordered checklist (off-topic first, then "does the text itself mention the update", then "could a listed change have affected it", then general) and says a comment is not about the update just because of where it was posted. On the same 255 items, the comments whose observations count toward the update label fell from 63 to 56; jokes such as "i love bugs!" and fix requests for things not in the notes moved out. Reviews were stable (14 and 14). Some errors remain in both directions: a long wishlist and a few fix demands still count as update feedback, and a few general opinions are now marked off-topic. These are spot checks by reading the output, not a measured accuracy.
  - Synonym keys sometimes split ("eac" and "easy_anticheat"), so the alias list in `config/analysis.json` grows from real output.
- Model accuracy has **not** been measured. Results from live runs are exploratory until a human-labeled sample has been compared with the model's output.

## Throughput tuning

Measured on **2026-11-08** by replaying 43 items from the 7.1.1 run (every sixth eligible item) through the same extraction code, each setting on a fresh `ollama serve`, warm, with nothing else using the GPU (RTX 3080, 10 GB, `qwen3:8b` Q4_K_M, `num_ctx` 12288).

| Ollama setting | Requests at once | s per item | Model in VRAM |
|---|---|---|---|
| Default (flash attention off) | 1 | 1.74 | 100% |
| `OLLAMA_FLASH_ATTENTION=1` | 1 | 1.62 | 100% |
| Flash attention, `OLLAMA_NUM_PARALLEL=2` | 2 | 1.20 | 93% |
| Flash attention, parallel 2, `OLLAMA_KV_CACHE_TYPE=q8_0` | 2 | 1.21 | 100% |
| Flash attention, parallel 4 (earlier pass, GPU shared with a game) | 4 | 4.09 | 64% |

- Two requests at once with flash attention cut the time per item by about 30%. Four do not fit in 10 GB at this context size and spill to the CPU, which is far slower.
- The quantized KV cache keeps the parallel-2 setup entirely in VRAM (8.0 GB instead of 9.2 GB) at the same speed, which leaves headroom for other GPU use.
- Labels did not move: every setting matched the original run on all overall sentiments and on 95–100% of update-relevance labels, the same spread as rerunning the default setting.
- Third attempts rescued 0 of 11 failing items in the 7.1.1 run while taking about a third of the time spent on failures, so extraction now stops after 2 attempts.
- A prompt asking for shorter evidence quotes removed retries (1.48 s per item) but matched only 87.5% of the original update-relevance labels against 95% for the default, so it was not adopted.
