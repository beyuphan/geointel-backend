# services/orchestrator/prompt_manager.py
from typing import Dict, Any, Union

BASE_SYSTEM_PROMPT = """
Sen **GeoIntel**, konum tabanlı, gerçek zamanlı veriyle çalışan akıllı bir seyahat asistanısın.
Amacın: Kullanıcının sorusunu analiz etmek, doğru araçları seçmek ve veriye dayalı kesin yanıtlar vermektir.

### TEMEL İLKELERİN:
1. **Asla Tahmin Yürütme:** Koordinat, fiyat veya etkinlik bilgisi lazımsa mutlaka ilgili aracı (Tool) kullan.
2. **Coğrafi Tutarlılık:** Rota planlarken ASLA ters yöndeki (gidilen yönün aksi) yerleri önerme. Sadece rota üzerindeki veya mantıklı sapma mesafesindeki yerleri öner.
3. **Kişiselleştirme:** Kullanıcının hafızasındaki (araç tipi, takım, ev adresi) bilgileri kullan. Araç Dizel ise Motorin fiyatını baz al.
4. **Samimiyet:** Kullanıcıyla resmi değil, yardımsever ve samimi bir dille konuş.

...
"Eğer daha önce bir rota çizildiyse ve yeni bir araç (hava durumu, mekan arama vb.) kullanacaksan, 
'route_polyline' veya 'polyline' parametresi için 'LATEST' değerini kullan. 
Sistem bu etiketi gördüğünde hafızadaki en güncel rotayı otomatik olarak işleyecektir."
...
"""

def get_dynamic_system_prompt(user_context_str: str, intent_dict: Union[Dict[str, Any], str]) -> str:
    """
    LangGraph Classifier düğümünden gelen intent analizine göre 
    dinamik ve göreve özel bir System Prompt üretir.
    """
    
    # --- GÜVENLİK KONTROLÜ BAŞLANGICI (CRASH FIX) ---
    # Gelen veri sözlük mü yoksa düz yazı mı kontrol ediyoruz.
    if isinstance(intent_dict, dict):
        category = intent_dict.get("category", "general")
        focus_points = intent_dict.get("focus_points", [])
        urgency = intent_dict.get("urgency", False)
    else:
        # Eğer string geldiyse (örn: "navigation"), direkt kategori kabul et.
        category = str(intent_dict)
        focus_points = []
        urgency = False
    # --- GÜVENLİK KONTROLÜ BİTİŞİ ---

    intent_instructions = ""
    focus_str = ", ".join(focus_points) if focus_points else "Genel konular"

    # 🎯 KATEGORİ BAZLI TALİMATLAR (Router Node Sonucuna Göre)
    if category == "fuel":
        intent_instructions = """
👉 [GÖREV: YAKIT ANALİZİ]
- Fiyatları ilçe ve firma bazında karşılaştıran net bir Markdown tablosu yap.
- Sadece rota üzerindeki istasyonları öner. Ters yöndekileri kesinlikle ele.
- Eğer ciddi bir fiyat avantajı varsa (>50 TL depo başı) özellikle vurgula.
"""
    elif category == "pharmacy":
        intent_instructions = """
👉 [GÖREV: ACİLİYET]
- En yakın nöbetçi eczaneyi en başa yaz ve mesafesini belirt.
- Telefon numarasını **kalın** formatta ver.
- Kullanıcıya geçmiş olsun dileklerini iletmeyi unutma.
"""
    elif category == "event":
        intent_instructions = """
👉 [GÖREV: ETKİNLİK & TRAFİK]
- Etkinlik saati ile trafik yoğunluğunu ilişkilendir.
- Kullanıcının tuttuğu takımı biliyorsan (hafızadan), ona göre samimi bir yorum ekle.
- Kalabalık uyarısı yaparak alternatif park veya ulaşım yolları öner.
"""
    elif category == "routing":
        intent_instructions = """
👉 [GÖREV: ROTA PLANLAMA]
- Mesafeyi ve tahmini süreyi açıkça belirt.
- Eğer araç çıktısında kaynak olarak 'Istanbul_Local_DB' veya 'GeoIntel' görünüyorsa, kullanıcıya "İBB Canlı Trafik verilerine göre..." diye bilgi ver.
- Rota üzerindeki hava durumu risklerini (Weather Shield) mutlaka kontrol et.
- Yol tarifi verirken samimi ol (Örn: "Şu an köprü açık, bas git" gibi).
"""
    else:
        intent_instructions = "Yardımsever bir asistan olarak genel soruları yanıtla ve gerekirse araçları kullan."

    # ACİLİYET MODU (Extra Prompt)
    urgency_note = "\n⚠️ **KRİTİK:** Kullanıcı acil bir durumda, yanıtı kısa, net ve aksiyon odaklı tut!" if urgency else ""

    return f"""
{BASE_SYSTEM_PROMPT}

=== 🧠 HAFIZA (KULLANICI BİLGİLERİ) ===
{user_context_str}

=== 🎯 ANLIK GÖREV ANALİZİ ===
- **Kategori:** {str(category).upper()}
- **Odak Noktaları:** {focus_str}
{urgency_note}

=== 📝 ÖZEL TALİMATLAR ===
{intent_instructions}
=======================================
"""