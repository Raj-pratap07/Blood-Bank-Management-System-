"""PRELIMINARY ABO/Rh red-cell compatibility check for whole blood.

This is a software pre-screen only. It does NOT replace laboratory grouping,
antibody screening, crossmatching or the authorisation of a qualified medical
officer. Note also that for *whole blood* (which contains plasma) many
centres restrict issue to ABO-identical units; this table follows the usual
red-cell compatibility chart and the issuing workflow prefers identical groups.
"""
from __future__ import annotations

from app.models.enums import BloodGroup as G

DISCLAIMER = (
    "Preliminary ABO/Rh software check only. It does not replace laboratory grouping, "
    "crossmatching or authorisation by a qualified medical officer."
)

# recipient -> donor groups whose red cells are ABO/Rh compatible
RECIPIENT_COMPATIBLE_DONORS: dict[G, tuple[G, ...]] = {
    G.O_NEG: (G.O_NEG,),
    G.O_POS: (G.O_POS, G.O_NEG),
    G.A_NEG: (G.A_NEG, G.O_NEG),
    G.A_POS: (G.A_POS, G.A_NEG, G.O_POS, G.O_NEG),
    G.B_NEG: (G.B_NEG, G.O_NEG),
    G.B_POS: (G.B_POS, G.B_NEG, G.O_POS, G.O_NEG),
    G.AB_NEG: (G.AB_NEG, G.A_NEG, G.B_NEG, G.O_NEG),
    G.AB_POS: tuple(G),
}


def compatible_donor_groups(recipient: G | str) -> tuple[G, ...]:
    return RECIPIENT_COMPATIBLE_DONORS[G(recipient)]


def is_compatible(recipient: G | str, donor: G | str) -> bool:
    return G(donor) in RECIPIENT_COMPATIBLE_DONORS[G(recipient)]
