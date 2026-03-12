from __future__ import annotations

import os
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Dict, List, Optional, Tuple

import httpx
from loguru import logger as log
import pyproj


def _strip_ns(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _maybe_float(x: str) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def _parse_pos_list(text: str) -> List[Tuple[float, float]]:
    """
    GML posList genelde 'x y x y ...' (veya 'lon lat ...') şeklindedir.
    Burada 2D varsayıyoruz.
    """
    if not text:
        return []
    nums = [_maybe_float(t) for t in re.split(r"[\s,]+", text.strip()) if t.strip()]
    nums = [n for n in nums if n is not None]
    if len(nums) < 2:
        return []
    if len(nums) % 2 != 0:
        nums = nums[:-1]
    return list(zip(nums[0::2], nums[1::2]))


def _project_coords(
    coords: List[Tuple[float, float]],
    src_epsg: int,
    dst_epsg: int,
) -> List[Tuple[float, float]]:
    if not coords or src_epsg == dst_epsg:
        return coords

    transformer = pyproj.Transformer.from_crs(
        f"EPSG:{src_epsg}",
        f"EPSG:{dst_epsg}",
        always_xy=True,
    )
    out: List[Tuple[float, float]] = []
    for x, y in coords:
        try:
            lon, lat = transformer.transform(x, y)
            out.append((lon, lat))
        except Exception:
            continue
    return out


def _gml_geometry_to_geojson(
    geom_el: ET.Element,
    src_epsg: int,
    dst_epsg: int,
) -> Optional[Dict[str, Any]]:
    """
    Minimum GML geometry desteği:
    - Point (gml:Point/gml:pos)
    - LineString (gml:LineString/gml:posList)
    - Polygon (gml:Polygon//gml:posList) (sadece dış ring)
    - MultiPoint / MultiLineString / MultiPolygon (temel seviye)
    """
    if geom_el is None:
        return None

    # gml:* elementini bul
    candidates = [geom_el] + list(geom_el.iter())
    for el in candidates:
        name = _strip_ns(el.tag).lower()

        if name == "multipoint":
            points: List[List[float]] = []
            for p in el.iter():
                if _strip_ns(p.tag).lower() == "point":
                    g = _gml_geometry_to_geojson(p, src_epsg, dst_epsg)
                    if g and g.get("type") == "Point":
                        points.append(g["coordinates"])
            if points:
                return {"type": "MultiPoint", "coordinates": points}

        if name == "point":
            pos = None
            for c in el.iter():
                if _strip_ns(c.tag).lower() == "pos" and c.text:
                    pos = c.text
                    break
            coords = _parse_pos_list(pos or "")
            coords = _project_coords(coords, src_epsg, dst_epsg)
            if coords:
                x, y = coords[0]
                return {"type": "Point", "coordinates": [x, y]}

        if name == "multilinestring":
            lines: List[List[List[float]]] = []
            for ls in el.iter():
                if _strip_ns(ls.tag).lower() == "linestring":
                    g = _gml_geometry_to_geojson(ls, src_epsg, dst_epsg)
                    if g and g.get("type") == "LineString":
                        lines.append(g["coordinates"])
            if lines:
                return {"type": "MultiLineString", "coordinates": lines}

        if name == "linestring":
            pos_list = None
            for c in el.iter():
                if _strip_ns(c.tag).lower() == "poslist" and c.text:
                    pos_list = c.text
                    break
            coords = _parse_pos_list(pos_list or "")
            coords = _project_coords(coords, src_epsg, dst_epsg)
            if len(coords) >= 2:
                return {"type": "LineString", "coordinates": [[x, y] for x, y in coords]}

        if name == "multipolygon":
            polys: List[List[List[List[float]]]] = []
            for p in el.iter():
                if _strip_ns(p.tag).lower() == "polygon":
                    g = _gml_geometry_to_geojson(p, src_epsg, dst_epsg)
                    if g and g.get("type") == "Polygon":
                        polys.append(g["coordinates"])
            if polys:
                return {"type": "MultiPolygon", "coordinates": polys}

        if name == "polygon":
            # dış ring (exterior) içindeki ilk posList
            pos_list = None
            for c in el.iter():
                if _strip_ns(c.tag).lower() == "poslist" and c.text:
                    pos_list = c.text
                    break
            coords = _parse_pos_list(pos_list or "")
            coords = _project_coords(coords, src_epsg, dst_epsg)
            if len(coords) >= 4:
                # ring kapalı değilse kapat
                if coords[0] != coords[-1]:
                    coords.append(coords[0])
                return {
                    "type": "Polygon",
                    "coordinates": [[[x, y] for x, y in coords]],
                }

    return None


def _extract_properties(feature_el: ET.Element, geom_field_hint: Optional[str]) -> Dict[str, Any]:
    props: Dict[str, Any] = {}
    for child in list(feature_el):
        key = _strip_ns(child.tag)
        if geom_field_hint and key == geom_field_hint:
            continue
        # geometry alanı değilse text topla
        if len(list(child)) == 0:
            text = child.text.strip() if child.text else ""
            if text != "":
                props[key] = text
        else:
            # kompleks alanlarda ham string'e düş
            text = "".join(child.itertext()).strip()
            if text:
                props[key] = text
    return props


def _guess_geom_field(feature_el: ET.Element) -> Optional[str]:
    """
    Tipik WFS feature içinde geometry alanı:
    - geom
    - geometry
    - the_geom
    - Shape
    """
    candidates = {"geom", "geometry", "the_geom", "shape", "wkb_geometry", "msgeometry"}
    for child in list(feature_el):
        k = _strip_ns(child.tag).lower()
        if k in candidates:
            return _strip_ns(child.tag)
        # child altında gml:Point/LineString/Polygon varsa geometry olma ihtimali yüksek
        for g in child.iter():
            gn = _strip_ns(g.tag).lower()
            if gn in {"point", "linestring", "polygon", "multilinestring", "multipolygon", "multipoint"}:
                return _strip_ns(child.tag)
    return None


def _parse_wfs_gml_to_geojson(
    xml_text: str,
    src_epsg: int,
    dst_epsg: int,
) -> Dict[str, Any]:
    root = ET.fromstring(xml_text)

    # WFS 2.0: wfs:FeatureCollection/wfs:member; WFS 1.1: gml:featureMember
    members: List[ET.Element] = []
    for el in root.iter():
        name = _strip_ns(el.tag).lower()
        if name in {"member", "featuremember"}:
            members.append(el)

    features: List[Dict[str, Any]] = []

    for m in members:
        # member altında genelde 1 adet feature olur
        feature_el = None
        for c in list(m):
            feature_el = c
            break
        if feature_el is None:
            continue

        geom_field = _guess_geom_field(feature_el)
        geom_el = None
        if geom_field:
            for c in list(feature_el):
                if _strip_ns(c.tag) == geom_field:
                    geom_el = c
                    break
        # geometry bulunamadıysa yine de child'larda gml arayalım
        if geom_el is None:
            geom_el = feature_el

        geometry = _gml_geometry_to_geojson(geom_el, src_epsg, dst_epsg)
        props = _extract_properties(feature_el, geom_field)

        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": props,
            }
        )

    return {"type": "FeatureCollection", "features": features}


