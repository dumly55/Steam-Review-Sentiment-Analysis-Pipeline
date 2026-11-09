-- Applied by `python -m steam_analyzer initdb`. Every statement is safe to rerun.
-- All timestamps are UTC (timestamptz).

CREATE TABLE IF NOT EXISTS games (
    app_id          INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    is_synthetic    BOOLEAN NOT NULL DEFAULT FALSE    -- TRUE for the invented demo game
);

INSERT INTO games (app_id, name)
VALUES (553850, 'Helldivers 2')
ON CONFLICT (app_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Release catalog
-- ---------------------------------------------------------------------------

-- One row per attempt to read the full announcement history.
CREATE TABLE IF NOT EXISTS catalog_refreshes (
    refresh_id          BIGSERIAL PRIMARY KEY,
    app_id              INTEGER NOT NULL REFERENCES games (app_id),
    started_at          TIMESTAMPTZ NOT NULL,
    completed_at        TIMESTAMPTZ,
    status              TEXT NOT NULL CHECK (status IN ('running', 'complete', 'failed')),
    pages_fetched       INTEGER NOT NULL DEFAULT 0,
    events_seen         INTEGER NOT NULL DEFAULT 0,
    oldest_event_at     TIMESTAMPTZ,
    newest_event_at     TIMESTAMPTZ,
    news_feed_count     INTEGER,          -- announcement count from the news API, as a coverage cross-check
    error               TEXT
);

-- Every announcement event as Steam last returned it (releases and news alike).
CREATE TABLE IF NOT EXISTS source_events (
    event_gid           TEXT PRIMARY KEY,
    app_id              INTEGER NOT NULL REFERENCES games (app_id),
    event_type          INTEGER NOT NULL,
    title               TEXT NOT NULL,
    announcement_gid    TEXT,
    forum_topic_id      TEXT,             -- the comment thread; not the same as either gid
    published_at        TIMESTAMPTZ,
    event_start_at      TIMESTAMPTZ,
    body_updated_at     TIMESTAMPTZ,
    hidden              BOOLEAN NOT NULL DEFAULT FALSE,
    raw                 JSONB NOT NULL,
    first_seen_refresh  BIGINT NOT NULL REFERENCES catalog_refreshes (refresh_id),
    last_seen_refresh   BIGINT NOT NULL REFERENCES catalog_refreshes (refresh_id)
);
CREATE INDEX IF NOT EXISTS source_events_app_seen ON source_events (app_id, last_seen_refresh);

-- Immutable patch-note snapshots. A new row appears only when the text changes.
CREATE TABLE IF NOT EXISTS update_note_versions (
    note_version_id     BIGSERIAL PRIMARY KEY,
    event_gid           TEXT NOT NULL REFERENCES source_events (event_gid),
    body                TEXT NOT NULL,
    body_sha256         TEXT NOT NULL,
    source_updated_at   TIMESTAMPTZ,      -- Steam's last-edit time when this text was observed
    first_observed_at   TIMESTAMPTZ NOT NULL,
    UNIQUE (event_gid, body_sha256)
);

-- One row per real release. Rows are never deleted, so runs can keep referring to them.
CREATE TABLE IF NOT EXISTS updates (
    update_id           BIGSERIAL PRIMARY KEY,
    app_id              INTEGER NOT NULL REFERENCES games (app_id),
    primary_event_gid   TEXT NOT NULL UNIQUE REFERENCES source_events (event_gid),
    title               TEXT NOT NULL,
    version             TEXT,
    release_type        TEXT NOT NULL,
    released_at         TIMESTAMPTZ,
    released_at_source  TEXT NOT NULL CHECK (released_at_source IN ('announcement_published', 'manual_override', 'missing')),
    is_current          BOOLEAN NOT NULL DEFAULT TRUE   -- FALSE once the latest catalog no longer lists it as a release
);
CREATE INDEX IF NOT EXISTS updates_app_released ON updates (app_id, released_at);

-- Every announcement post that describes a release (the primary post plus any duplicates).
CREATE TABLE IF NOT EXISTS update_sources (
    update_id           BIGINT NOT NULL REFERENCES updates (update_id),
    event_gid           TEXT NOT NULL REFERENCES source_events (event_gid),
    role                TEXT NOT NULL CHECK (role IN ('primary', 'duplicate_post')),
    PRIMARY KEY (update_id, event_gid)
);

-- ---------------------------------------------------------------------------
-- Runs: the cutoff, configuration, timeline and windows are frozen at creation
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS runs (
    run_id              BIGSERIAL PRIMARY KEY,
    app_id              INTEGER NOT NULL REFERENCES games (app_id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    cutoff_at           TIMESTAMPTZ NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('queued', 'collecting', 'analyzing', 'aggregating',
                                                        'completed', 'partial', 'failed', 'cancelled')),
    catalog_refresh_id  BIGINT REFERENCES catalog_refreshes (refresh_id),
    config              JSONB NOT NULL,
    timeline_snapshot   JSONB NOT NULL,   -- every release and its time, not only the selected ones
    started_at          TIMESTAMPTZ,      -- first time a worker picked the run up
    finished_at         TIMESTAMPTZ,
    error               TEXT,
    cancel_requested_at TIMESTAMPTZ       -- set by Cancel; the worker stops at its next check
);

CREATE TABLE IF NOT EXISTS run_updates (
    run_id              BIGINT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
    update_id           BIGINT NOT NULL REFERENCES updates (update_id),
    window_start        TIMESTAMPTZ,
    window_end          TIMESTAMPTZ,
    window_status       TEXT NOT NULL CHECK (window_status IN ('closed', 'ongoing', 'uncertain')),
    window_note         TEXT NOT NULL DEFAULT '',
    next_update_id      BIGINT REFERENCES updates (update_id),
    simultaneous_release BOOLEAN NOT NULL DEFAULT FALSE,
    note_version_id     BIGINT REFERENCES update_note_versions (note_version_id),
    notes_status        TEXT NOT NULL CHECK (notes_status IN ('unedited', 'edited_during_window',
                                                              'edited_after_window', 'missing')),
    notes_context       TEXT,             -- the exact patch-note text the model sees, with [C#] change tags
    notes_truncated     BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (run_id, update_id)
);

-- The changes listed in a run's patch notes, so a [C#] reference can be shown with its text.
CREATE TABLE IF NOT EXISTS run_note_changes (
    run_id              BIGINT NOT NULL,
    update_id           BIGINT NOT NULL,
    change_id           TEXT NOT NULL,
    section             TEXT NOT NULL,
    text                TEXT NOT NULL,
    PRIMARY KEY (run_id, update_id, change_id),
    FOREIGN KEY (run_id, update_id) REFERENCES run_updates (run_id, update_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Feedback: one identity per Steam review or comment, plus every distinct version seen
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS feedback_items (
    feedback_id         BIGSERIAL PRIMARY KEY,
    app_id              INTEGER NOT NULL REFERENCES games (app_id),
    source_type         TEXT NOT NULL CHECK (source_type IN ('review', 'comment')),
    source_item_id      TEXT NOT NULL,    -- recommendationid or comment id
    author_id           TEXT,             -- steamid64; NULL when Steam does not show the author
    created_at          TIMESTAMPTZ NOT NULL,
    thread_id           TEXT,             -- comments: the announcement's forum_topic_id
    event_gid           TEXT,             -- comments: the announcement post that owns the thread
    permalink           TEXT NOT NULL,
    first_observed_at   TIMESTAMPTZ NOT NULL,
    UNIQUE (app_id, source_type, source_item_id)
);
CREATE INDEX IF NOT EXISTS feedback_items_created ON feedback_items (app_id, source_type, created_at);
CREATE INDEX IF NOT EXISTS feedback_items_author ON feedback_items (author_id);

-- A new row appears only when the text, rating or edit time changes, so earlier text stays available.
CREATE TABLE IF NOT EXISTS feedback_versions (
    version_id          BIGSERIAL PRIMARY KEY,
    feedback_id         BIGINT NOT NULL REFERENCES feedback_items (feedback_id),
    text                TEXT NOT NULL,
    text_sha256         TEXT NOT NULL,
    voted_up            BOOLEAN,          -- Steam recommendation; reviews only
    language            TEXT NOT NULL,    -- reviews: Steam's tag; comments: local check (english/non_english/uncertain)
    edited_at           TIMESTAMPTZ NOT NULL,  -- last edit Steam showed; equals created_at when never edited
    first_observed_at   TIMESTAMPTZ NOT NULL,
    last_observed_at    TIMESTAMPTZ NOT NULL,
    raw                 JSONB NOT NULL,   -- review JSON, or the comment's HTML block and page
    CONSTRAINT feedback_versions_identity UNIQUE NULLS NOT DISTINCT (feedback_id, text_sha256, voted_up, edited_at)
);

-- ---------------------------------------------------------------------------
-- Local-model assessments: a durable cache shared by all runs
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ai_assessments (
    assessment_id       BIGSERIAL PRIMARY KEY,
    cache_key           TEXT NOT NULL,    -- sha256 of every input below that can change the output
    app_id              INTEGER NOT NULL REFERENCES games (app_id),
    source_type         TEXT NOT NULL,
    source_item_id      TEXT NOT NULL,
    text_sha256         TEXT NOT NULL,
    context_sha256      TEXT NOT NULL,    -- instructions plus the patch notes given to the model
    model               TEXT NOT NULL,
    model_digest        TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,
    schema_version      TEXT NOT NULL,
    settings            JSONB NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    error               TEXT,
    attempts            INTEGER NOT NULL,
    overall_sentiment   TEXT CHECK (overall_sentiment IN ('positive', 'negative', 'mixed', 'neutral', 'unclear')),
    update_relevance    TEXT CHECK (update_relevance IN ('explicit_update', 'plausibly_related', 'general_game', 'unrelated')),
    uncertainty_reason  TEXT,
    input_truncated     BOOLEAN NOT NULL,
    raw_output          TEXT,             -- the model's last answer, kept to diagnose failures
    duration_ms         INTEGER,
    output_tokens       INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ai_assessments_one_success ON ai_assessments (cache_key) WHERE status = 'succeeded';

CREATE TABLE IF NOT EXISTS feature_observations (
    assessment_id       BIGINT NOT NULL REFERENCES ai_assessments (assessment_id) ON DELETE CASCADE,
    position            INTEGER NOT NULL,
    feature_key         TEXT NOT NULL,    -- normalized; the run's alias map is applied in the views
    category            TEXT NOT NULL,
    sentiment           TEXT NOT NULL CHECK (sentiment IN ('positive', 'negative', 'mixed', 'neutral', 'unclear')),
    relevance           TEXT NOT NULL CHECK (relevance IN ('explicit_update', 'plausibly_related', 'general_game', 'unrelated')),
    evidence            TEXT NOT NULL,    -- exact quote from the analyzed text
    change_id           TEXT,             -- the [C#] patch-note change it is about, when the model links one
    PRIMARY KEY (assessment_id, position)
);

-- ---------------------------------------------------------------------------
-- Run processing: per-source collection state and per-item membership
-- ---------------------------------------------------------------------------

-- One row per source of a run update: the reviews query, and one comment thread per announcement post.
CREATE TABLE IF NOT EXISTS run_sources (
    run_source_id       BIGSERIAL PRIMARY KEY,
    run_id              BIGINT NOT NULL,
    update_id           BIGINT NOT NULL,
    source_type         TEXT NOT NULL CHECK (source_type IN ('review', 'comment')),
    event_gid           TEXT,             -- comments: the announcement post; NULL for reviews
    thread_id           TEXT,             -- comments: forum_topic_id
    status              TEXT NOT NULL CHECK (status IN ('pending', 'collecting', 'complete', 'partial', 'unavailable')),
    next_cursor         TEXT,             -- resume point: review cursor or next comment page
    pages_fetched       INTEGER NOT NULL DEFAULT 0,
    items_collected     INTEGER NOT NULL DEFAULT 0,
    reported_total      INTEGER,          -- Steam's own count; NULL when it gave none
    capped              BOOLEAN NOT NULL DEFAULT FALSE,
    detail              TEXT NOT NULL DEFAULT '',
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    UNIQUE NULLS NOT DISTINCT (run_id, update_id, source_type, event_gid),
    FOREIGN KEY (run_id, update_id) REFERENCES run_updates (run_id, update_id) ON DELETE CASCADE
);

-- Every item a run collected for an update, the version it analyzed, and why anything was excluded.
CREATE TABLE IF NOT EXISTS run_feedback (
    run_id              BIGINT NOT NULL,
    update_id           BIGINT NOT NULL,
    source_type         TEXT NOT NULL,
    source_item_id      TEXT NOT NULL,
    feedback_id         BIGINT NOT NULL REFERENCES feedback_items (feedback_id),
    version_id          BIGINT REFERENCES feedback_versions (version_id),
    eligibility         TEXT NOT NULL DEFAULT 'pending' CHECK (eligibility IN ('pending', 'eligible', 'excluded')),
    exclusion_reason    TEXT CHECK (exclusion_reason IN ('outside_window', 'edited_after_window', 'non_english', 'empty_text')),
    analysis_status     TEXT NOT NULL DEFAULT 'pending'
                        CHECK (analysis_status IN ('pending', 'succeeded', 'failed', 'not_applicable')),
    assessment_id       BIGINT REFERENCES ai_assessments (assessment_id),
    cache_hit           BOOLEAN,
    PRIMARY KEY (run_id, update_id, source_type, source_item_id),
    FOREIGN KEY (run_id, update_id) REFERENCES run_updates (run_id, update_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS run_feedback_feedback ON run_feedback (feedback_id);

-- ---------------------------------------------------------------------------
-- Upgrades: columns added after a table was first created. No-ops on a new database.
-- ---------------------------------------------------------------------------

ALTER TABLE games ADD COLUMN IF NOT EXISTS is_synthetic BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS error TEXT,
    ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ;
ALTER TABLE run_updates
    ADD COLUMN IF NOT EXISTS notes_context TEXT,
    ADD COLUMN IF NOT EXISTS notes_truncated BOOLEAN NOT NULL DEFAULT FALSE;
