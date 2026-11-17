"""Base-map provider, tile-server Referer policy, and honest 'no published stock' wording (17 Nov 2026 fixes)."""
from __future__ import annotations

import copy

import pytest

from app.config import ConfigurationError, settings, validate_settings
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
