import asyncio
from playwright.async_api import async_playwright

async def run_bubilet_final(city="ankara"):
    print(f"🕵️ [GEOINTEL] {city.upper()} Hattı: Tarih ve Saatler Senkronize Ediliyor...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            url = f"https://www.bubilet.com.tr/{city}"
            await page.goto(url, wait_until="networkidle", timeout=60000)
            
            # 1. Çerez butonu (Resim 3'teki yeşil buton)
            try: await page.get_by_role("button", name="Kabul Et").click(timeout=3000)
            except: pass

            await asyncio.sleep(4)
            # 52 veriyi garanti altına almak için scroll devam
            await page.mouse.wheel(0, 4000)
            await asyncio.sleep(2)

            # 2. CERRAHİ PARSİNG (Resim 6'ya göre güncellendi)
            events_data = await page.evaluate("""() => {
                const results = [];
                document.querySelectorAll('a.group.block').forEach(link => {
                    const title = link.querySelector('h3')?.innerText.trim();
                    // Mekan: p içindeki span.truncate (İkonun yanındaki)
                    const venue = link.querySelector('p span.truncate')?.innerText.trim();
                    // Tarih: 'text-gray-500' ve 'text-xs' olan p etiketi (Resim 6'daki en alt satır)
                    // Bazı kartlarda birden fazla p olabildiği için 'last-of-type' mantığıyla yakalıyoruz
                    const dateElements = link.querySelectorAll('p.text-xs.text-gray-500');
                    const dateString = dateElements.length > 0 ? dateElements[dateElements.length - 1].innerText.trim() : "Zaman Belirtilmemiş";
                    
                    // Fiyat: Resim 6'daki fiyat div'i
                    const price = link.querySelector('div.flex.items-start.gap-2')?.innerText.trim() || "Fiyat Sorun";
                    
                    if (title) {
                        results.push({ title, venue, date: dateString, price, url: link.href });
                    }
                });
                return results;
            }""")

            print(f"✅ TOPLAM {len(events_data)} ETKİNLİK TAM VERİYLE HAZIR!\n")
            for i, ev in enumerate(events_data, 1):
                # '30 Ocak, 31 Ocak ...' gibi gelen veriyi temiz basıyoruz
                print(f"{i}. 📅 {ev['date']} | 💰 {ev['price']}")
                print(f"   🌟 {ev['title']} | 📍 {ev['venue']}")
                print("-" * 55)

        except Exception as e:
            print(f"🔥 HATA: {str(e)}")
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(run_bubilet_final("ankara"))