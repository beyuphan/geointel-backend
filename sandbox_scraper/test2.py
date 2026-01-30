import asyncio
from playwright.async_api import async_playwright

# Rotadaki kritik noktalar
ROUTE_DISTRICTS = [
    {"city": "ankara", "district": "mamak"},
    {"city": "kirikkale", "district": "merkez"},
    {"city": "corum", "district": "sungurlu"},
    {"city": "amasya", "district": "merzifon"},
    {"city": "samsun", "district": "havza"}
]

# Total için slug 'total' olarak kalsın kanka
FIRMS = ["opet", "shell", "petrol-ofisi", "aytemiz", "total", "m-oil", "lukoil", "aygaz", "türkiye-petrolleri", "sunpet", "bpet"]

async def get_district_prices_surgical(page, city, district, firm):
    url = f"https://www.doviz.com/akaryakit-fiyatlari/{city}/{district}/{firm}"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        
        # JS tarafında null kontrolü ekledik kanka
        prices = await page.evaluate("""() => {
            const row = document.querySelector('table tbody tr');
            if (!row) return null;
            const cells = row.querySelectorAll('td');
            return {
                benzin: cells[1]?.innerText.trim() || "-",
                motorin: cells[2]?.innerText.trim() || "-",
                lpg: cells[3]?.innerText.trim() || "-",
                date: cells[4]?.innerText.trim() || "-"
            };
        }""")
        return prices
    except:
        return None

async def run_route_scan_v3():
    print(f"🚀 [GEOINTEL] Ankara-Samsun Yakıt Hattı Analizi (Ocak 2026)\n")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0...")
        page = await context.new_page()

        for loc in ROUTE_DISTRICTS:
            header = f"📍 {loc['city'].upper()} - {loc['district'].upper()}"
            print(f"{'='*55}\n{header}\n{'='*55}")
            print(f"{'Firma':<15} | {'Benzin':<10} | {'Motorin':<10} | {'LPG':<10}")
            print("-" * 55)

            for firm in FIRMS:
                data = await get_district_prices_surgical(page, loc['city'], loc['district'], firm)
                firm_display = "Total" if firm == "total" else firm.capitalize()
                
                if data:
                    # 'data['benzin'] or "-"' diyerek None gelirse patlamasını önlüyoruz kanka
                    b = data.get('benzin') or "-"
                    m = data.get('motorin') or "-"
                    l = data.get('lpg') or "-"
                    print(f"{firm_display:<15} | {b:<10} | {m:<10} | {l:<10}")
                else:
                    # data komple None geldiyse burası çalışır
                    print(f"{firm_display:<15} | {'Veri Yok':<10} | {'Veri Yok':<10} | {'Veri Yok':<10}")
            print("\n")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run_route_scan_v3())