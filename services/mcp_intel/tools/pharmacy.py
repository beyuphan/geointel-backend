import asyncio
from playwright.async_api import async_playwright
import re
import unicodedata
from loguru import logger as log

class PharmacyScraper:
    def __init__(self):
        self.base_url = "https://www.eczaneler.gen.tr/nobetci-{}"

    def _slugify(self, text: str) -> str:
        """Türkçe karakterleri URL dostu hale getirir."""
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

    def _extract_coords(self, map_url):
        if not map_url: return None
        try:
            match = re.search(r'q=([\d\.]+),([\d\.]+)', map_url)
            if match:
                return f"{match.group(1)}, {match.group(2)}"
        except: return None
        return None

    async def get_pharmacies(self, city: str, district: str = ""):
        city_slug = self._slugify(city)
        target_district_slug = self._slugify(district).replace("-", " ") if district else ""
        
        url = self.base_url.format(city_slug)
        log.info(f"💊 [ECZANE] Hedef: {url} (İlçe Filtresi: {target_district_slug or 'YOK'})")
        
        results = []
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if resp.status != 200:
                    log.error(f"❌ [HTTP] Eczane sitesi hatası: {resp.status}")
                    return []

                # Tablo kontrolü
                try:
                    await page.wait_for_selector("tbody tr", state="attached", timeout=5000)
                except:
                    log.warning(f"⚠️ [DOM] Tablo bulunamadı! Sayfa yapısı değişmiş olabilir.")
                    return []

                # JS Parsing
                all_pharmacies = await page.evaluate("""() => {
                    const results = [];
                    let rows = document.querySelectorAll('div.tab-pane.active tbody tr');
                    if (rows.length === 0) { rows = document.querySelectorAll('tbody tr'); }
                    
                    rows.forEach(row => {
                        const rowContainer = row.querySelector('.row');
                        if (!rowContainer) return;
                        const name = rowContainer.querySelector('span.isim')?.innerText.trim();
                        const district = rowContainer.querySelector('.bg-info')?.innerText.trim() || "";
                        const addressDiv = rowContainer.querySelector('.col-lg-6');
                        let address = "";
                        if (addressDiv) {
                            let rawAddress = addressDiv.innerText.trim();
                            address = rawAddress.replace(district, '').trim().replace(/\\s+/g, ' ').trim();
                        }
                        const phone = rowContainer.querySelector('.col-lg-3.py-lg-2')?.innerText.trim();
                        const mapLink = rowContainer.querySelector('a[href*="maps"]')?.href;
                        if (name) { results.push({ name, district, address, phone, mapLink }); }
                    });
                    return results;
                }""")
                
                log.info(f"📊 [HAM] Siteden {len(all_pharmacies)} eczane çekildi. Filtreleniyor...")

                for p_data in all_pharmacies:
                    # İlçe eşleştirme (normalize edilmiş haliyle)
                    current_district = self._slugify(p_data['district']).replace("-", " ")
                    
                    if target_district_slug and target_district_slug not in current_district:
                        continue
                    
                    coords = self._extract_coords(p_data['mapLink'])
                    results.append({
                        "isim": p_data['name'],
                        "adres": p_data['address'],
                        "tel": p_data['phone'] or "-",
                        "ilce": p_data['district'],
                        "koordinat": coords
                    })

            except Exception as e:
                log.error(f"🔥 [ECZANE PATLADI] Hata: {e}")
            finally:
                await browser.close()
            
        log.success(f"✅ [SONUÇ] {len(results)} eczane bulundu.")
        return results

# --- HANDLER ---
async def get_pharmacies_handler(city: str, district: str = "") -> list:
    scraper = PharmacyScraper()
    data = await scraper.get_pharmacies(city, district)
    if not data:
        return [{"error": f"{city} {district} için nöbetçi eczane bulunamadı. Veri kaynağı yanıt vermiyor."}]
    return data