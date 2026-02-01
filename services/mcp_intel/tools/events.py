import asyncio
from playwright.async_api import async_playwright
from thefuzz import fuzz
import re
from loguru import logger as log

class EventScraper:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        }

    def _normalize(self, text):
        """Fuzzy match için metni temizler"""
        if not text: return ""
        text = text.lower().replace('İ', 'i').replace('ı', 'i').replace('ğ', 'g').replace('ü', 'u').replace('ş', 's').replace('ö', 'o').replace('ç', 'c')
        return re.sub(r'[^a-z0-9]', '', text)

    async def _get_biletinial(self, page, city):
        log.info(f"🎫 [Biletinial] {city.upper()} taranıyor...")
        url = f"https://biletinial.com/tr-tr/sehrineozel/{city}"
        
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=40000)
            
            # Çerez kapatma denemesi
            try: await page.get_by_role("button", name="Kabul Ediyorum").click(timeout=2000)
            except: pass

            # Biraz scroll yapalım ki lazy-load tetiklensin
            for _ in range(4):
                await page.evaluate("window.scrollBy(0, 1000)")
                await asyncio.sleep(0.5)

            raw_events = await page.evaluate("""() => {
                const results = [];
                document.querySelectorAll('.sehir-detay__liste li').forEach(li => {
                    const titleEl = li.querySelector('h2 a');
                    const linkEl = li.querySelector('a.etlinlikLink');
                    const dateEl = li.querySelector('.sehir-detay__liste-mobiltarih') || li.querySelector('.sehir-detay__liste-tarih');
                    
                    if (titleEl && linkEl) {
                        results.push({
                            title: titleEl.innerText.trim(),
                            link: linkEl.href,
                            date: dateEl ? dateEl.innerText.replace(/\\n/g, ' ').trim() : "Tarih Yok", 
                            venue: "Biletinial",
                            price: "Detayda",
                            source: "biletinial"
                        });
                    }
                });
                return results;
            }""")
            return raw_events

        except Exception as e:
            log.warning(f"❌ Biletinial Hatası: {e}")
            return []

    async def _get_bubilet(self, page, city):
        log.info(f"🎫 [Bubilet] {city.upper()} taranıyor...")
        url = f"https://www.bubilet.com.tr/{city}"
        
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=40000)
            
            # Bubilet daha çok scroll ister
            await page.mouse.wheel(0, 4000)
            await asyncio.sleep(1.5)

            events = await page.evaluate("""() => {
                const results = [];
                document.querySelectorAll('a.group.block').forEach(link => {
                    const title = link.querySelector('h3')?.innerText.trim();
                    const venue = link.querySelector('p span.truncate')?.innerText.trim();
                    const dateElements = link.querySelectorAll('p.text-xs.text-gray-500');
                    const date = dateElements.length > 0 ? dateElements[dateElements.length - 1].innerText.trim() : "Belirsiz";
                    const price = link.querySelector('div.flex.items-start.gap-2')?.innerText.trim() || "Belirsiz";

                    if (title) {
                        results.push({ 
                            title, venue, date, price, 
                            link: link.href, 
                            source: "bubilet" 
                        });
                    }
                });
                return results;
            }""")
            return events

        except Exception as e:
            log.warning(f"❌ Bubilet Hatası: {e}")
            return []

    async def get_city_events(self, city):
        merged_results = []
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(user_agent=self.headers["User-Agent"])
            
            # Bot dedektörlerini atlatmak için
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            
            page = await context.new_page()

            # Paralel veya sıralı çekim (Şimdilik sıralı güvenli)
            list_biletinial = await self._get_biletinial(page, city)
            list_bubilet = await self._get_bubilet(page, city)
            
            log.info(f"📊 Ham Veri: Biletinial({len(list_biletinial)}) - Bubilet({len(list_bubilet)})")

            # --- BASİT FÜZYON (Merge) ---
            fused_list = []
            
            # 1. Bubilet'i ana liste yap (Verisi genelde daha temiz)
            for ev in list_bubilet:
                ev['normalized'] = self._normalize(ev['title'])
                fused_list.append(ev)

            # 2. Biletinial verisini kontrol et, yoksa ekle
            for b_ev in list_biletinial:
                b_norm = self._normalize(b_ev['title'])
                match_found = False
                
                for f_ev in fused_list:
                    # %85 isim benzerliği varsa aynı etkinliktir
                    ratio = fuzz.ratio(b_norm, f_ev['normalized'])
                    if ratio > 85:
                        f_ev['source'] += ", biletinial"
                        match_found = True
                        break
                
                if not match_found:
                    b_ev['normalized'] = b_norm
                    fused_list.append(b_ev)
            
            # 'normalized' anahtarını temizle
            for ev in fused_list:
                if 'normalized' in ev: del ev['normalized']
            
            merged_results = fused_list
            log.success(f"🔗 {city.upper()} için toplam {len(merged_results)} etkinlik derlendi.")
            await browser.close()
            
        return merged_results[:20] # Çok şişirmemek için ilk 20 etkinlik

# --- HANDLER ---
async def get_events_handler(city: str) -> list:
    scraper = EventScraper()
    return await scraper.get_city_events(city)