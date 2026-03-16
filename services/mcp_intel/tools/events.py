import asyncio
from playwright.async_api import async_playwright
import re
import unicodedata
from loguru import logger as log
from thefuzz import fuzz

class EventScraper:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
        self.sources = {
            "biletinial": "https://biletinial.com/tr-tr/etkinlikler/{}",
            "bubilet": "https://www.bubilet.com.tr/{}/etkinlikler"
        }

    def _normalize(self, text: str) -> str:
        """Fuzzy match için metni temizler"""
        if not text: return ""
        text = text.lower().replace('İ', 'i').replace('ı', 'i').replace('ğ', 'g').replace('ü', 'u').replace('ş', 's').replace('ö', 'o').replace('ç', 'c')
        return re.sub(r'[^a-z0-9]', '', text)

    def _slugify(self, text: str) -> str:
        """Metni URL dostu hale getirir"""
        if not text: return ""
        text = self._normalize(text)
        text = re.sub(r'[^a-z0-9\s-]', '', text).strip()
        text = re.sub(r'[\s_-]+', '-', text)
        return text

    async def get_city_events(self, city: str) -> list:
        merged_results = []
        biletinial_events = []
        bubilet_events = []
        
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
                context = await browser.new_context(user_agent=self.headers["User-Agent"])
                
                # --- Biletinial ---
                page_b = await context.new_page()
                try:
                    url_b = self.sources["biletinial"].format(self._slugify(city))
                    log.info(f"🎭 [Biletinial] {url_b}")
                    await page_b.goto(url_b, wait_until="domcontentloaded", timeout=30000)
                    
                    # Sayfanın yüklenmesini bekle ve biraz kaydır (Lazy load)
                    try:
                        await page_b.wait_for_selector('.event-item', timeout=5000)
                    except:
                        # Bazı şehirlerde farklı class olabiliyor
                        await page_b.wait_for_selector('.sehir-detay__liste li', timeout=5000)

                    await page_b.evaluate("window.scrollBy(0, 1000)")
                    await asyncio.sleep(1)

                    biletinial_events = await page_b.evaluate("""() => {
                        const results = [];
                        // Biletinial'da birden fazla layout var, ikisini de kontrol et
                        let items = document.querySelectorAll('.event-item');
                        if (items.length === 0) items = document.querySelectorAll('.sehir-detay__liste li');
                        
                        items.forEach(el => {
                            const name = (el.querySelector('.event-title') || el.querySelector('h2 a'))?.innerText.trim();
                            const venue = (el.querySelector('.event-venue') || el.querySelector('.sehir-detay__liste-mekan'))?.innerText.trim();
                            const date = (el.querySelector('.event-date') || el.querySelector('.sehir-detay__liste-tarih'))?.innerText.trim();
                            const img = el.querySelector('img')?.src;
                            const link = el.querySelector('a')?.href;
                            
                            if (name) {
                                results.push({
                                    title: name,
                                    venue: venue || "Bilinmiyor",
                                    date: date || "Belirtilmemiş",
                                    image: img,
                                    link: link,
                                    source: "biletinial"
                                });
                            }
                        });
                        return results;
                    }""")
                except Exception as e:
                    log.warning(f"❌ Biletinial Hatası: {e}")
                finally:
                    await page_b.close()

                # --- Bubilet ---
                page_bu = await context.new_page()
                try:
                    url_bu = self.sources["bubilet"].format(self._slugify(city))
                    log.info(f"🎭 [Bubilet] {url_bu}")
                    await page_bu.goto(url_bu, wait_until="domcontentloaded", timeout=30000)
                    
                    await page_bu.wait_for_selector('.event-card, a.group.block', timeout=5000)
                    await page_bu.evaluate("window.scrollBy(0, 1000)")
                    await asyncio.sleep(1)
                    
                    bubilet_events = await page_bu.evaluate("""() => {
                        const results = [];
                        document.querySelectorAll('.event-card, a.group.block').forEach(el => {
                            const name = (el.querySelector('.title') || el.querySelector('h3'))?.innerText.trim();
                            const venue = (el.querySelector('.venue') || el.querySelector('p span.truncate'))?.innerText.trim();
                            const date_els = el.querySelectorAll('p.text-xs.text-gray-500');
                            const date = date_els.length > 0 ? date_els[date_els.length -1].innerText.trim() : (el.querySelector('.date')?.innerText.trim() || "Belirtilmemiş");
                            const link = el.href || el.querySelector('a')?.href;
                            
                            if (name) {
                                results.push({
                                    title: name,
                                    venue: venue || "Bilinmiyor",
                                    date: date,
                                    link: link,
                                    source: "bubilet"
                                });
                            }
                        });
                        return results;
                    }""")
                except Exception as e:
                    log.warning(f"❌ Bubilet Hatası: {e}")
                finally:
                    await page_bu.close()

                await browser.close()
            
            log.info(f"📊 Ham Veri: Biletinial({len(biletinial_events)}) - Bubilet({len(bubilet_events)})")

            # --- Merge Logic (Restored) ---
            fused_list = []
            for ev in bubilet_events:
                ev['normalized'] = self._normalize(ev['title'])
                fused_list.append(ev)

            for b_ev in biletinial_events:
                b_norm = self._normalize(b_ev['title'])
                match_found = False
                for f_ev in fused_list:
                    if fuzz.ratio(b_norm, f_ev['normalized']) > 85:
                        f_ev['source'] += ", biletinial"
                        match_found = True
                        break
                if not match_found:
                    b_ev['normalized'] = b_norm
                    fused_list.append(b_ev)

            # Cleanup
            for ev in fused_list:
                if 'normalized' in ev: del ev['normalized']
            
            merged_results = fused_list
            log.success(f"🔗 {city.upper()} için toplam {len(merged_results)} etkinlik derlendi.")
            
        except Exception as e:
            log.error(f"🔥 [EVENT PATLADI] Hata: {e}")

        return merged_results[:20]

# --- HANDLER ---
async def get_events_handler(city: str) -> list:
    scraper = EventScraper()
    return await scraper.get_city_events(city)