import asyncio
import asyncpg
import json
import os

DB_DSN = "postgresql://user:password@geo_db:5432/geodb"

# ROTA: Beşiktaş Meydan -> Maslak (İTÜ Ayazağa)
START_POINT = (41.0425, 29.0075) 
END_POINT   = (41.1110, 29.0220) 

async def run_istanbul_test():
    print(f"🔌 Veritabanına bağlanılıyor...")
    conn = await asyncpg.connect(DB_DSN)

    print(f"📍 ROTA HESAPLANIYOR: Beşiktaş -> Maslak")

    # 1. EN YAKIN NOKTALARI BUL (Smart Snap)
    # Koordinatları en yakın yola "mıknatıs" gibi yapıştırıyoruz.
    snap_sql = """
    SELECT id FROM ways_vertices_pgr 
    ORDER BY the_geom <-> ST_SetSRID(ST_MakePoint($1, $2), 4326) LIMIT 1;
    """
    
    start_node = await conn.fetchval(snap_sql, START_POINT[1], START_POINT[0])
    end_node = await conn.fetchval(snap_sql, END_POINT[1], END_POINT[0])

    print(f"   ✅ Start Node: {start_node}")
    print(f"   🏁 End Node:   {end_node}")

    if not start_node or not end_node:
        print("❌ HATA: Başlangıç veya bitiş noktası harita sınırları dışında!")
        return

    # 2. ROTA HESAPLA FONKSİYONU
    async def get_route(mode_name, cost_type):
        print(f"🚗 {mode_name} hesaplanıyor...")
        
        # --- DÜZELTME BURADA ---
        # Eğer kriter MESAFE ise: Ters yön de aynı mesafedir.
        # Eğer kriter SÜRE ise: Ters yön farklı olabilir (Tek yönlü yol, trafik vs.)
        if cost_type == "length_m":
            sql_cost = "length_m as cost"
            sql_reverse = "length_m as reverse_cost" # Mesafe her iki yönde eşittir
        else:
            sql_cost = "cost_time as cost"
            sql_reverse = "reverse_cost_time as reverse_cost" # Süre yöne göre değişir

        sql = f"""
        SELECT sum(b.length_m) as dist, sum(b.cost_time) as time, 
               ST_AsGeoJSON(ST_Union(b.the_geom)) as geom
        FROM pgr_dijkstra(
            'SELECT gid as id, source, target, {sql_cost}, {sql_reverse} FROM ways',
            $1::bigint, $2::bigint, directed := true
        ) a
        JOIN ways b ON (a.edge = b.gid);
        """
        return await conn.fetchrow(sql, start_node, end_node)

    # A) EN KISA (Sadece Mesafeye Bakar)
    r_short = await get_route("EN KISA (Mesafe)", "length_m")
    
    # B) EN HIZLI (Canlı Trafik Verisine Bakar)
    r_fast = await get_route("EN HIZLI (Canlı Trafik)", "cost_time")

    # 3. SONUÇLARI YAZDIR VE KAYDET
    print("\n" + "="*50)
    print("📊 İSTANBUL TRAFİK RAPORU")
    print("="*50)

    features = []

    # En Kısa Yol (Mavi)
    if r_short and r_short['geom']:
        km = r_short['dist'] / 1000
        # Süreyi o anki trafik hızına göre biz hesaplayalım (Yaklaşık)
        print(f"📏 [EN KISA YOL]  Mesafe: {km:.2f} km")
        
        features.append({
            "type": "Feature",
            "properties": {
                "name": "En Kısa (Mesafe)", 
                "stroke": "#0000FF", # MAVİ
                "stroke-width": 4,
                "description": f"{km:.2f} km"
            }, 
            "geometry": json.loads(r_short['geom'])
        })
    else:
        print("❌ En kısa yol bulunamadı (Rota hesaplanamadı).")

    # En Hızlı Yol (Kırmızı)
    if r_fast and r_fast['geom']:
        km = r_fast['dist'] / 1000
        mins = r_fast['time'] / 60
        print(f"⚡ [CANLI TRAFİK] Mesafe: {km:.2f} km  |  Tahmini Süre: {mins:.1f} dk")
        
        features.append({
            "type": "Feature",
            "properties": {
                "name": "En Hızlı (Trafik)", 
                "stroke": "#FF0000", # KIRMIZI
                "stroke-width": 4,
                "description": f"{mins:.1f} dk"
            }, 
            "geometry": json.loads(r_fast['geom'])
        })
    else:
        print("❌ En hızlı yol bulunamadı (Trafik verisi eksik olabilir).")

    # GEOJSON KAYDET
    if features:
        geojson = {"type": "FeatureCollection", "features": features}
        
        # Docker içinde /app/data klasörüne yazıyoruz
        output_path = "/app/data/istanbul_route.geojson"
        with open(output_path, "w") as f:
            json.dump(geojson, f)
            
        print(f"\n✅ DOSYA OLUŞTURULDU: {output_path}")
        print("👉 Şimdi bu dosyayı bilgisayarına çekip geojson.io sitesine yükle.")
    
    await conn.close()

if __name__ == "__main__":
    asyncio.run(run_istanbul_test())