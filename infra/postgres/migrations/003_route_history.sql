-- ============================================================
-- GeoIntel DB Migration: Phase 3 - Rota Geçmişi
-- Dosya: infra/postgres/migrations/003_route_history.sql
-- Tarih: 2026-03
-- Açıklama: route_history tablosunu mevcut DB'ye ekler.
--            init.sql'i yeniden çalıştırmak yerine sadece
--            bu migration'ı çalıştırın (idempotent).
-- ============================================================

-- Tabloyu oluştur (zaten varsa atla)
CREATE TABLE IF NOT EXISTS route_history (
    id            SERIAL PRIMARY KEY,
    user_id       UUID REFERENCES users(id) ON DELETE CASCADE,
    origin        VARCHAR(200) NOT NULL,
    destination   VARCHAR(200) NOT NULL,
    distance_km   NUMERIC(8,2) DEFAULT 0,
    duration_min  NUMERIC(8,1) DEFAULT 0,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- İndeksi oluştur (zaten varsa atla)
CREATE INDEX IF NOT EXISTS idx_route_history_user_date
    ON route_history (user_id, created_at DESC);

-- Tablo varlığını doğrula
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'route_history') THEN
        RAISE NOTICE '✅ route_history tablosu mevcut/oluşturuldu.';
    ELSE
        RAISE EXCEPTION '❌ route_history tablosu oluşturulamadı!';
    END IF;
END $$;
