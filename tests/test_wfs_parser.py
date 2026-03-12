import pytest

from services.mcp_city.tools.wfs import _parse_wfs_gml_to_geojson


def test_parse_wfs_point_gml_to_geojson():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0"
                           xmlns:gml="http://www.opengis.net/gml/3.2"
                           xmlns:ibb="http://example.com/ibb">
      <wfs:member>
        <ibb:afettoplanma>
          <ibb:id>1</ibb:id>
          <ibb:name>Test Alan</ibb:name>
          <ibb:geom>
            <gml:Point>
              <gml:pos>29 41</gml:pos>
            </gml:Point>
          </ibb:geom>
        </ibb:afettoplanma>
      </wfs:member>
    </wfs:FeatureCollection>
    """

    fc = _parse_wfs_gml_to_geojson(xml, src_epsg=4326, dst_epsg=4326)
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 1
    f = fc["features"][0]
    assert f["geometry"]["type"] == "Point"
    assert f["geometry"]["coordinates"] == [29.0, 41.0]


def test_parse_wfs_linestring_gml_to_geojson():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0"
                           xmlns:gml="http://www.opengis.net/gml/3.2"
                           xmlns:ibb="http://example.com/ibb">
      <wfs:member>
        <ibb:line>
          <ibb:geom>
            <gml:LineString>
              <gml:posList>29 41 29.1 41.1</gml:posList>
            </gml:LineString>
          </ibb:geom>
        </ibb:line>
      </wfs:member>
    </wfs:FeatureCollection>
    """

    fc = _parse_wfs_gml_to_geojson(xml, src_epsg=4326, dst_epsg=4326)
    f = fc["features"][0]
    assert f["geometry"]["type"] == "LineString"
    assert f["geometry"]["coordinates"][0] == [29.0, 41.0]


def test_parse_wfs_polygon_gml_to_geojson_closes_ring():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0"
                           xmlns:gml="http://www.opengis.net/gml/3.2"
                           xmlns:ibb="http://example.com/ibb">
      <wfs:member>
        <ibb:poly>
          <ibb:geom>
            <gml:Polygon>
              <gml:exterior>
                <gml:LinearRing>
                  <gml:posList>29 41 29.1 41 29.1 41.1 29 41.1</gml:posList>
                </gml:LinearRing>
              </gml:exterior>
            </gml:Polygon>
          </ibb:geom>
        </ibb:poly>
      </wfs:member>
    </wfs:FeatureCollection>
    """

    fc = _parse_wfs_gml_to_geojson(xml, src_epsg=4326, dst_epsg=4326)
    ring = fc["features"][0]["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1]

