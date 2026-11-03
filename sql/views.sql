-- Aggregation views, applied after schema.sql by `python -m steam_analyzer initdb`.
-- Views hold no data, so they are dropped and recreated on every initdb.
--
-- SQL is the authoritative place for every count, share and label. Thresholds and the feature alias map
-- come from each run's frozen config (runs.config -> analysis), so editing config/analysis.json never
-- changes an existing run. Each view states its grain; never join two views at different grains and
-- then count rows.

DROP VIEW IF EXISTS v_latest_update_results, v_update_reception, v_feature_reasons, v_source_reception,
    v_author_reactions, v_feature_authors, v_observations, v_run_feedback CASCADE;

-- Grain: one row per run membership (run, update, source, item).
CREATE VIEW v_run_feedback AS
SELECT rf.run_id, rf.update_id, rf.source_type, rf.source_item_id, rf.eligibility, rf.exclusion_reason,
       rf.analysis_status, rf.cache_hit, rf.assessment_id,
       fi.author_id, fi.created_at, fi.permalink,
       fv.version_id, fv.text, fv.voted_up, fv.language,
       a.overall_sentiment, a.update_relevance
FROM run_feedback rf
JOIN feedback_items fi ON fi.feedback_id = rf.feedback_id
LEFT JOIN feedback_versions fv ON fv.version_id = rf.version_id
LEFT JOIN ai_assessments a ON a.assessment_id = rf.assessment_id AND a.status = 'succeeded';

