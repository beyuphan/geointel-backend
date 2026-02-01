import asyncio
from playwright.async_api import async_playwright
import re
import unicodedata
from loguru import logger as log

class FuelScraper:
    # Test için firmaları azalttım, hızlı sonuç alalım. Sorun varsa hepsinde vardır zaten.
    FIRMS = ["opet", "petrol-ofisi", "total"] 

    def _slugify(self, text: str) -> str:
        if not text: return ""
        text = text.lower()
        mapping = {
            'ç': 'c', 'ğ': 'g', 'ı': 'i', 'ö': 'o', 'ş': 's', 'ü': 'u',
            'Ç': 'c', 'Ğ': 'g', 'İ': 'i', 'Ö': 'o', 'Ş': 's', 'Ü': 'u', ' ': '-'
        }
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

    async def _get_firm_price_surgical(self, page, city, district, firm):
        city_slug = self._slugify(city)
        district_slug = self._slugify(district)
        
        # Link yapısını logla
        url = f"https://www.doviz.com/akaryakit-fiyatlari/{city_slug}/{district_slug}/{firm}"
        log.info(f"🔗 [ISTEK] Gidiliyor: {url}")

        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            
            # Sayfa yüklendi mi?
            if resp.status != 200:
                log.error(f"❌ [HTTP] Sayfa hatası ({firm}): {resp.status}")
                return None

            # Tablo var mı kontrolü
            has_table = await page.evaluate("() => document.querySelector('table tbody tr') !== null")
            if not has_table:
                log.warning(f"⚠️ [DOM] Tablo bulunamadı! ({url}) - Sayfa yapısı farklı olabilir.")
                return None

            raw_data = await page.evaluate("""() => {
                const row = document.querySelector('table tbody tr');
                const cells = row.querySelectorAll('td');
                // Hücre içeriklerini loglamak için ham halini dönelim
                return {
                    benzin: cells[1]?.innerText.trim(),
                    motorin: cells[2]?.innerText.trim(),
                    lpg: cells[3]?.innerText.trim(),
                    html_dump: row.innerHTML // Debug için satırın HTML'ini alalım
                };
            }""")
            
            log.success(f"✅ [DOM] Veri çekildi ({firm}): {raw_data.get('benzin')} / {raw_data.get('motorin')}")
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
                await asyncio.sleep(0.5)
                raw = await self._get_firm_price_surgical(page, city, district, firm)
                
                if raw:
                    benzin = self._parse_price(raw.get('benzin'))
                    motorin = self._parse_price(raw.get('motorin'))
                    lpg = self._parse_price(raw.get('lpg'))

                    # Fiyat kontrolü: 43.18 gibi saçma fix değerler geliyorsa burada anlarız
                    # Ama doviz.com'da veri yoksa 0 döner.
                    
                    if benzin > 10:
                        results.append({
                            "firma": firm.title(),
                            "benzin": benzin,
                            "motorin": motorin,
                            "lpg": lpg,
                            "ilce": district.capitalize()
                        })
                    else:
                        log.warning(f"📉 [SKIP] Fiyat çok düşük veya 0: {benzin}")
            
            await browser.close()
        
        log.info(f"🏁 [BITIS] Toplam {len(results)} sonuç bulundu.")
        return results

# --- HANDLER ---
async def get_fuel_prices_handler(city: str, district: str) -> list:
    scraper = FuelScraper()
    data = await scraper.get_district_prices(city, district)
    
    # KANKA BURASI ÇOK ÖNEMLİ
    # Eğer liste boşsa, LLM uydurmasın diye ona açıkça hata dönüyoruz.
    if not data:
        log.error("❌ [HANDLER] Hiç veri bulunamadı! LLM uydurmasın diye hata dönüyorum.")
        return [{"error": f"{city}-{district} için güncel veri çekilemedi. Site yapısı değişmiş veya bağlantı hatası olabilir. Lütfen uydurma sayı verme."}]
    
    return data