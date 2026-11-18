"""Fixed SYNTHETIC component figures and the demo map pin, for the teacher demonstration (/demo only).

- Whole blood on /demo still comes from the fictional demo facility's own (synthetic, seeded) inventory, so the
  request -> approval -> issue workflow visibly lowers it.
- Packed red cells, platelets and plasma are not held in inventory. For the demonstration only, /demo shows the
  FIXED figures below for them. They are constants, so every build and every page load shows the same numbers.
- Nothing here is written to the database, and it is applied only to demo rows (is_demo) while a current demo
  publication exists. The real directory (/find, /api/public/*) never calls this module.
- The demo centre is fictional and has no real location. Its pin is placed at the map's configured default
  centre (settings.map_default_center) and is always labelled as an illustrative position.
"""
from __future__ import annotations

from dataclasses import replace

from app.config import settings
from app.models import BloodGroup, Facility, Product
from app.services.availability import PublicAvailability, SearchRow

G = BloodGroup
# Plausible small-centre figures in bags (units), including zero-stock examples. Deliberately fixed (no randomness).
SIMULATED: dict[Product, dict[BloodGroup, int]] = {
    Product.PACKED_RED_CELLS: {G.A_POS: 7, G.A_NEG: 0, G.B_POS: 8, G.B_NEG: 2,
                               G.AB_POS: 3, G.AB_NEG: 0, G.O_POS: 9, G.O_NEG: 1},
    Product.PLATELETS: {G.A_POS: 3, G.A_NEG: 1, G.B_POS: 3, G.B_NEG: 0,
                        G.AB_POS: 1, G.AB_NEG: 0, G.O_POS: 4, G.O_NEG: 0},
    Product.FRESH_FROZEN_PLASMA: {G.A_POS: 5, G.A_NEG: 1, G.B_POS: 5, G.B_NEG: 0,
                                  G.AB_POS: 2, G.AB_NEG: 1, G.O_POS: 6, G.O_NEG: 1},
}


def overlay(rows: list[SearchRow], view: PublicAvailability) -> list[SearchRow]:
    """Fill the demo facility's untracked components with the fixed simulated figures (demo rows only)."""
    if not settings.demo_mode or not view.is_current:
        return rows                      # stale / withdrawn / off: keep the honest status, never simulate
    out = []
    for r in rows:
        sim = SIMULATED.get(Product(r.product))
        if r.is_demo and sim is not None and r.status == "not_reported":
            n = sim[BloodGroup(r.blood_group)]
            r = replace(r, status="available" if n > 0 else "unavailable", units=n,
                        low=0 < n <= settings.availability_low_max, updated_at=view.published_at, simulated=True)
        out.append(r)
    return out


def map_marker(facility: Facility, view: PublicAvailability) -> dict | None:
    """Pin data for a FICTIONAL demo facility (None for anything else)."""
    if not settings.demo_mode or not facility.is_demo:
        return None
    lat, lon = settings.map_default_center
    stock = []
    if view.is_current:
        for g in BloodGroup:
            c = view.cell(g, Product.WHOLE_BLOOD)
            if c is not None and c.status is not None:
                stock.append({"group": g.value, "units": c.units, "status": c.status.value})
    return {"demo": True, "name": facility.display_name, "lat": lat, "lon": lon, "approximate": True,
            "position_note": "Fictional centre. Illustrative position at the Haldwani map centre, not a real location.",
            "current": view.is_current, "whole_blood": stock,
            "request_url": "/demo/request", "search_url": "/demo?component=whole_blood"}
