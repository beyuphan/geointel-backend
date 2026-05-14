import json
import logging
logging.basicConfig(level=logging.DEBUG)

from tools.geometry import get_distance_from_route, sample_route_points

# standard google polyline for a small segment
poly = "gqcwF{|yiDq@kAg@u@a@q@[]w@_A"

print("testing sample_route_points:")
points = sample_route_points(encoded_polyline=poly)
print("points:", len(points))

print("testing get_distance_from_route:")
loc = {"lat": 41.0, "lng": 29.0}
dist = get_distance_from_route(loc, poly)
print("dist:", dist)
