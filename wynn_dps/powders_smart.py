"""Element-aligned powder picker.

Replaces the brute-force "try all 5 elements at tier 6" with a focused
search aligned to the weapon's actual base damage elements. Most weapons
have a clear primary (Divzer = thunder; Ascendancy = water+thunder), so
trying all 5 elements is wasteful and occasionally picks the wrong one
when small skill-pct boosts swing the calc.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .constants import SKP_ELEMENTS, SKP_ORDER

if TYPE_CHECKING:
    from .dps import Build
    from .models import Item

# Map skill stat → matching element for boosting.
_SKILL_TO_ELEM = {
    "strength": "earth", "dexterity": "thunder", "intelligence": "water",
    "defence": "fire", "agility": "air",
}


def candidate_elements(weapon: "Item", build: "Build", limit: int = 3) -> list[str]:
    """Pick up to `limit` candidate powder elements for a weapon.

    Strategy:
    1. Elements with non-zero base damage on the weapon (max-rolled).
    2. The element matching the build's strongest skill (so atree/skill
       %damage stacks).
    """
    base = weapon.base_damage or {}
    by_dmg = sorted(
        ((e, base.get(e, (0, 0))[1]) for e in SKP_ELEMENTS),
        key=lambda kv: -kv[1],
    )
    cands = [e for e, dmg in by_dmg if dmg > 0]

    # Add the highest-skill element if not already in cands.
    eff_skp = build.skillpoints
    if eff_skp:
        top_stat = max(SKP_ORDER, key=lambda s: eff_skp.get(s, 0))
        if eff_skp.get(top_stat, 0) > 0:
            elem = _SKILL_TO_ELEM[top_stat]
            if elem not in cands:
                cands.append(elem)

    # Fallback: if weapon is pure-neutral (no elemental base), pick all 5 so
    # we don't return an empty list.
    if not cands:
        cands = list(SKP_ELEMENTS)

    return cands[:limit]


def pick_powders(
    weapon: "Item", build: "Build", constraints=None,
) -> list[tuple[str, int]]:
    """Return the best (element, tier=6) stack for the weapon.

    Picks a single element (all powder slots same element, all tier 6).
    Iterates a small candidate set from `candidate_elements` and selects
    the highest-DPS option that doesn't violate constraints.
    """
    from .constraints import meets_constraints
    from .dps import Build, compute_melee_dps  # local import to avoid cycle

    if weapon.powder_slots <= 0:
        return []

    cands = candidate_elements(weapon, build, limit=3)
    best_dps = -1.0
    best_powders: list[tuple[str, int]] = []
    for elem in cands:
        powders = [(elem, 6)] * weapon.powder_slots
        trial = Build(
            weapon=weapon, armor=build.armor, accessories=build.accessories,
            powders=powders, skillpoints=build.skillpoints,
            atree_bonuses=build.atree_bonuses,
            atree_short_bonuses=build.atree_short_bonuses,
            atree_spells=build.atree_spells,
            atree_spell_cost_delta=build.atree_spell_cost_delta,
            atree_damage_mults=build.atree_damage_mults,
        )
        if constraints is not None and not meets_constraints(trial, constraints):
            continue
        dps = compute_melee_dps(trial)
        if dps > best_dps:
            best_dps = dps
            best_powders = powders
    return best_powders
