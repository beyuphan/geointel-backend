# Araç Profili Güçlendirme - Veritabanı Değişikliği

Kullanıcı araç bilgilerini (marka, model, yıl, tüketim) daha detaylı tutmak istiyoruz.

```sql
-- existing user_vehicles table update
ALTER TABLE user_vehicles ADD COLUMN IF NOT EXISTS brand VARCHAR(50);
ALTER TABLE user_vehicles ADD COLUMN IF NOT EXISTS model VARCHAR(50);
ALTER TABLE user_vehicles ADD COLUMN IF NOT EXISTS year INTEGER;
ALTER TABLE user_vehicles ADD COLUMN IF NOT EXISTS city_consumption DECIMAL(5,2); -- Şehir içi (L/100km)
ALTER TABLE user_vehicles ADD COLUMN IF NOT EXISTS highway_consumption DECIMAL(5,2); -- Uzun yol (L/100km)
```