def normalize_feature_collection(
    fc: Dict[str, Any],
    dataset_id: str,
) -> Dict[str, Any]:
    """
    Farklı WFS katmanlarından gelen FeatureCollection yapısını minimum ortak şemaya indirger.
    Bu, orchestrator/mobil tarafında tek tip tüketim sağlar.
    """
    if not isinstance(fc, dict) or fc.get("type") != "FeatureCollection":
        return {"type": "FeatureCollection", "features": []}

    out_features: List[Dict[str, Any]] = []
    for f in fc.get("features", []) or []:
        if not isinstance(f, dict):
            continue
        props = f.get("properties") or {}
        if not isinstance(props, dict):
            props = {}
        geom = f.get("geometry")

        fid = f.get("id") or props.get("id") or props.get("OBJECTID") or props.get("objectid")
        if not fid:
            # stabil id üret
            raw = json.dumps({"p": props, "g": geom}, sort_keys=True, ensure_ascii=False).encode("utf-8")
            fid = sha256(raw).hexdigest()[:16]

        name = (
            props.get("name")
            or props.get("NAME")
            or props.get("adi")
            or props.get("ADI")
            or props.get("title")
            or props.get("TITLE")
        )

        out_features.append(
            {
                "type": "Feature",
                "id": str(fid),
                "geometry": geom,
                "properties": {
                    "dataset_id": dataset_id,
                    "name": name,
                    **props,
                },
            }
        )

    return {"type": "FeatureCollection", "features": out_features}


@dataclass(frozen=True)
class WFS_CKAN_Dataset:
    dataset_id: str
    url: str  # CKAN datastore_search API url'i veya doğrudan .geojson linki
    src_epsg: int = 4326 # CKAN genellikle WGS84 döner

