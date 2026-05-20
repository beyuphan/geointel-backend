-- ============================================================
-- GeoIntel DB Migration: Phase 5 - Route History Extended
-- Dosya: infra/postgres/migrations/005_route_history_extended.sql
-- Açıklama: route_history tablosuna polyline, waypoints, etiket,
--            hava özeti, uyarılar ve LLM anlatımı kolonları ekler.
--            Geçmiş rotayı tıklayınca tam re-open mümkün olur.
-- ============================================================

ALTER TABLE route_history
  ADD COLUMN IF NOT EXISTS polyline_encoded TEXT,
  ADD COLUMN IF NOT EXISTS waypoints JSONB,
  ADD COLUMN IF NOT EXISTS waypoint_labels JSONB,
  ADD COLUMN IF NOT EXISTS label VARCHAR(120),
  ADD COLUMN IF NOT EXISTS weather_summary VARCHAR(200),
  ADD COLUMN IF NOT EXISTS warnings JSONB,
  ADD COLUMN IF NOT EXISTS narrative TEXT;

DO $$
BEGIN
    RAISE NOTICE '✅ route_history genişletildi (polyline, waypoints, label, narrative).';
END $$;
