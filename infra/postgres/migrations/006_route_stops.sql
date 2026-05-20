-- ============================================================
-- GeoIntel DB Migration: Phase 6 - Route Stops
-- Dosya: infra/postgres/migrations/006_route_stops.sql
-- Açıklama: route_history tablosuna zenginleştirilmiş duraklar
--            (stops JSONB) eklenir. Her durak: lat, lon, name,
--            address, kind (origin|waypoint|fuel|food|rest|destination),
--            km. Geçmiş rota detayı timeline'da artık tüm durakları
--            (yakıt + yemek + mola) gösterebilir.
-- ============================================================

ALTER TABLE route_history
  ADD COLUMN IF NOT EXISTS stops JSONB;

DO $$
BEGIN
    RAISE NOTICE '✅ route_history.stops kolonu eklendi (zenginleştirilmiş duraklar).';
END $$;
