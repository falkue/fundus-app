import pytest

from fundus.geo import region_slug


@pytest.mark.parametrize("geo,slug", [
    ("United States", "us"), ("US", "us"), ("us", "us"), ("United States – Texas", "us"), ("Singapore", "sg"),
    ("SG", "sg"), ("United Arab Emirates", "ae"), ("World", "global"), ("World (excl. China)", "global"),
    ("Global (11 markets)", "global"), ("EU27", "europe"), ("Switzerland", "ch"), ("Mainland China", "cn"),
    ("US/CA", None), ("15 countries (Chegg panel)", None), ("United States / China", None), ("Texas", None),
])
def test_region_slug(geo, slug):
    assert region_slug(geo) == slug


def test_add_tags_region_from_geo(store):
    with store.batch("t"):
        i = store.add("observation", {"metric": "Share", "value": 4.69, "unit": "%", "geo": "Singapore"},
                      tags={"industry": ["cross-industry"], "topic": ["labour-market"]})
    assert ("region", "sg") in {tuple(r) for r in store.db.execute(
        "SELECT facet, value FROM tag WHERE item_id = ?", (str(i),))}
