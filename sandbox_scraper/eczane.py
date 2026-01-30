import asyncio
from playwright.async_api import async_playwright

async def get_pharmacy_data(city="samsun"):
    print(f"🛰️ [GEOINTEL] {city.upper()} Hattı Sökülüyor...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            url = f"https://www.eczaneler.gen.tr/nobetci-{city}"
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Listenin DOM'a girmesini bekle
            await page.wait_for_selector("tbody tr", state="attached", timeout=15000)

            # 🛠️ CERRAHİ PARSİNG (Attığın görsellere göre tam uyumlu)
            pharmacies = await page.evaluate("""() => {
                const results = [];
                document.querySelectorAll('tbody tr').forEach(row => {
                    const rowContainer = row.querySelector('.row');
                    if (!rowContainer) return;

                    // 1. İsim
                    const name = rowContainer.querySelector('span.isim')?.innerText.trim();
                    
                    // 2. İlçe (Badge içindeki veri)
                    const district = rowContainer.querySelector('.bg-info')?.innerText.trim() || "";
                    
                    // 3. Adres (İlçe ismini temizleyerek tam metni alıyoruz)
                    const addressDiv = rowContainer.querySelector('.col-lg-6');
                    let address = "";
                    if (addressDiv) {
                        // Div içindeki tüm metni al ve ilçe ismini içinden sök
                        let rawAddress = addressDiv.innerText.trim();
                        address = rawAddress.replace(district, '').trim();
                        // Fazlalık kalan "/" veya yeni satırları temizle
                        address = address.replace(/\\s+/g, ' ').trim();
                    }
                    
                    // 4. Telefon
                    const phone = rowContainer.querySelector('.col-lg-3.py-lg-2')?.innerText.trim();
                    
                    // 5. Google Maps Link
                    const mapLink = rowContainer.querySelector('a[href*="maps"]')?.href;

                    if (name && address) {
                        results.push({ name, district, address, phone, mapLink });
                    }
                });
                return results;
            }""")

            return pharmacies

        except Exception as e:
            print(f"🔥 {city.upper()} Hatası: {str(e)}")
            return []
        finally:
            await browser.close()

async def run_nationwide_intel():
    # OMÜ projen için Samsun ve Rize kritik
    target_cities = ["samsun", "rize", "ankara", "istanbul"]
    
    for city in target_cities:
        data = await get_pharmacy_data(city)
        if data:
            print(f"\n✅ {city.upper()}: {len(data)} eczane saptandı.")
            for p in data[:5]: # İlk 5 tanesini detaylı görelim kanka
                print(f" 💊 {p['name']} | 📍 {p['district']}")
                print(f" 🏠 Adres: {p['address']}")
                print(f" 📞 Tel: {p['phone']}")
                print("-" * 30)
        else:
            print(f"⚠️ {city.upper()}: Veri sökülemedi!")
        await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(run_nationwide_intel())