# İBB Açık Veri (CKAN) veya doğrudan GeoJSON/WFS URL'leri
WFS_DATASETS: Dict[str, WFS_CKAN_Dataset] = {
    # Örnek IBB CKAN Datastore API Endpoint'leri:
    "ibb_afet_toplanma": WFS_CKAN_Dataset(
        dataset_id="ibb_afet_toplanma", 
        url="dynamic_search_Afet Toplanma", 
        src_epsg=4326
    ),
    "ibb_ispark": WFS_CKAN_Dataset(
        dataset_id="ibb_ispark", 
        url="live_api_ispark", 
        src_epsg=4326
    ),
    "ibb_wifi": WFS_CKAN_Dataset(
        dataset_id="ibb_wifi",
        url="https://data.ibb.gov.tr/api/3/action/datastore_search?resource_id=5d0a0b1e-9e56-4038-b966-7d3e7b46f882",
        src_epsg=4326
    ),
    "ibb_sosyal_tesis": WFS_CKAN_Dataset(
        dataset_id="ibb_sosyal_tesis",
        url="dynamic_search_Sosyal Tesis", # "Sosyal Tesis" kelimesini aratacağız
        src_epsg=4326
    )
}

def list_wfs_datasets() -> List[Dict[str, Any]]:
    return [
        {"dataset_id": ds.dataset_id, "url": ds.url, "src_epsg": ds.src_epsg}
        for ds in WFS_DATASETS.values()
    ]


class _TTLCache:
    def __init__(self, ttl_s: int = 300, max_items: int = 128):
        self.ttl_s = ttl_s
        self.max_items = max_items
        self._store: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        item = self._store.get(key)
        if not item:
            return None
        ts, val = item
        if now - ts > self.ttl_s:
            self._store.pop(key, None)
            return None
        return val

    def set(self, key: str, val: Dict[str, Any]) -> None:
        if len(self._store) >= self.max_items:
            # en eskiyi at (basit strateji)
            oldest = sorted(self._store.items(), key=lambda kv: kv[1][0])[0][0]
            self._store.pop(oldest, None)
        self._store[key] = (time.time(), val)


_wfs_cache = _TTLCache(ttl_s=300, max_items=128)


