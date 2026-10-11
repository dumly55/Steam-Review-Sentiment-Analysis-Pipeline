-- v0: starting point only.

CREATE TABLE IF NOT EXISTS games (
    app_id  INTEGER PRIMARY KEY,
    name    TEXT NOT NULL
);

INSERT INTO games (app_id, name)
VALUES (553850, 'Helldivers 2')
ON CONFLICT (app_id) DO NOTHING;
