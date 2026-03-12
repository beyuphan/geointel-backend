
import asyncio
from playwright.async_api import async_playwright

async def scrape_zes(page):
    print("\n--- ⚡ ZES Fiyatlandırma ---")
    try:
        await page.goto("https://zes.net/tr/fiyatlandirma", timeout=60000)
        await page.wait_for_load_state("networkidle")
        content = await page.inner_text("body")
        # Regex yerine daha esnek bir arama
        lines = [l.strip() for l in content.splitlines() if "₺/kWh" in l]
        for line in lines[:3]:
            print(f"✅ ZES: {line}")
    except Exception as e:
        print(f"❌ ZES Hatası: {e}")

async def scrape_trugo(page):
    print("\n--- 🔋 Trugo (Togg) Fiyatlandırma ---")
    try:
        # Trugo bazen botları sevmez, wait_until'i domcontentloaded yapalım
        await page.goto("https://www.trugo.com.tr/price", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3) # JS'in fiyatları render etmesi için kısa bir es
        content = await page.inner_text("body")
        
        # Sadece ₺ olan ve rakam içeren satırları yakalayalım (AC/DC şartını esnetiyoruz)
        lines = [l.strip() for l in content.splitlines() if "₺" in l and any(char.isdigit() for char in l)]
        
        if lines:
            for line in lines:
                print(f"✅ Trugo: {line}")
        else:
            print("⚠️ Trugo verisi bulundu ama parse edilemedi. Sayfa yapısı farklı olabilir.")
    except Exception as e:
        print(f"❌ Trugo Hatası: {e}")

async def scrape_shell_recharge(page):
    print("\n--- 🐚 Shell Recharge ---")
    try:
        await page.goto("https://www.shell.com.tr/suruculer/shell-recharge-turkiye/fiyat-tarifesi.html", timeout=60000)
        await page.wait_for_load_state("networkidle")
        # Shell tablo kullanır
        table_text = await page.locator("table").first.inner_text()
        lines = [l.strip() for l in table_text.splitlines() if "TL" in l]
        for line in lines:
            print(f"✅ Shell: {line}")
    except Exception as e:
        print(f"❌ Shell Hatası: {e}")

async def scrape_fuel_opet(page):
    print("\n--- ⛽ Akaryakıt Fiyatları (Opet Örneği) ---")
    try:
        # Opet il/ilçe bazlı fiyatlar
        await page.goto("https://www.opet.com.tr/akaryakit-fiyatlari", timeout=60000)
        await page.wait_for_load_state("networkidle")
        
        # Varsayılan olarak İstanbul verisi genelde ekranda olur
        prices = await page.locator(".fuel-price-card").all_inner_texts()
        if not prices:
            # Kart yapısı yoksa tabloyu ara
            prices = await page.locator("table").first.all_inner_texts()
            
        print("✅ Opet Bölgesel (Genel) Fiyatlar:")
        for p in prices[:3]:
            print(f"🔹 {p.replace('\n', ' ')}")
    except Exception as e:
        print(f"❌ Opet Hatası: {e}")


async def scrape_esarj_v2(page):
    print("\n--- 🔌 Eşarj (Zorlamalı Mod) ---")
    try:
        # URL'i ve bekleme stratejisini güncelledik
        await page.goto("https://esarj.com/fiyat-listesi", timeout=60000)
        # Eşarj tablosu bazen geç gelir, direkt tablo hücresini bekleyelim
        await page.wait_for_selector("td", timeout=20000)
        
        # Tabloyu parça parça çekelim
        cells = await page.locator("td").all_inner_texts()
        prices = [c.strip() for c in cells if "TL" in c or "₺" in c]
        
        if prices:
            for p in prices[:4]:
                print(f"✅ Eşarj: {p}")
        else:
            print("⚠️ Eşarj hücresel veri bulunamadı, metin taramasına geçiliyor...")
            content = await page.inner_text("body")
            print(f"İpucu: {content[500:800]}...") # Debug için bir kesit
    except Exception as e:
        print(f"❌ Eşarj Hatası: {e}")

async def scrape_petrol_ofisi(page):
    print("\n--- ⛽ Petrol Ofisi (Bölgesel Veri) ---")
    try:
        # Petrol Ofisi genelde İstanbul/Merkez verisini direkt basar
        await page.goto("https://www.petrolofisi.com.tr/akaryakit-fiyatlari", timeout=60000)
        await page.wait_for_load_state("networkidle")
        
        # Fiyat tablosunu veya kartlarını bulalım
        content = await page.inner_text("body")
        lines = [l.strip() for l in content.splitlines() if "TL/LT" in l or "TL/L" in l]
        
        print("✅ PO Fiyat Örnekleri:")
        for line in lines[:5]:
            print(f"🔹 {line}")
    except Exception as e:
        print(f"❌ Petrol Ofisi Hatası: {e}")

async def scrape_shell_fuel(page):
    print("\n--- ⛽ Shell Akaryakıt (Bölgesel Veri) ---")
    try:
        await page.goto("https://www.shell.com.tr/suruculer/shell-yakitlari/shell-akaryakit-fiyatlari.html", timeout=60000)
        await page.wait_for_load_state("networkidle")
        
        # Shell genelde bir widget kullanır, o yüzden bekleme süresi önemli
        await asyncio.sleep(3)
        content = await page.inner_text("body")
        lines = [l.strip() for l in content.splitlines() if ("Kurşunsuz" in l or "V-Power" in l) and "TL" in l]
        
        print("✅ Shell Akaryakıt:")
        for line in lines[:3]:
            print(f"🔹 {line}")
    except Exception as e:
        print(f"❌ Shell Yakıt Hatası: {e}")

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Sağlam bir user-agent şart
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Teker teker tüm istihbaratı topla
        await scrape_zes(page)
        await scrape_trugo(page)
        await scrape_shell_recharge(page)
        await scrape_fuel_opet(page)
        await scrape_esarj_v2(page)
        # Yeni devleri ekle
        await scrape_petrol_ofisi(page)
        await scrape_shell_fuel(page)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())



