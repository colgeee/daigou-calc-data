import json
from datetime import date

from pipeline.bounds import gu
from pipeline.census import Census


def test_guam_is_one_rectangle_at_zero_per_cent(tmp_path):
    out = gu.collect(tmp_path, Census(centroids={}, county={}, place={}), date(2026, 9, 9))
    doc = json.loads(out.path.read_text(encoding="utf-8"))
    ring = doc["features"][0]["geometry"]["coordinates"][0]
    assert doc["features"][0]["properties"] == {
        "id": "gu:territory", "source": "gu", "profile": "GU-0-0-GUAM"}
    assert ring[0] == ring[-1] and len(ring) == 5
    lons = {p[0] for p in ring}
    lats = {p[1] for p in ring}
    assert lons == {144.5, 145.1} and lats == {13.1, 13.75}
    assert out.profiles["GU-0-0-GUAM"] == {
        "state": "GU", "label": "Guam", "stateRate": "0", "localRate": "0",
        "foodDrugRate": None}
    assert out.notes["gu"]["bounds"] == [13.1, 13.75, 144.5, 145.1]
