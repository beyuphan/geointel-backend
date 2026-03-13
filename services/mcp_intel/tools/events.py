import asyncio
import httpx
from selectolax.lexbor import LexborHTMLParser
from thefuzz import fuzz
import re
from loguru import logger as log

class EventScraper:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }

    def _normalize(self, text):
        """Fuzzy match için metni temizler"""
        if not text: return ""
        text = text.lower().replace('İ', 'i').replace('ı', 'i').replace('ğ', 'g').replace('ü', 'u').replace('ş', 's').replace('ö', 'o').replace('ç', 'c')
        return re.sub(r'[^a-z0-9]', '', text)

    async def _get_biletinial(self, client, city):
        log.info(f"🎫 [Biletinial] {city.upper()} taranıyor...")
        url = f"https://biletinial.com/tr-tr/sehrineozel/{city}"
        
        try:
            resp = await client.get(url)
            if resp.status_code != 200: return []
            
            parser = LexborHTMLParser(resp.text)
            results = []
            
            for li in parser.css('.sehir-detay__liste li'):
                title_el = li.css_first('h2 a')
                link_el = li.css_first('a.etlinlikLink')
                date_el = li.css_first('.sehir-detay__liste-mobiltarih') or li.css_first('.sehir-detay__liste-tarih')
                
                if title_el and link_el:
                    title = title_el.text(strip=True)
                    link = link_el.attributes.get('href', '')
                    if link and not link.startswith('http'): link = "https://biletinial.com" + link
                    
                    results.append({
                        "title": title,
                        "link": link,
                        "date": date_el.text(strip=True).replace('\n', ' ') if date_el else "Tarih Yok", 
                        "venue": "Biletinial",
                        "price": "Detayda",
                        "source": "biletinial"
                    })
            return results

        except Exception as e:
            log.warning(f"❌ Biletinial Hatası: {e}")
            return []

    async def _get_bubilet(self, client, city):
        log.info(f"🎫 [Bubilet] {city.upper()} taranıyor...")
        url = f"https://www.bubilet.com.tr/{city}"
        
        try:
            resp = await client.get(url)
            if resp.status_code != 200: return []
            
            parser = LexborHTMLParser(resp.text)
            results = []
            
            # Bubilet link.group.block kullanıyor
            for link_el in parser.css('a.group.block'):
                title_el = link_el.css_first('h3')
                venue_el = link_el.css_first('p span.truncate')
                date_elements = link_el.css('p.text-xs.text-gray-500')
                price_el = link_el.css_first('div.flex.items-start.gap-2')

                if title_el:
                    title = title_el.text(strip=True)
                    venue = venue_el.text(strip=True) if venue_el else "Bilinmiyor"
                    date = date_elements[-1].text(strip=True) if date_elements else "Belirsiz"
                    price = price_el.text(strip=True) if price_el else "Belirsiz"
                    link = link_el.attributes.get('href', '')
                    if link and not link.startswith('http'): link = "https://www.bubilet.com.tr" + link

                    results.append({ 
                        "title": title, 
                        "venue": venue, 
                        "date": date, 
                        "price": price, 
                        "link": link, 
                        "source": "bubilet" 
                    })
            return results

        except Exception as e:
            log.warning(f"❌ Bubilet Hatası: {e}")
            return []

    async def get_city_events(self, city):
        merged_results = []
        
        try:
            async with httpx.AsyncClient(headers=self.headers, follow_redirects=True, timeout=15.0) as client:
                # Paralel çekim yapalım
                t1 = self._get_biletinial(client, city)
                t2 = self._get_bubilet(client, city)
                list_biletinial, list_bubilet = await asyncio.gather(t1, t2)
            
            log.info(f"📊 Ham Veri: Biletinial({len(list_biletinial)}) - Bubilet({len(list_bubilet)})")

            # --- BASİT FÜZYON (Merge) ---
            fused_list = []
            
            # 1. Bubilet'i ana liste yap
            for ev in list_bubilet:
                ev['normalized'] = self._normalize(ev['title'])
                fused_list.append(ev)

            # 2. Biletinial verisini kontrol et, yoksa ekle
            for b_ev in list_biletinial:
                b_norm = self._normalize(b_ev['title'])
                match_found = False
                
                for f_ev in fused_list:
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
            
        except Exception as e:
            log.error(f"🔥 [EVENT PATLADI] Hata: {e}")

        return merged_results[:20]

# --- HANDLER ---
async def get_events_handler(city: str) -> list:
    scraper = EventScraper()
    return await scraper.get_city_events(city)