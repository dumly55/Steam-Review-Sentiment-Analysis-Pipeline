-- Applied by `python -m steam_analyzer initdb`. Every statement is safe to rerun.
-- All timestamps are UTC (timestamptz).

CREATE TABLE IF NOT EXISTS games (
    app_id  INTEGER PRIMARY KEY,
    name    TEXT NOT NULL
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
    timeline_snapshot   JSONB NOT NULL    -- every release and its time, not only the selected ones
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
    PRIMARY KEY (run_id, update_id)
);
