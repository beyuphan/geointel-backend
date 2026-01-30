import asyncio
from playwright.async_api import async_playwright

async def get_price_surgical(browser, url, city="Samsun"):
    """Etkinlik detayından şehre özel fiyatı söküp alan cerrah fonksiyon."""
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2) # JS'nin fiyatı basması için şart
        
        # Verdiğin seçici: ed-biletler__sehir[data-sehir="Samsun"] -> span[itemprop="price"]
        price = await page.evaluate(f"""() => {{
            const cityDiv = document.querySelector('div.ed-biletler__sehir[data-sehir="{city}"]');
            if (cityDiv) {{
                const priceSpan = cityDiv.querySelector('span.price-info[itemprop="price"]');
                return priceSpan ? priceSpan.getAttribute('content') : "Fiyat Bulunamadı";
            }}
            return "Şehir Eşleşmedi";
        }}""")
        return price
    except:
        return "Bağlantı Hatası"
    finally:
        await context.close()

async def run_geointel_v3():
    print("🛰️ [GEOINTEL] Samsun Hattı Limit Breaker & Surgical Price Başlatılıyor...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            viewport={'width': 1280, 'height': 900}
        )
        page = await context.new_page()

        try:
            url = "https://biletinial.com/tr-tr/sehrineozel/samsun#"
            await page.goto(url, wait_until="networkidle", timeout=60000)
            
            try:
                await page.get_by_role("button", name="Kabul Ediyorum").click(timeout=3000)
            except: pass

            # 1. 15 SINIRINI ZORLAYARAK AŞMA (Element-Count Based Scroll)
            print("⏳ 15 sınırı aşılmaya çalışılıyor, liste sonuna kadar zorlanıyor...")
            current_count = 0
            while True:
                # Sayfadaki li sayısını kontrol et
                new_count = await page.locator('.sehir-detay__liste li').count()
                if new_count <= current_count: # Daha fazla eleman yüklenmiyorsa dur
                    # Bir kez daha aşağı kaydırıp 3 sn bekle, garanti olsun
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(3)
                    final_count = await page.locator('.sehir-detay__liste li').count()
                    if final_count <= new_count: break
                
                current_count = new_count
                await page.evaluate("window.scrollBy(0, 1500)")
                await asyncio.sleep(2)
                print(f"📊 Şu anki etkinlik sayısı: {current_count}")

            # 2. TEMEL LİSTEYİ TOPLA
            raw_events = await page.evaluate("""() => {
                const results = [];
                let currentDay = "Tarih Yok";
                const items = document.querySelectorAll('.sehir-detay__liste li');
                
                items.forEach(li => {
                    const dateEl = li.querySelector('.sehir-detay__liste-mobiltarih, .sehir-detay__liste-tarih');
                    if (dateEl) { currentDay = dateEl.innerText.trim(); }

                    const linkEl = li.querySelector('a.etlinlikLink');
                    if (linkEl && linkEl.href) {
                        const titleEl = li.querySelector('h2 a');
                        results.push({
                            date: currentDay,
                            title: titleEl ? titleEl.innerText.trim() : "Başlıksız",
                            link: linkEl.href
                        });
                    }
                });
                return results;
            }""")

            print(f"✅ {len(raw_events)} etkinlik bulundu. Detaylı fiyatlar sökülüyor...")

            # 3. SURGICAL PRICE EXTRACTION
            for i, ev in enumerate(raw_events, 1):
                print(f"[{i}/{len(raw_events)}] 🕵️ Fiyat sökülüyor: {ev['title'][:35]}...")
                ev['price'] = await get_price_surgical(browser, ev['link'], "Samsun")
                
                print(f"   💰 {ev['price']} TL | 📅 {ev['date']}")
                print("-" * 40)

        except Exception as e:
            print(f"🔥 HATA: {str(e)}")
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(run_geointel_v3())