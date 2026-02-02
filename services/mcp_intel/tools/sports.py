import asyncio
from playwright.async_api import async_playwright
from datetime import datetime, timedelta
from loguru import logger as log

class SportsScraper:
    def __init__(self):
        # Süper Lig ve 1. Lig sayfaları
        self.urls = [
            "https://www.tff.org/default.aspx?pageID=198", 
            "https://www.tff.org/default.aspx?pageID=142"
        ]
        self.base_url = "https://www.tff.org/"

    async def get_matches(self):
        log.info("⚽ [SPOR-INTEL] TFF Ligleri Taranıyor...")
        all_matches = []
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(user_agent="Mozilla/5.0...")
            
            # Sadece Bugün ve Yarının maçlarını alıyoruz (Trafik etkisi için)
            today = datetime.now().date()
            tomorrow = today + timedelta(days=1)

            for url in self.urls:
                page = await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    
                    # Haftanın maçları tablosundaki detay linklerini topla
                    target_links = await page.evaluate("""() => {
                        const links = [];
                        const table = document.querySelector("table[id$='dtlHaftaninMaclari']");
                        if (table) {
                            const anchors = table.querySelectorAll("a[href*='macId=']");
                            anchors.forEach(a => {
                                if (!links.includes(a.href)) links.push(a.href);
                            });
                        }
                        return links;
                    }""")
                    
                    # Her maça girip detay bak (Stadyum ve Şehir bilgisi detayda gizli)
                    for link in target_links:
                        try:
                            if not link.startswith("http"): link = self.base_url + link
                            
                            # Yeni sekme açmadan aynı page üzerinde gidebiliriz, daha hızlı olur
                            await page.goto(link, wait_until="domcontentloaded", timeout=15000)
                            
                            match_data = await page.evaluate("""() => {
                                try {
                                    const stadEl = document.querySelector('a[id*="lnkStad"]');
                                    const dateEl = document.querySelector('span[id*="lblTarih"]');
                                    const homeEl = document.querySelector('a[id*="lnkTakim1"]');
                                    const awayEl = document.querySelector('a[id*="lnkTakim2"]');
                                    
                                    return {
                                        stadium: stadEl ? stadEl.innerText.trim() : "Bilinmiyor",
                                        date_str: dateEl ? dateEl.innerText.trim() : "",
                                        home: homeEl ? homeEl.innerText.trim() : "Ev Sahibi",
                                        away: awayEl ? awayEl.innerText.trim() : "Deplasman"
                                    };
                                } catch(e) { return null; }
                            }""")

                            if not match_data or not match_data['date_str']: continue

                            # Tarih Parse (Format: 30.01.2026 - 20:00)
                            dt_str = match_data['date_str'].replace(" - ", " ")
                            match_dt = datetime.strptime(dt_str, "%d.%m.%Y %H:%M")
                            
                            # Zaman Filtresi
                            if match_dt.date() not in [today, tomorrow]:
                                continue
                            
                            # Şehir Ayıklama ("Ali Sami Yen - İstanbul" -> "İstanbul")
                            full_stadium_txt = match_data['stadium']
                            city_name = "Bilinmiyor"
                            if " - " in full_stadium_txt:
                                city_name = full_stadium_txt.split(" - ")[-1].strip()
                                
                            item = {
                                "mac": f"{match_data['home']} vs {match_data['away']}",
                                "zaman": dt_str,
                                "stadyum": full_stadium_txt,
                                "sehir": city_name,
                                "uyari": "Maç saatinde stadyum çevresinde trafik yoğun olabilir."
                            }
                            all_matches.append(item)
                            log.info(f"✅ Maç Bulundu: {item['mac']}")

                        except Exception as e:
                            log.warning(f"Maç Detay Hatası: {e}")
                            continue

                except Exception as e:
                    log.error(f"Lig Sayfası Hatası: {e}")
                finally:
                    await page.close()
            
            await browser.close()
            
        return all_matches

# --- HANDLER ---
async def get_matches_handler() -> list:
    scraper = SportsScraper()
    return await scraper.get_matches()