import asyncio
from playwright.async_api import async_playwright
import re
import unicodedata
from loguru import logger as log

class FuelScraper:
    FIRMS = ["opet", "petrol-ofisi", "total"]

    def __init__(self):
        # Listeleri init içinde tanımlayalım, garanti olsun.
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
        # Önce manuel düzeltme (Türkçe karakter belası için)
        text = text.replace("İ", "i").replace("I", "i").replace("ı", "i")
        text = text.lower()
        mapping = {
            'ç': 'c', 'ğ': 'g', 'ö': 'o', 'ş': 's', 'ü': 'u',
            ' ': '-'
        }
        for k, v in mapping.items():
            text = text.replace(k, v)
        
        # ASCII temizliği
        text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('utf-8')
        return text.strip()

    def _parse_price(self, price_str: str) -> float:
        if not price_str or price_str == "-" or "Veri" in price_str: return 0.0
        try:
            clean_str = re.sub(r'[^\d,]', '', price_str).replace(",", ".")
            return float(clean_str)
        except ValueError: return 0.0

    async def _get_firm_price_surgical(self, page, city, district, firm):
        # Slug işlemleri
        city_slug = self._slugify(city)
        district_slug = self._slugify(district)
        
        # LOG EKLEDİM: Bakalım neye çevirmiş?
        log.info(f"🔍 [SLUG KONTROL] Gelen: {city}/{district} -> Çevrilen: {city_slug}/{district_slug}")

        # 🔥🔥🔥 ZORLA İSTANBUL KONTROLÜ 🔥🔥🔥
        if "istanbul" in city_slug:
            if district_slug in self.ISTANBUL_AVRUPA:
                log.warning(f"📍 {district_slug} -> AVRUPA YAKASI tespit edildi.")
                city_slug = "istanbul-avrupa"
            elif district_slug in self.ISTANBUL_ANADOLU:
                log.warning(f"📍 {district_slug} -> ANADOLU YAKASI tespit edildi.")
                city_slug = "istanbul-anadolu"
            else:
                log.error(f"⚠️ {district_slug} İstanbul listelerinde YOK! Link hatalı olabilir.")

        url = f"https://www.doviz.com/akaryakit-fiyatlari/{city_slug}/{district_slug}/{firm}"
        log.info(f"🔗 [ISTEK] Gidiliyor: {url}")

        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            
            if resp.status != 200:
                log.error(f"❌ [HTTP] Sayfa hatası ({firm}): {resp.status}")
                return None

            has_table = await page.evaluate("() => document.querySelector('table tbody tr') !== null")
            if not has_table:
                # Doviz.com bazen boş sayfa dönüyor, tablo yoksa veri yoktur.
                log.warning(f"⚠️ [DOM] Tablo yok ({firm}).")
                return None

            raw_data = await page.evaluate("""() => {
                const row = document.querySelector('table tbody tr');
                if (!row) return null;
                const cells = row.querySelectorAll('td');
                return {
                    benzin: cells[1]?.innerText.trim(),
                    motorin: cells[2]?.innerText.trim(),
                    lpg: cells[3]?.innerText.trim()
                };
            }""")
            
            if raw_data:
                log.success(f"✅ [DOM] {firm}: {raw_data.get('benzin')} / {raw_data.get('motorin')}")
            return raw_data

        except Exception as e:
            log.error(f"🔥 [PATLADI] {url} -> Hata: {e}")
            return None

    async def get_district_prices(self, city: str, district: str) -> list:
        results = []
        log.info(f"🚀 [BASLAT] {city}/{district} Taraması Başlıyor...")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            
            for firm in self.FIRMS:
                await asyncio.sleep(1.0)
                raw = await self._get_firm_price_surgical(page, city, district, firm)
                
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
                            "ilce": district.capitalize()
                        })
            
            await browser.close()
        
        log.info(f"🏁 [BITIS] Toplam {len(results)} sonuç bulundu.")
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