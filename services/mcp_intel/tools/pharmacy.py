import httpx
from selectolax.lexbor import LexborHTMLParser
import re
import unicodedata
from loguru import logger as log

class PharmacyScraper:
    def __init__(self):
        self.base_url = "https://www.eczaneler.gen.tr/nobetci-{}"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }

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
        
        try:
            async with httpx.AsyncClient(headers=self.headers, follow_redirects=True, timeout=15.0) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    log.error(f"❌ [HTTP] Eczane sitesi hatası: {resp.status_code}")
                    return []
                
                parser = LexborHTMLParser(resp.text)
                # Sitede div.tab-pane.active tbody tr veya direkt tbody tr olabiliyor
                rows = parser.css('div.tab-pane.active tbody tr')
                if not rows:
                    rows = parser.css('tbody tr')
                
                if not rows:
                    log.warning(f"⚠️ [DOM] Tablo bulunamadı! Sayfa yapısı değişmiş olabilir.")
                    return []

                log.info(f"📊 [HAM] Siteden {len(rows)} satır bulundu. Filtreleniyor...")

                for row in rows:
                    row_container = row.css_first('.row')
                    if not row_container: continue

                    name_el = row_container.css_first('span.isim')
                    if not name_el: continue
                    name = name_el.text(strip=True)

                    district_el = row_container.css_first('.bg-info')
                    district_text = district_el.text(strip=True) if district_el else ""

                    # Adres çekimi
                    address_div = row_container.css_first('.col-lg-6')
                    address = ""
                    if address_div:
                        raw_address = address_div.text(strip=True)
                        address = raw_address.replace(district_text, '').strip()
                        address = re.sub(r'\s+', ' ', address)

                    phone_el = row_container.css_first('.col-lg-3.py-lg-2')
                    phone = phone_el.text(strip=True) if phone_el else "-"

                    map_link_el = row_container.css_first('a[href*="maps"]')
                    map_link = map_link_el.attributes.get('href') if map_link_el else ""

                    # İlçe eşleştirme
                    current_district_slug = self._slugify(district_text).replace("-", " ")
                    if target_district_slug and target_district_slug not in current_district_slug:
                        continue
                    
                    coords = self._extract_coords(map_link)
                    results.append({
                        "isim": name,
                        "adres": address,
                        "tel": phone,
                        "ilce": district_text,
                        "koordinat": coords
                    })

        except Exception as e:
            log.error(f"🔥 [ECZANE PATLADI] Hata: {e}")
            
        log.success(f"✅ [SONUÇ] {len(results)} eczane bulundu.")
        return results

# --- HANDLER ---
async def get_pharmacies_handler(city: str, district: str = "") -> list:
    scraper = PharmacyScraper()
    data = await scraper.get_pharmacies(city, district)
    if not data:
        return [{"error": f"{city} {district} için nöbetçi eczane bulunamadı. Veri kaynağı yanıt vermiyor."}]
    return data