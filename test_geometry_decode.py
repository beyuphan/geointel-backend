import asyncio
import json
from services.mcp_city.tools.geometry import get_distance_from_route, _get_line_coords

polyline_str = r"gqcwF{|yiDq@kAg@u@a@q@[]w@_A" # just an example

print("Testing geometry...")
try:
    coords = _get_line_coords(polyline_str)
    print("line coords length:", len(coords))
except Exception as e:
    print("error:", e)

location = {"lat": 41.0, "lng": 29.0}
dist = get_distance_from_route(location, polyline_str)
print("dist:", dist)
