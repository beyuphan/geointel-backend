import asyncio
import sys
import os

# Python yoluna ana dizini ekle ki modülleri bulabilsin
sys.path.append("/app")

from tools.here import get_route_data_handler

async def main():
    print("\n" + "="*50)
    print("🚀 HİBRİT ROTA TESTİ BAŞLIYOR")
    print("="*50)

    # SENARYO 1: İSTANBUL İÇİ (Yerel DB Testi)
    origin = "Beşiktaş Meydan"
    destination = "Maslak İTÜ"
    
    print(f"\n🏙️  Test 1: {origin} -> {destination}")
    print("    (Beklenti: 'GeoIntel_Local_DB' kaynaklı sonuç)")
    
    result = await get_route_data_handler(origin, destination)
    
    if "error" in result:
        print(f"❌ HATA: {result['error']}")
    else:
        print(f"✅ SONUÇ BAŞARILI!")
        print(f"   📍 Kaynak: {result.get('source')}")
        print(f"   📏 Mesafe: {result.get('mesafe_km')} km")
        print(f"   ⏱️ Süre:   {result.get('sure_dk')} dk")
        print(f"   📝 Not:    {result.get('not')}")

    # SENARYO 2: ŞEHİRLERARASI (HERE API Testi)
    origin_long = "İstanbul"
    dest_long = "Ankara"
    
    print(f"\n🌍 Test 2: {origin_long} -> {dest_long}")
    print("    (Beklenti: 'HERE_Maps_API' kaynaklı sonuç)")
    
    result_long = await get_route_data_handler(origin_long, dest_long)
    
    if "error" in result_long:
        print(f"❌ HATA: {result_long['error']}")
    else:
        print(f"✅ SONUÇ BAŞARILI!")
        print(f"   📍 Kaynak: {result_long.get('source')}")
        print(f"   📏 Mesafe: {result_long.get('mesafe_km')} km")
        print(f"   ⏱️ Süre:   {result_long.get('sure_dk')} dk")

    print("\n" + "="*50)

if __name__ == "__main__":
    asyncio.run(main())