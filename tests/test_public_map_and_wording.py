"""Base-map provider, tile-server Referer policy, and honest 'no published stock' wording (17 Nov 2026 fixes)."""
from __future__ import annotations

import copy

import pytest

from app.config import BASE_DIR, ConfigurationError, settings, validate_settings
from tests.conftest import login


def test_public_pages_send_referer_friendly_policy(client):
    # OpenStreetMap's tile policy requires a Referer; "same-origin" stripped it and caused 403 "Access blocked".
    for url in ("/", "/find", "/find/about-data"):            # HTML pages that load map tiles
        assert client.get(url).headers["Referrer-Policy"] == "strict-origin-when-cross-origin", url


def test_staff_pages_keep_strict_referrer_policy(client):
    login(client, "staff")
    assert client.get("/donors").headers["Referrer-Policy"] == "same-origin"
    assert client.get("/login").headers["Referrer-Policy"] in ("same-origin", "no-referrer")


def test_raster_provider_is_configurable(client, monkeypatch):
    monkeypatch.setattr(settings, "map_provider", "raster")
    monkeypatch.setattr(settings, "map_tile_url", "https://tiles.example.org/{z}/{x}/{y}.png")
    monkeypatch.setattr(settings, "map_attribution", "Example tiles")
    html = client.get("/find").text
    assert 'data-provider="raster"' in html and "tiles.example.org/{z}/{x}/{y}.png" in html
    assert "maplibre-gl.js" not in html                      # no vector renderer loaded for raster maps
    assert "Example tiles" in html


@pytest.mark.parametrize("field,value", [("map_provider", "google"), ("map_style_url", "http://insecure.example"),
                                         ("map_tile_url", "ftp://tiles.example")])
def test_invalid_map_settings_fail_fast(field, value):
    with pytest.raises(ConfigurationError):
        s = copy.copy(settings)
        setattr(s, field, value)
        validate_settings(s)


def test_no_marker_without_verified_location_under_any_provider(client, monkeypatch):
    for provider in ("openfreemap", "raster"):
        monkeypatch.setattr(settings, "map_provider", provider)
        assert client.get("/api/public/facilities").json()["features"] == []


def test_availability_api_explains_absence_of_data(client):
    body = client.get("/api/public/availability?group=O%2B").json()
    assert body["results"] == [] and body["facilities_reporting"] == 0
    assert "does not mean blood is unavailable" in body["message"]
    assert body["official_portal"].startswith("https://eraktkosh")


def test_directory_states_no_stock_published_without_a_search(client):
    html = " ".join(client.get("/find").text.split())
    assert "No participating blood bank has published current stock on this site yet" in html
    assert "Check stock on e-RaktKosh (official)" in html
    assert "does <strong>not</strong> mean blood is unavailable in Haldwani" in html


# ---------------------------------------------------------------- map sizing (Phase 6A)
# Root cause of the partial-tile map: Leaflet caches its container size and only re-measures on WINDOW resize; the
# MapLibre layer sizes its canvas from that cache. Container-only size changes (split panels, columns, scrollbars,
# the wrapping demo banner) left the canvas stale. These tests pin the fix's code paths; rendering, resize and
# mobile behaviour were verified in headless Chrome (see the Phase 6 report).
MAP_JS = (BASE_DIR / "app" / "static" / "js" / "public_map.js").read_text(encoding="utf-8")
PUBLIC_CSS = (BASE_DIR / "app" / "static" / "css" / "public.css").read_text(encoding="utf-8")


def test_map_resyncs_leaflet_size_when_its_container_changes():
    assert "new ResizeObserver(" in MAP_JS and ".observe(mapEl)" in MAP_JS
    assert "invalidateSize(" in MAP_JS
    assert "keepSizeInSync(map)" in MAP_JS


def test_map_height_uses_measured_header_offset():
    # The fixed 57px assumption ignored the demo banner, pushing the map below the viewport.
    assert "syncHeaderOffset()" in MAP_JS and 'setProperty("--pub-head"' in MAP_JS
    assert 'addEventListener("resize", syncHeaderOffset)' in MAP_JS
    assert "calc(100vh - var(--pub-head" in PUBLIC_CSS


def test_map_shows_loading_state_and_falls_back_to_raster():
    assert 'classList.add("is-loading")' in MAP_JS and 'gl.once("load"' in MAP_JS
    assert '"raster-fallback"' in MAP_JS and "gl.isStyleLoaded()" in MAP_JS
    assert ".pub-map.is-loading::after" in PUBLIC_CSS


def test_markers_still_only_for_server_verified_coordinates():
    # The JS adds a pin only when the server sent coordinates; the server sends them only for verified locations.
    assert "if (f.lat === null || f.lon === null || f.lat === undefined) return;" in MAP_JS