-- Grain: one row per feature observation of an eligible, successfully analyzed item.
-- tier: patch_related = explicit update reaction, or plausibly related with a linked patch change;
--       tentative = plausibly related without a linked change; general; unrelated.
CREATE VIEW v_observations AS
SELECT rf.run_id, rf.update_id, rf.source_type, rf.source_item_id, rf.author_id, rf.permalink, rf.created_at,
       COALESCE(r.config #>> ARRAY['analysis', 'feature_aliases', o.feature_key], o.feature_key) AS feature_key,
       o.category, o.sentiment, o.relevance, o.change_id, o.evidence,
       CASE WHEN o.relevance = 'explicit_update'
              OR (o.relevance = 'plausibly_related' AND o.change_id IS NOT NULL) THEN 'patch_related'
            WHEN o.relevance = 'plausibly_related' THEN 'tentative'
            WHEN o.relevance = 'general_game' THEN 'general'
            ELSE 'unrelated' END AS tier
FROM v_run_feedback rf
JOIN runs r ON r.run_id = rf.run_id
JOIN feature_observations o ON o.assessment_id = rf.assessment_id
WHERE rf.eligibility = 'eligible' AND rf.analysis_status = 'succeeded';

-- Grain: one row per (run, update, source, tier, feature, identifiable author).
-- An author counts once per feature however many times they mention it. Explicitly mixed, or both
-- positive and negative, is mixed; neutral/unclear never dilutes a clear positive or negative.
CREATE VIEW v_feature_authors AS
SELECT run_id, update_id, source_type, tier, feature_key, author_id,
       CASE WHEN bool_or(sentiment = 'mixed') OR (bool_or(sentiment = 'positive') AND bool_or(sentiment = 'negative'))
                 THEN 'mixed'
            WHEN bool_or(sentiment = 'positive') THEN 'positive'
            WHEN bool_or(sentiment = 'negative') THEN 'negative'
            ELSE 'neutral_unclear' END AS polarity,
       count(*) AS observations
FROM v_observations
WHERE author_id IS NOT NULL
GROUP BY run_id, update_id, source_type, tier, feature_key, author_id;

-- Grain: one row per (run, update, source, identifiable author) with an evaluative patch-related reaction.
-- Built from observation-level relevance, not the item's overall sentiment: general praise of the game
-- plus criticism of a patch change is a negative patch reaction.
CREATE VIEW v_author_reactions AS
SELECT run_id, update_id, source_type, author_id,
       CASE WHEN bool_or(polarity = 'mixed') OR (bool_or(polarity = 'positive') AND bool_or(polarity = 'negative'))
                 THEN 'mixed'
            WHEN bool_or(polarity = 'positive') THEN 'positive'
            ELSE 'negative' END AS reaction
FROM v_feature_authors
WHERE tier = 'patch_related' AND polarity <> 'neutral_unclear'
GROUP BY run_id, update_id, source_type, author_id;

-- Grain: one row per (run, update, source). Populations, coverage and the source label.
CREATE VIEW v_source_reception AS
WITH thresholds AS (
    SELECT run_id,
           (config #>> '{analysis,thresholds,label_min_qualifying_authors}')::int AS min_authors,
           (config #>> '{analysis,thresholds,label_min_coverage}')::numeric AS min_coverage,
           (config #>> '{analysis,thresholds,label_majority_share}')::numeric AS majority,
           (config #>> '{analysis,thresholds,label_min_margin}')::numeric AS margin
    FROM runs
), collection AS (
    SELECT run_id, update_id, source_type,
           CASE WHEN bool_and(status = 'complete') THEN 'complete'
                WHEN bool_and(status = 'unavailable') THEN 'unavailable'
                WHEN bool_or(status IN ('pending', 'collecting')) THEN 'collecting'
                ELSE 'partial' END AS collection_status,
           bool_or(capped) AS capped,
           count(*) AS channels,
           CASE WHEN bool_and(reported_total IS NOT NULL) THEN sum(reported_total) END AS reported_total,
           string_agg(NULLIF(detail, ''), '; ' ORDER BY run_source_id) AS collection_detail
    FROM run_sources
    GROUP BY run_id, update_id, source_type
), items AS (
    SELECT run_id, update_id, source_type,
           count(*) AS collected,
           count(*) FILTER (WHERE eligibility = 'excluded') AS excluded,
           count(*) FILTER (WHERE exclusion_reason = 'outside_window') AS excluded_outside_window,
           count(*) FILTER (WHERE exclusion_reason = 'edited_after_window') AS excluded_edited_after_window,
           count(*) FILTER (WHERE exclusion_reason = 'non_english') AS excluded_non_english,
           count(*) FILTER (WHERE exclusion_reason = 'empty_text') AS excluded_empty_text,
           count(*) FILTER (WHERE eligibility = 'eligible') AS eligible,
           count(*) FILTER (WHERE eligibility = 'eligible' AND language = 'uncertain') AS eligible_uncertain_language,
           count(*) FILTER (WHERE eligibility = 'eligible' AND analysis_status = 'succeeded') AS analyzed,
           count(*) FILTER (WHERE eligibility = 'eligible' AND analysis_status = 'failed') AS failed,
           count(*) FILTER (WHERE eligibility = 'eligible' AND analysis_status = 'pending') AS pending,
           count(*) FILTER (WHERE eligibility = 'eligible' AND cache_hit) AS cache_hits,
           count(DISTINCT author_id) FILTER (WHERE eligibility = 'eligible' AND analysis_status = 'succeeded')
               AS analyzed_authors,
           count(*) FILTER (WHERE eligibility = 'eligible' AND analysis_status = 'succeeded' AND author_id IS NULL)
               AS unknown_author_items,
           count(*) FILTER (WHERE eligibility = 'eligible' AND voted_up) AS recommended,
           count(*) FILTER (WHERE eligibility = 'eligible' AND voted_up IS NOT NULL) AS with_recommendation,
           count(*) FILTER (WHERE overall_sentiment = 'positive') AS text_positive,
           count(*) FILTER (WHERE overall_sentiment = 'negative') AS text_negative,
           count(*) FILTER (WHERE overall_sentiment = 'mixed') AS text_mixed,
           count(*) FILTER (WHERE overall_sentiment IN ('neutral', 'unclear')) AS text_neutral_unclear
    FROM v_run_feedback
    GROUP BY run_id, update_id, source_type
), reactions AS (
    SELECT run_id, update_id, source_type,
           count(*) FILTER (WHERE reaction = 'positive') AS positive_authors,
           count(*) FILTER (WHERE reaction = 'negative') AS negative_authors,
           count(*) FILTER (WHERE reaction = 'mixed') AS mixed_authors
    FROM v_author_reactions
    GROUP BY run_id, update_id, source_type
), combined AS (
    SELECT c.run_id, c.update_id, c.source_type, c.collection_status, c.capped, c.channels, c.reported_total,
           c.collection_detail,
           COALESCE(i.collected, 0) AS collected, COALESCE(i.excluded, 0) AS excluded,
           COALESCE(i.excluded_outside_window, 0) AS excluded_outside_window,
           COALESCE(i.excluded_edited_after_window, 0) AS excluded_edited_after_window,
           COALESCE(i.excluded_non_english, 0) AS excluded_non_english,
           COALESCE(i.excluded_empty_text, 0) AS excluded_empty_text,
           COALESCE(i.eligible, 0) AS eligible,
           COALESCE(i.eligible_uncertain_language, 0) AS eligible_uncertain_language,
           COALESCE(i.analyzed, 0) AS analyzed, COALESCE(i.failed, 0) AS failed,
           COALESCE(i.pending, 0) AS pending, COALESCE(i.cache_hits, 0) AS cache_hits,
           COALESCE(i.analyzed_authors, 0) AS analyzed_authors,
           COALESCE(i.unknown_author_items, 0) AS unknown_author_items,
           COALESCE(i.recommended, 0) AS recommended, COALESCE(i.with_recommendation, 0) AS with_recommendation,
           COALESCE(i.text_positive, 0) AS text_positive, COALESCE(i.text_negative, 0) AS text_negative,
           COALESCE(i.text_mixed, 0) AS text_mixed, COALESCE(i.text_neutral_unclear, 0) AS text_neutral_unclear,
           COALESCE(x.positive_authors, 0) AS positive_authors,
           COALESCE(x.negative_authors, 0) AS negative_authors,
           COALESCE(x.mixed_authors, 0) AS mixed_authors,
           t.min_authors, t.min_coverage, t.majority, t.margin
    FROM collection c
    JOIN thresholds t USING (run_id)
    LEFT JOIN items i USING (run_id, update_id, source_type)
    LEFT JOIN reactions x USING (run_id, update_id, source_type)
), judged AS (
    SELECT combined.*,
           positive_authors + negative_authors + mixed_authors AS qualifying_update_authors,
           round(analyzed::numeric / NULLIF(eligible, 0), 4) AS coverage,
           CASE WHEN collection_status <> 'complete' THEN 'collection ' || collection_status
                WHEN eligible = 0 THEN 'no eligible feedback'
                WHEN analyzed < min_coverage * eligible
                    THEN 'analysis coverage below ' || round(min_coverage * 100) || '%'
                WHEN positive_authors + negative_authors + mixed_authors < min_authors
                    THEN 'fewer than ' || min_authors || ' qualifying authors'
           END AS insufficient_reason
    FROM combined
)
SELECT run_id, update_id, source_type,
       CASE WHEN insufficient_reason IS NOT NULL THEN 'Insufficient feedback'
            WHEN positive_authors >= majority * qualifying_update_authors
             AND positive_authors - negative_authors >= margin * qualifying_update_authors THEN 'Positive'
            WHEN negative_authors >= majority * qualifying_update_authors
             AND negative_authors - positive_authors >= margin * qualifying_update_authors THEN 'Negative'
            ELSE 'Mixed' END AS label,
       insufficient_reason,
       collection_status, capped, channels, reported_total, collection_detail,
       collected, excluded, excluded_outside_window, excluded_edited_after_window, excluded_non_english,
       excluded_empty_text, eligible, eligible_uncertain_language, analyzed, failed, pending, cache_hits, coverage,
       analyzed_authors, unknown_author_items,
       qualifying_update_authors, positive_authors, negative_authors, mixed_authors,
       recommended, with_recommendation,
       round(recommended::numeric / NULLIF(with_recommendation, 0), 4) AS recommendation_share,
       text_positive, text_negative, text_mixed, text_neutral_unclear
FROM judged;

-- Grain: one row per (run, update, source, tier, feature).
-- share = distinct identifiable authors mentioning the feature / analyzed_authors of that source and window.
CREATE VIEW v_feature_reasons AS
WITH obs AS (
    SELECT run_id, update_id, source_type, tier, feature_key,
           mode() WITHIN GROUP (ORDER BY category) AS category,
           mode() WITHIN GROUP (ORDER BY change_id) AS change_id,
           count(*) AS observations,
           count(*) FILTER (WHERE author_id IS NULL) AS unknown_author_observations,
           (array_agg(evidence ORDER BY author_id IS NULL, created_at) FILTER (WHERE sentiment = 'positive'))[1]
               AS positive_example,
           (array_agg(permalink ORDER BY author_id IS NULL, created_at) FILTER (WHERE sentiment = 'positive'))[1]
               AS positive_example_link,
           (array_agg(evidence ORDER BY author_id IS NULL, created_at) FILTER (WHERE sentiment = 'negative'))[1]
               AS negative_example,
           (array_agg(permalink ORDER BY author_id IS NULL, created_at) FILTER (WHERE sentiment = 'negative'))[1]
               AS negative_example_link,
           (array_agg(evidence ORDER BY author_id IS NULL, created_at))[1] AS any_example,
           (array_agg(permalink ORDER BY author_id IS NULL, created_at))[1] AS any_example_link
    FROM v_observations
    GROUP BY run_id, update_id, source_type, tier, feature_key
), authors AS (
    SELECT run_id, update_id, source_type, tier, feature_key,
           count(*) AS authors,
           count(*) FILTER (WHERE polarity = 'positive') AS positive_authors,
           count(*) FILTER (WHERE polarity = 'negative') AS negative_authors,
           count(*) FILTER (WHERE polarity = 'mixed') AS mixed_authors,
           count(*) FILTER (WHERE polarity = 'neutral_unclear') AS neutral_unclear_authors
    FROM v_feature_authors
    GROUP BY run_id, update_id, source_type, tier, feature_key
), joined AS (
    SELECT o.*, COALESCE(a.authors, 0) AS authors,
           COALESCE(a.positive_authors, 0) AS positive_authors,
           COALESCE(a.negative_authors, 0) AS negative_authors,
           COALESCE(a.mixed_authors, 0) AS mixed_authors,
           COALESCE(a.neutral_unclear_authors, 0) AS neutral_unclear_authors,
           s.analyzed_authors,
           (r.config #>> '{analysis,thresholds,recurring_min_authors}')::int AS min_authors,
           (r.config #>> '{analysis,thresholds,recurring_min_share}')::numeric AS min_share
    FROM obs o
    LEFT JOIN authors a USING (run_id, update_id, source_type, tier, feature_key)
    JOIN v_source_reception s USING (run_id, update_id, source_type)
    JOIN runs r ON r.run_id = o.run_id
), passes AS (
    SELECT joined.*,
           positive_authors >= min_authors AND positive_authors >= min_share * analyzed_authors AS positive_recurs,
           negative_authors >= min_authors AND negative_authors >= min_share * analyzed_authors AS negative_recurs,
           mixed_authors >= min_authors AND mixed_authors >= min_share * analyzed_authors AS mixed_recurs,
           positive_authors + negative_authors + mixed_authors >= min_authors
               AND positive_authors + negative_authors + mixed_authors >= min_share * analyzed_authors AS evaluative_recurs,
           authors >= min_authors AND authors >= min_share * analyzed_authors AS mentions_recur
    FROM joined
)
SELECT run_id, update_id, source_type, tier, feature_key, category, change_id,
       authors, analyzed_authors, round(authors::numeric / NULLIF(analyzed_authors, 0), 4) AS share,
       positive_authors, negative_authors, mixed_authors, neutral_unclear_authors,
       observations, unknown_author_observations,
       CASE WHEN positive_recurs AND NOT negative_recurs AND NOT mixed_recurs THEN 'recurring_praise'
            WHEN negative_recurs AND NOT positive_recurs AND NOT mixed_recurs THEN 'recurring_criticism'
            WHEN evaluative_recurs THEN 'recurring_mixed'
            WHEN mentions_recur THEN 'recurring_neutral'
            ELSE 'limited' END AS support,
       positive_example, positive_example_link, negative_example, negative_example_link,
       any_example, any_example_link
FROM passes;

-- Grain: one row per (run, update). The overall label combines the two source labels.
CREATE VIEW v_update_reception AS
WITH src AS (
    SELECT run_id, update_id,
           max(label) FILTER (WHERE source_type = 'review') AS reviews_label,
           max(label) FILTER (WHERE source_type = 'comment') AS comments_label
    FROM v_source_reception
    GROUP BY run_id, update_id
), usable AS (
    SELECT src.*,
           reviews_label IS NOT NULL AND reviews_label <> 'Insufficient feedback' AS reviews_usable,
           comments_label IS NOT NULL AND comments_label <> 'Insufficient feedback' AS comments_usable
    FROM src
)
SELECT ru.run_id, ru.update_id, u.title, u.version, ru.window_start, ru.window_end, ru.window_status,
       r.cutoff_at AS as_of, r.status AS run_status, ru.notes_status, ru.notes_truncated,
       ru.simultaneous_release, snap.released_at_source,
       us.reviews_label, us.comments_label,
       CASE WHEN ru.window_status = 'uncertain' THEN 'Uncertain window'
            WHEN us.reviews_usable AND us.comments_usable THEN
                 CASE WHEN us.reviews_label = us.comments_label THEN us.reviews_label ELSE 'Mixed' END
            WHEN us.reviews_usable THEN us.reviews_label
            WHEN us.comments_usable THEN us.comments_label
            ELSE 'Insufficient feedback' END AS overall_label,
       CASE WHEN us.reviews_usable AND us.comments_usable THEN 'both sources'
            WHEN us.reviews_usable THEN 'provisional, reviews only'
            WHEN us.comments_usable THEN 'provisional, comments only' END AS basis,
       array_remove(ARRAY[
           CASE WHEN ru.window_status = 'ongoing' THEN 'Ongoing window: results are as of the run cutoff.' END,
           CASE WHEN ru.window_status = 'uncertain' THEN 'Uncertain window: ' || ru.window_note END,
           CASE WHEN ru.simultaneous_release
                THEN 'Released at the same time as another update; feedback cannot be attributed to only one.' END,
           CASE WHEN snap.released_at_source = 'announcement_published'
                THEN 'Release time is the announcement post time (no separate go-live time is published).' END,
           CASE WHEN ru.notes_status = 'edited_after_window'
                THEN 'Patch notes were edited after this window closed and no earlier copy exists; patch context is provisional.' END,
           CASE WHEN ru.notes_status = 'missing' THEN 'No patch notes were captured; patch context is missing.' END,
           CASE WHEN ru.notes_truncated
                THEN 'Patch notes were cut to fit the model input; changes after the cut were not available to it.' END
       ], NULL) AS caveats
FROM run_updates ru
JOIN runs r ON r.run_id = ru.run_id
JOIN updates u ON u.update_id = ru.update_id
LEFT JOIN usable us ON us.run_id = ru.run_id AND us.update_id = ru.update_id
LEFT JOIN LATERAL (
    SELECT e ->> 'released_at_source' AS released_at_source
    FROM jsonb_array_elements(r.timeline_snapshot) e
    WHERE (e ->> 'update_id')::bigint = ru.update_id
) snap ON TRUE;

-- Grain: one row per update: its most recent completed or partial run.
CREATE VIEW v_latest_update_results AS
SELECT DISTINCT ON (update_id) *
FROM v_update_reception
WHERE run_status IN ('completed', 'partial')
ORDER BY update_id, run_id DESC;