async def fetch_wfs_as_geojson(
    base_url: str,
    type_name: str,
    bbox: Optional[Tuple[float, float, float, float]] = None,
    src_epsg: int = 5254,
    dst_epsg: int = 4326,
    max_features: int = 200,
    extra_params: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    WFS katmanını çekip GeoJSON döndürür.

    Strateji:
    1) outputFormat GeoJSON istenir (birçok WFS bunu destekler)
    2) GeoJSON gelmezse GML/XML parse edilip GeoJSON'a çevrilir

    Not:
    - bbox koordinat sistemi WFS'e göre değişebilir. İBB tarafında sıklıkla yerel CRS (EPSG:5254)
      kullanıldığı için varsayılan src_epsg=5254 tutuldu.
    """
    cache_key = json.dumps(
        {
            "u": base_url,
            "t": type_name,
            "b": bbox,
            "s": src_epsg,
            "d": dst_epsg,
            "c": max_features,
            "x": extra_params or {},
        },
        sort_keys=True,
    )
    cached = _wfs_cache.get(cache_key)
    if cached is not None:
        return cached

    params: Dict[str, str] = {
        "service": "WFS",
        "request": "GetFeature",
        "typeNames": type_name,
        "count": str(max_features),
        "outputFormat": "application/json",
    }

    if bbox:
        minx, miny, maxx, maxy = bbox
        # BBOX'ın CRS'i açıkça belirtilsin (WFS 2.0 için)
        params["bbox"] = f"{minx},{miny},{maxx},{maxy},EPSG:{src_epsg}"

    if extra_params:
        params.update(extra_params)

    async with httpx.AsyncClient(timeout=30.0) as client:
        log.info(f"🌐 [WFS] GetFeature: {type_name}")
        resp = await client.get(base_url, params=params)
        text = resp.text

        ctype = (resp.headers.get("content-type") or "").lower()
        if "application/json" in ctype or text.lstrip().startswith("{"):
            try:
                data = resp.json()
                # Bazı sunucular zaten GeoJSON döner.
                if data.get("type") == "FeatureCollection":
                    _wfs_cache.set(cache_key, data)
                    return data
            except Exception:
                pass

        # JSON gelmediyse XML/GML'e fallback
        params_xml = dict(params)
        params_xml["outputFormat"] = "text/xml; subtype=gml/3.2"
        resp2 = await client.get(base_url, params=params_xml)
        xml_text = resp2.text
        try:
            data = _parse_wfs_gml_to_geojson(xml_text, src_epsg=src_epsg, dst_epsg=dst_epsg)
        except Exception as e:
            log.error(f"❌ [WFS] XML Parse Hatası: {e} | Sunucu Yanıtı: {xml_text[:100]}")
            data = {"type": "FeatureCollection", "features": []}
        
        _wfs_cache.set(cache_key, data)
        return data

async def fetch_ibb_dataset_geojson(
    dataset_id: str,
    bbox: Optional[Tuple[float, float, float, float]] = None,
    max_features: int = 200,
) -> Dict[str, Any]:
    if dataset_id not in WFS_DATASETS:
        raise ValueError(f"Bilinmeyen dataset_id: {dataset_id}")

    ds = WFS_DATASETS[dataset_id]
    
    # --- STRATEJİ 1: İSPARK CANLI API (test_ispark.py mantığı) ---
    if ds.url == "live_api_ispark":
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            log.info(f"📡 [Canlı API] Veri çekiliyor: {dataset_id}")
            try:
                resp = await client.get("https://api.ibb.gov.tr/ispark/Park", headers=headers)
                data = resp.json()
                features = []
                for park in data[:max_features]:
                    lat, lon = park.get("lat"), park.get("lng")
                    if lat and lon:
                        features.append({
                            "type": "Feature",
                            "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
                            "properties": {
                                "name": park.get("parkName", "İSPARK"),
                                "capacity": park.get("capacity", 0),
                                "emptyCapacity": park.get("emptyCapacity", 0),
                                "workHours": park.get("workHours", "-")
                            }
                        })
                fc = {"type": "FeatureCollection", "features": features}
                return normalize_feature_collection(fc, dataset_id=dataset_id)
            except Exception as e:
                log.error(f"❌ [İSPARK API] Hata: {e}")
                return {"type": "FeatureCollection", "features": []}

    # --- STRATEJİ 2: DİNAMİK PAKET ARAMA (test_ibb_sosyal.py mantığı) ---
    elif ds.url.startswith("dynamic_search_"):
        search_query = ds.url.split("dynamic_search_")[1]
        base_url = "https://data.ibb.gov.tr/api/3/action/"
        
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            log.info(f"🔍 [Dinamik Arama] '{search_query}' aranıyor...")
            try:
                pkg_resp = await client.get(base_url + "package_search", params={"q": search_query, "rows": 1})
                resource_id = None
                
                if pkg_resp.json().get("result", {}).get("results"):
                    for res in pkg_resp.json()["result"]["results"][0]["resources"]:
                        if res["format"].upper() in ["JSON", "CSV"]:
                            resource_id = res["id"]
                            break
                
                if not resource_id:
                    log.warning(f"⚠️ {search_query} için güncel veri ID'si bulunamadı.")
                    return {"type": "FeatureCollection", "features": []}
                
                # ID bulundu, şimdi veriyi çek (Strateji 3'teki datastore mantığına yönlendir)
                ds = WFS_CKAN_Dataset(dataset_id=dataset_id, url=f"{base_url}datastore_search?resource_id={resource_id}")
                # Koda aşağıdan devam edecek...
            except Exception as e:
                log.error(f"❌ [Dinamik Arama] Hata: {e}")
                return {"type": "FeatureCollection", "features": []}

    # --- STRATEJİ 3: CKAN DATASTORE API (Afet Toplanma ve test_ibb_wifi.py mantığı) ---
    if "datastore_search" in ds.url:
        params = {"limit": max_features}
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            log.info(f"🌐 [CKAN API] Veri çekiliyor: {dataset_id}")
            try:
                resp = await client.get(ds.url, params=params)
                data = resp.json()
                
                if data.get("success"):
                    records = data["result"]["records"]
                    features = []
                    for rec in records:
                        # Enlem/Boylam eşleştirmesi (test_ibb_wifi.py ve eski kod harmanı)
                        lat = rec.get("ENLEM") or rec.get("Enlem") or rec.get("LATITUDE") or rec.get("lat")
                        lon = rec.get("BOYLAM") or rec.get("Boylam") or rec.get("LONGITUDE") or rec.get("lon")
                        
                        lat_f, lon_f = _maybe_float(str(lat)), _maybe_float(str(lon))
                        if lat_f is not None and lon_f is not None:
                            geom = {"type": "Point", "coordinates": [lon_f, lat_f]}
                            if ds.src_epsg != 4326:
                                geom["coordinates"] = _project_coords([geom["coordinates"]], ds.src_epsg, 4326)[0]
                                
                            features.append({"type": "Feature", "geometry": geom, "properties": rec})
                    
                    fc = {"type": "FeatureCollection", "features": features}
                    return normalize_feature_collection(fc, dataset_id=dataset_id)
                else:
                    raise ValueError(f"CKAN API başarısız yanıt döndü.")
            except Exception as e:
                log.error(f"❌ [CKAN API] Hata: {e}")
                return {"type": "FeatureCollection", "features": []}

    # 4. Aşama: Eskiden kalan GeoJSON linkleri veya Fallback WFS URL (Eski kodun geri kalanı)
    elif ds.url.endswith(".geojson") or ds.url.endswith(".json"):
        # ... (burası senin wfs.py'deki eski kodun 2. aşaması olarak aynı kalabilir)
        pass
        
    else:
        # ... (burası senin wfs.py'deki eski kodun 3. aşaması olarak aynı kalabilir)
        pass