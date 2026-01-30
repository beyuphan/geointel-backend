import asyncio
from playwright.async_api import async_playwright

# --- 🏟️ GENİŞLETİLMİŞ MAPPING ---
STADIUM_DATA = {
    "Gaziantep": "Kalyon Stadyumu (Gaziantep)",
    "Fenerbahçe Beko": "Ülker Spor ve Etkinlik Salonu (İstanbul)",
    "Anadolu Efes": "Sinan Erdem Spor Salonu (İstanbul)",
    "Galatasaray": "Ali Sami Yen Spor Kompleksi (İstanbul)",
    "Fenerbahçe": "Ülker Stadyumu (İstanbul)",
    "Beşiktaş": "Tüpraş Stadyumu (İstanbul)",
    "Samsunspor": "Samsun Yeni 19 Mayıs Stadı (Samsun)",
    "Kasımpaşa": "Recep Tayyip Erdoğan Stadı (İstanbul)",
    # ... buraya tüm ligi ekleyeceğiz kanka
}

async def scrape_sporx_by_day(page, day_offset=0):
    day_label = "BUGÜN" if day_offset == 0 else "YARIN"
    url = f"https://www.sporx.com/tvdebugun/?gun={day_offset}"
    print(f"\n📅 {day_label} İÇİN TARANIYOR: {url}")
    
    await page.goto(url, wait_until="domcontentloaded")
    await asyncio.sleep(3)

    items = await page.evaluate("""() => {
        const results = [];
        document.querySelectorAll('.list-group-item, li').forEach(el => {
            const txt = el.innerText.trim();
            if (/\\d{2}:\\d{2}/.test(txt) && txt.includes('-')) {
                results.push(txt.replace(/\\n/g, ' '));
            }
        });
        return [...new Set(results)];
    }""")

    for item in items:
        if any(bad in item.lower() for bad in ["haber", "transfer", "tahliye"]): continue
        
        venue = "📍 Mekan: Deplasman / Liste Dışı"
        for team, stadium in STADIUM_DATA.items():
            if team.lower() in item.lower():
                idx = item.lower().find(team.lower())
                if '-' in item[idx:idx+30]:
                    venue = f"🚨 KRİTİK MEKAN: {stadium}"
                    break
        print(f"🏆 {item}\n{venue}\n" + "-"*30)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        # Hem bugünü hem yarını çekiyoruz kanka (Proaktif Intel)
        await scrape_sporx_by_day(page, 0) # Bugün
        await scrape_sporx_by_day(page, 1) # Yarın
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())