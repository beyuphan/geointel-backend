-- ============================================================
-- GeoIntel DB Migration: Phase 7 - Day Plan History
-- Dosya: infra/postgres/migrations/007_day_plan_history.sql
-- Açıklama: Günlük plan schedule sonuçlarını persist eden tablo.
--            Hub'daki "geçmiş günlük planlar" listesi bunu okur.
-- ============================================================

CREATE TABLE IF NOT EXISTS day_plan_history (
    id              SERIAL PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at      TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc'),
    plan_date       VARCHAR(10) NOT NULL,
    city            VARCHAR(120),
    activity_note   TEXT,
    summary         TEXT,
    schedule        JSONB
);

CREATE INDEX IF NOT EXISTS day_plan_history_user_idx
    ON day_plan_history (user_id, created_at DESC);

DO $$
BEGIN
    RAISE NOTICE '✅ day_plan_history tablosu oluşturuldu.';
END $$;
