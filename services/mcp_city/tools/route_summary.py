"""
Rota Özet Kartı Oluşturucu
Rota hesaplandıktan sonra LLM'in kullanacağı kompakt özet metni üretir.
"""

def build_route_summary_handler(
    route_data: dict,
    radar_data: dict | None = None,
    toll_data: dict | None = None,
    weather_data: dict | None = None,
) -> dict:
    """
    Rota, radar, geçiş ücreti ve hava verilerini birleştirip
    kullanıcıya sunulacak Markdown özet kart döner.
    """
    lines = ["## 🗺️ Rota Özeti"]

    # Temel rota bilgisi
    mesafe = route_data.get("mesafe_km", "?")
    sure = route_data.get("sure_dk", "?")
    kaynak = route_data.get("source", "")

    lines.append(f"**📍 Mesafe:** {mesafe} km")
    if sure != "?":
        saat = int(float(sure)) // 60
        dakika = int(float(sure)) % 60
        if saat > 0:
            lines.append(f"**⏱️ Tahmini Süre:** {saat} sa {dakika} dk (canlı trafik dahil)")
        else:
            lines.append(f"**⏱️ Tahmini Süre:** {dakika} dk (canlı trafik dahil)")

    # Alternatif rotalar
    alts = route_data.get("alternatif_rotalar", [])
    if len(alts) > 1:
        lines.append(f"**🔀 Alternatif Rota:** {len(alts) - 1} alternatif mevcut")

    lines.append("")

    # Radar / Kamera bilgisi
    if radar_data:
        total_cameras = radar_data.get("total_count", 0)
        if total_cameras > 0:
            lines.append(f"**📷 Hız Kameraları:** {total_cameras} nokta tespit edildi — dikkatli sürüş!")
        else:
            lines.append("**📷 Hız Kameraları:** Kayıtlı aktif kamera yok ✅")

    # Geçiş ücreti
    if toll_data:
        total_toll = toll_data.get("total_toll_cost_tl", 0)
        toll_count = toll_data.get("toll_count", 0)
        if total_toll > 0:
            lines.append(f"**💳 HGS/OGS Ücreti:** Tahmini {total_toll:.2f} TL ({toll_count} geçiş)")
        else:
            lines.append("**💳 HGS/OGS:** Bu rotada ücretli geçiş yok ✅")

    # Hava durumu
    if weather_data:
        alerts = weather_data.get("alerts", [])
        conditions = weather_data.get("conditions", "")
        if alerts:
            lines.append(f"**🌩️ Hava Uyarısı:** {alerts[0]}")
        elif conditions:
            lines.append(f"**🌤️ Hava Durumu:** {conditions}")

    lines.append("")
    lines.append("---")
    lines.append("*Veriler HERE Maps'ten gerçek zamanlı alınmıştır.*")

    summary_text = "\n".join(lines)

    return {
        "summary_card": summary_text,
        "mesafe_km": mesafe,
        "sure_dk": sure,
        "radar_count": radar_data.get("total_count", 0) if radar_data else 0,
        "toll_tl": toll_data.get("total_toll_cost_tl", 0) if toll_data else 0,
        "has_weather_alert": bool(weather_data and weather_data.get("alerts")),
    }
