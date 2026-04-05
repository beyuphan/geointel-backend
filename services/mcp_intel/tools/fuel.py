"""
V3 Fuel Scraper — Playwright KALDIRILDI, httpx + regex ile 5x hızlı çalışır.
doviz.com'dan akaryakıt fiyatlarını çeker (OPET, Petrol Ofisi, Total).
"""
import asyncio
import httpx
import re
import unicodedata
from loguru import logger as log
from datetime import datetime


class FuelScraper:
    FIRMS = ["opet", "petrol-ofisi", "total"]
    BASE_URL = "https://www.doviz.com/akaryakit-fiyatlari"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.5"
    }

    def __init__(self):
        self.ISTANBUL_AVRUPA = [
            "arnavutkoy", "avcilar", "bagcilar", "bahcelievler", "bakirkoy", 
            "basaksehir", "bayrampasa", "besiktas", "beylikduzu", "beyoglu", 
            "buyukcekmece", "catalca", "esenler", "esenyurt", "eyupsultan", "eyup",
            "fatih", "gaziosmanpasa", "gungoren", "kagithane", "kucukcekmece", 
            "sariyer", "silivri", "sultangazi", "sisli", "zeytinburnu"
        ]
        
        self.ISTANBUL_ANADOLU = [
            "adalar", "atasehir", "beykoz", "cekmekoy", "kadikoy", "kartal", 
            "maltepe", "pendik", "sancaktepe", "sultanbeyli", "sile", "tuzla", 
            "umraniye", "uskudar"
        ]

    def _slugify(self, text: str) -> str:
        if not text: return ""
        text = text.replace("İ", "i").replace("I", "i").replace("ı", "i")
        text = text.lower()
        mapping = {'ç': 'c', 'ğ': 'g', 'ö': 'o', 'ş': 's', 'ü': 'u', ' ': '-'}
        for k, v in mapping.items():
            text = text.replace(k, v)
        text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('utf-8')
        return text.strip()

    def _parse_price(self, price_str: str) -> float:
        if not price_str or price_str == "-" or "Veri" in price_str: return 0.0
        try:
            clean_str = re.sub(r'[^\d,]', '', price_str).replace(",", ".")
            return float(clean_str)
        except ValueError: return 0.0

    def _parse_html_table(self, html: str) -> dict | None:
        """HTML'den tablo verisi çeker — Playwright yerine regex ile."""
        # İlk <tbody> <tr> içindeki <td> hücrelerini bul
        tbody_match = re.search(r'<tbody[^>]*>(.*?)</tbody>', html, re.DOTALL)
        if not tbody_match:
            return None
        
        first_row = re.search(r'<tr[^>]*>(.*?)</tr>', tbody_match.group(1), re.DOTALL)
        if not first_row:
            return None
        
        cells = re.findall(r'<td[^>]*>(.*?)</td>', first_row.group(1), re.DOTALL)
        if len(cells) < 4:
            return None
        
        # HTML tag'larını temizle
        clean = lambda x: re.sub(r'<[^>]+>', '', x).strip()
        
        return {
            "benzin": clean(cells[1]),
            "motorin": clean(cells[2]),
            "lpg": clean(cells[3]) if len(cells) > 3 else None,
        }

    async def _get_firm_price(self, client: httpx.AsyncClient, city: str, district: str, firm: str) -> dict | None:
        city_slug = self._slugify(city)
        district_slug = self._slugify(district)
        
        log.info(f"🔍 [SLUG] {city}/{district} -> {city_slug}/{district_slug}")

        if "istanbul" in city_slug:
            if district_slug in self.ISTANBUL_AVRUPA:
                city_slug = "istanbul-avrupa"
            elif district_slug in self.ISTANBUL_ANADOLU:
                city_slug = "istanbul-anadolu"
            else:
                log.warning(f"⚠️ {district_slug} İstanbul listelerinde YOK!")

        url = f"{self.BASE_URL}/{city_slug}/{district_slug}/{firm}"
        log.info(f"🔗 [GET] {url}")

        try:
            resp = await client.get(url, timeout=15.0)
            
            if resp.status_code != 200:
                log.error(f"❌ [HTTP] {firm}: {resp.status_code}")
                return None

            html = resp.text
            
            # Tarih kontrolü — sayfa güncel mi?
            bugun = datetime.now()
            bugun_str = bugun.strftime("%d.%m.%Y")
            months_tr = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", 
                        "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
            bugun_str_2 = f"{bugun.day} {months_tr[bugun.month - 1]} {bugun.year}"
            
            if bugun_str not in html and bugun_str_2 not in html:
                log.warning(f"⚠️ [ESKİ VERİ] {firm} sayfasında güncel tarih yok. Atlanıyor...")
                return None

            # HTML tablosunu parse et
            data = self._parse_html_table(html)
            if data:
                log.success(f"✅ [PARSE] {firm}: {data.get('benzin')} / {data.get('motorin')}")
            return data

        except Exception as e:
            log.error(f"🔥 [HATA] {url} -> {e}")
            return None

    async def get_district_prices(self, city: str, district: str) -> list:
        results = []
        log.info(f"🚀 [V3] {city}/{district} Taraması Başlıyor (httpx — Playwright YOK)...")
        
        # V3.5: Paylaşımlı httpx client ile paralel istek
        async with httpx.AsyncClient(headers=self.HEADERS, follow_redirects=True) as client:
            tasks = [self._get_firm_price(client, city, district, firm) for firm in self.FIRMS]
            raw_results = await asyncio.gather(*tasks)
            
            for i, raw in enumerate(raw_results):
                firm = self.FIRMS[i]
                if raw:
                    benzin = self._parse_price(raw.get('benzin'))
                    motorin = self._parse_price(raw.get('motorin'))
                    lpg = self._parse_price(raw.get('lpg'))
                    
                    if benzin > 10:
                        results.append({
                            "firma": firm.title(),
                            "benzin": benzin,
                            "motorin": motorin,
                            "lpg": lpg,
                            "ilce": district.capitalize(),
                            "city": city.capitalize()
                        })
        
        log.info(f"🏁 [V3] Toplam {len(results)} sonuç bulundu.")
        return results


# --- HANDLER ---
async def get_fuel_prices_handler(city: str, district: str) -> list:
    scraper = FuelScraper()
    try:
        data = await scraper.get_district_prices(city, district)
        if not data:
            log.error("❌ [HANDLER] Veri boş döndü.")
            return [{"error": f"{city}-{district} için veri bulunamadı. Lütfen sayı uydurma."}]
        return data
    except Exception as e:
        log.error(f"🔥 [CRITICAL] Handler hatası: {e}")
        return [{"error": "Sistem hatası oluştu."}]