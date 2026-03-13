import httpx
from selectolax.lexbor import LexborHTMLParser
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
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }

    async def get_matches(self):
        log.info("⚽ [SPOR-INTEL] TFF Ligleri Taranıyor...")
        all_matches = []
        
        today = datetime.now().date()
        tomorrow = today + timedelta(days=1)

        try:
            async with httpx.AsyncClient(headers=self.headers, follow_redirects=True, timeout=20.0) as client:
                for lig_url in self.urls:
                    resp = await client.get(lig_url)
                    if resp.status_code != 200: continue
                    
                    parser = LexborHTMLParser(resp.text)
                    # Haftanın maçları tablosundaki detay linklerini topla
                    # table[id$='dtlHaftaninMaclari'] table[id$='dtlHaftaninMaclari']
                    # Sitede ID'ler dinamik olabiliyor, a[href*='macId='] güvenli
                    links = []
                    for a in parser.css("a[href*='macId=']"):
                        href = a.attributes.get('href')
                        if href:
                            if not href.startswith("http"): href = self.base_url + href
                            if href not in links: links.append(href)
                    
                    # Her maça girip detay bak
                    for link in links:
                        try:
                            m_resp = await client.get(link)
                            if m_resp.status_code != 200: continue
                            
                            m_parser = LexborHTMLParser(m_resp.text)
                            
                            stad_el = m_parser.css_first('a[id*="lnkStad"]')
                            date_el = m_parser.css_first('span[id*="lblTarih"]')
                            home_el = m_parser.css_first('a[id*="lnkTakim1"]')
                            away_el = m_parser.css_first('a[id*="lnkTakim2"]')
                            
                            stadium = stad_el.text(strip=True) if stad_el else "Bilinmiyor"
                            date_str = date_el.text(strip=True) if date_el else ""
                            home = home_el.text(strip=True) if home_el else "Ev Sahibi"
                            away = away_el.text(strip=True) if away_el else "Deplasman"

                            if not date_str: continue

                            # Tarih Parse (Format: 30.01.2026 - 20:00)
                            dt_str = date_str.replace(" - ", " ")
                            try:
                                match_dt = datetime.strptime(dt_str, "%d.%m.%Y %H:%M")
                            except: continue
                            
                            # Zaman Filtresi
                            if match_dt.date() not in [today, tomorrow]:
                                continue
                            
                            # Şehir Ayıklama
                            city_name = "Bilinmiyor"
                            if " - " in stadium:
                                city_name = stadium.split(" - ")[-1].strip()
                                
                            item = {
                                "match": f"{home} vs {away}",
                                "time": dt_str,
                                "stadium": stadium,
                                "city": city_name,
                                "warning": "Maç saatinde stadyum çevresinde trafik yoğun olabilir."
                            }
                            all_matches.append(item)
                            log.info(f"✅ Maç Bulundu: {item['match']}")

                        except Exception as e:
                            log.warning(f"Maç Detay Hatası: {e}")
                            continue

        except Exception as e:
            log.error(f"Lig Sayfası Hatası: {e}")
            
        return all_matches

# --- HANDLER ---
async def get_matches_handler() -> list:
    scraper = SportsScraper()
    return await scraper.get_matches()