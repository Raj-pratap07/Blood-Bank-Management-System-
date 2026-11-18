"""Teacher-demo improvements: responsive result cards, the demo map pin, fixed synthetic component stock, and
strict separation from the real directory. All data here is fictional; every test uses a disposable database."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.config import BASE_DIR, settings
from app.models import AvailabilityPublication, BloodGroup, Facility, Product, StockVisibility
from app.schemas.facilities import FacilityIn
from app.services import availability as avail
from app.services import demo_stock
from app.services import facilities as fsvc
from app.utils import utcnow

DATA_RE = re.compile(r'<script id="facility-data" type="application/json">(.*?)</script>', re.S)


@pytest.fixture(autouse=True)
def _demo_mode(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)


def _publish(db, users, make_unit, groups=("B+", "B+")):
    for g in groups:
        make_unit(group=g)
    fac = db.get(Facility, users["admin"].facility_id)
    fac.stock_visibility = StockVisibility.EXACT
    db.commit()
    avail.publish(db, users["medical_officer"])
    return fac


def _rows(db, fac, now=None):
    view = avail.demo_view(db, fac, now=now)
    rows = avail.search_rows(view, "Demo", None, True, list(BloodGroup), list(avail.SEARCH_PRODUCTS))
    return demo_stock.overlay(rows, view)


def _map_data(html):
    return json.loads(DATA_RE.search(html).group(1))


# ----------------------------------------------------------------------------- responsive results
class TestResponsiveResults:
    def test_results_are_cards_not_a_wide_table(self, client, db, users, make_unit):
        _publish(db, users, make_unit)
        html = client.get("/demo?group=B%2B").text
        results = html[html.index('class="pub-results'):html.index("Availability is not a reservation")]
        assert "<table" not in results
        cards = re.findall(r'<article class="pub-result [^"]*".*?</article>', results, re.S)
        assert len(cards) == 4                                        # B+ x 4 components
        for card in cards:                                            # every field present in every card
            assert "Quantity:" in card and "Last updated:" in card and "B+" in card and "Demo Blood Centre" in card
            assert 'class="pub-chip' in card

    def test_find_panel_uses_the_same_cards(self, client):
        html = client.get("/find?group=A%2B").text
        assert "<table" not in html and "pub-search-results" not in html

    def test_css_wraps_instead_of_overflowing(self):
        css = (Path(BASE_DIR) / "app" / "static" / "css" / "public.css").read_text(encoding="utf-8")
        assert "grid-template-columns: repeat(auto-fill, minmax(min(100%, 15rem), 1fr))" in css
        assert re.search(r"\.pub-result \{[^}]*min-width: 0;[^}]*overflow-wrap: anywhere", css)


# ----------------------------------------------------------------------------- demo map
class TestDemoMap:
    def test_demo_hub_has_one_labelled_demo_marker(self, client, db, users, make_unit):
        _publish(db, users, make_unit)
        (m,) = _map_data(client.get("/demo").text)
        assert m["demo"] is True and m["approximate"] is True
        assert [m["lat"], m["lon"]] == list(settings.map_default_center)       # illustrative, from config only
        assert "not a real location" in m["position_note"]
        assert m["request_url"] == "/demo/request"
        stock = {s["group"]: s for s in m["whole_blood"]}
        assert stock["B+"]["units"] == 2 and stock["B+"]["status"] == "low"     # 2 <= BBMS_AVAILABILITY_LOW_MAX
        assert stock["O-"]["units"] == 0 and stock["O-"]["status"] == "not_available"

    def test_marker_without_publication_claims_no_stock(self, client):
        (m,) = _map_data(client.get("/demo").text)
        assert m["current"] is False and m["whole_blood"] == []

    def test_real_directory_map_has_no_demo_marker(self, client, db, users, make_unit):
        _publish(db, users, make_unit)
        assert all(not f.get("demo") for f in _map_data(client.get("/find").text))

    def test_no_marker_for_real_facility_or_outside_demo_mode(self, db, users, monkeypatch):
        real = fsvc.create_facility(db, FacilityIn(display_name="Real-like Facility (test)"), users["platform_admin"])
        assert demo_stock.map_marker(real, avail.PublicAvailability("off")) is None
        demo = db.get(Facility, users["admin"].facility_id)
        monkeypatch.setattr(settings, "demo_mode", False)
        assert demo_stock.map_marker(demo, avail.PublicAvailability("off")) is None

    def test_demo_pin_is_distinctly_styled(self):
        js = (Path(BASE_DIR) / "app" / "static" / "js" / "public_map.js").read_text(encoding="utf-8")
        assert "pub-pin-demo" in js and "DEMO DATA" in js and "textContent" in js


# ----------------------------------------------------------------------------- synthetic stock
class TestSyntheticStock:
    def test_components_use_fixed_figures_with_zero_examples(self, db, users, make_unit):
        rows = {(r.blood_group, r.product): r for r in _rows(db, _publish(db, users, make_unit))}
        for product, figures in demo_stock.SIMULATED.items():
            for g, n in figures.items():
                r = rows[(g.value, product.value)]
                assert r.simulated and r.units == n and r.status == ("available" if n else "unavailable")
        assert any(n == 0 for f in demo_stock.SIMULATED.values() for n in f.values())
        wb = rows[("B+", "whole_blood")]
        assert not wb.simulated and wb.units == 2                     # whole blood: from the synthetic inventory

    def test_deterministic_across_loads(self, client, db, users, make_unit):
        fac = _publish(db, users, make_unit)
        assert [(r.blood_group, r.product, r.units) for r in _rows(db, fac)] == \
               [(r.blood_group, r.product, r.units) for r in _rows(db, fac)]
        a = client.get("/demo?component=platelets").text
        b = client.get("/demo?component=platelets").text
        strip = lambda h: re.findall(r'<strong>([^<]*bags \(units\))</strong>', h)   # noqa: E731
        assert strip(a) == strip(b) and len(strip(a)) == 8

    def test_every_simulated_figure_is_labelled_demo(self, client, db, users, make_unit):
        _publish(db, users, make_unit)
        html = client.get("/demo?component=fresh_frozen_plasma").text
        for card in re.findall(r'<article class="pub-result .*?</article>', html, re.S):
            assert "DEMO DATA" in card.split("Quantity:")[1] and "simulated" in card
        assert "fixed simulated" in html

    def test_no_simulation_when_report_is_stale_or_missing(self, db, users, make_unit):
        fac = db.get(Facility, users["admin"].facility_id)
        assert all(r.status == "not_reported" and not r.simulated for r in _rows(db, fac))
        _publish(db, users, make_unit)
        from datetime import timedelta
        later = utcnow() + timedelta(hours=settings.availability_max_hours + 1)
        assert all(r.status == "stale" and r.units is None and not r.simulated for r in _rows(db, fac, now=later))


# ----------------------------------------------------------------------------- real / synthetic separation
class TestSeparation:
    def test_overlay_never_touches_real_rows(self, db, users, make_unit):
        fac = _publish(db, users, make_unit)
        view = avail.demo_view(db, fac)
        real_rows = avail.search_rows(view, "Real", "real", False, [BloodGroup.O_POS], [Product.PLATELETS])
        assert demo_stock.overlay(real_rows, view) == real_rows

    def test_real_directory_never_simulates(self, client, db, users, make_unit):
        _publish(db, users, make_unit)
        html = client.get("/find?component=platelets").text
        assert "simulated" not in html and "Demo Blood Centre" not in html
        assert 'data-status="available"' not in html and 'data-status="unavailable"' not in html

    def test_viewing_demo_pages_writes_nothing(self, client, db, users, make_unit):
        _publish(db, users, make_unit)
        before = db.scalar(select(func.count()).select_from(AvailabilityPublication))
        for url in ("/demo", "/demo?group=O-", "/demo?component=platelets"):
            assert client.get(url).status_code == 200
        assert db.scalar(select(func.count()).select_from(AvailabilityPublication)) == before

    def test_outside_demo_mode_nothing_is_simulated(self, db, users, make_unit, monkeypatch):
        fac = _publish(db, users, make_unit)
        view = avail.demo_view(db, fac)
        rows = avail.search_rows(view, "Demo", None, True, [BloodGroup.O_POS], [Product.PLATELETS])
        monkeypatch.setattr(settings, "demo_mode", False)
        assert demo_stock.overlay(rows, view) == rows
