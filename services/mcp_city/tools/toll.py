import json
import os

# Veriyi yükleyen yardımcı fonksiyon (Dosya içinde gizli kalabilir)
def _load_toll_data():
    try:
        # Bir üst klasöre çık (tools -> mcp_city) sonra data'ya gir
        base_dir = os.path.dirname(os.path.dirname(__file__))
        file_path = os.path.join(base_dir, "data", "toll_prices.json")
        
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"error": f"Veri okunamadı: {str(e)}"}

async def get_toll_prices_handler(filter_region: str = None) -> str:
    """
    Köprü ve otoyol ücretlerini getirir.
    """
    data = _load_toll_data()
    if "error" in data:
        return data["error"]

    result_text = "🚗 **GÜNCEL GEÇİŞ ÜCRETLERİ (2026 Tahmini)**\n\n"
    
    # Köprüler
    result_text += "🌉 **KÖPRÜLER & TÜNELLER**\n"
    found = False
    for bridge in data.get("bridges", []):
        if filter_region and filter_region.lower() not in bridge["location"].lower():
            continue
        result_text += f"- **{bridge['name']}**: {bridge['price_tl']} TL ({bridge['direction']})\n"
        found = True

    # Otoyollar
    result_text += "\n🛣️ **OTOYOLLAR**\n"
    for highway in data.get("highways", []):
        if filter_region and filter_region.lower() not in highway["route"].lower():
            continue
        result_text += f"- **{highway['name']}**: {highway['price_tl']} TL ({highway['note']})\n"
        found = True
        
    if not found and filter_region:
        return f"❌ '{filter_region}' bölgesi için geçiş ücreti bulunamadı."

    return result_text