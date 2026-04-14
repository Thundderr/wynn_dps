"""Skill-point assignment.

Ports the two-phase approach from wynnbuilder_ref/js/skillpoints.js. For our
optimizer we don't need the full `recurse_check` dependency-ordering search
(which matters when an item *grants* skill points that enable another item).
Instead we use a conservative net-requirement approach: assume the wearer
equips items in the order that lets them satisfy requirements as SP is
granted, which is what wynnbuilder's apply_to_fit + fix_should_pop converges
on when a feasible ordering exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .constants import SKP_ORDER, level_to_skill_points
from .models import Item


def items_sp_gains(items: Sequence[Item]) -> dict[str, int]:
    """Sum of rawStrength/rawDex/etc. granted by equipped items."""
    gains = {s: 0 for s in SKP_ORDER}
    for it in items:
        for stat in SKP_ORDER:
            raw_key = "raw" + stat[0].upper() + stat[1:]
            gains[stat] += int(it.ids.get(raw_key, 0))
    return gains


def total_reqs_met(
    items: Sequence[Item], assigned: dict[str, int], sp_gains: dict[str, int]
) -> bool:
    """Check every item's skill_reqs against (assigned + sp_gains)."""
    for it in items:
        for stat, req in it.skill_reqs.items():
            if assigned.get(stat, 0) + sp_gains.get(stat, 0) < req:
                return False
    return True


def minimum_required_assignment(items: Sequence[Item]) -> dict[str, int]:
    """Smallest assigned SP (per stat) that lets the wearer equip everything,
    accounting for the SP items themselves provide.

    We approximate the optimal ordering: pretend all SP items grant at once
    (best case), then require only the shortfall.
    """
    gains = items_sp_gains(items)
    req_max = {s: 0 for s in SKP_ORDER}
    for it in items:
        for stat, req in it.skill_reqs.items():
            if req > req_max[stat]:
                req_max[stat] = req
    # Only stats that some item actually requires need any SP; negative item
    # bonuses on stats with no requirement are irrelevant.
    needed = {
        s: (max(0, req_max[s] - gains[s]) if req_max[s] > 0 else 0)
        for s in SKP_ORDER
    }
    return needed


@dataclass
class SPAssignment:
    assigned: dict[str, int]   # what the player puts in manually
    total: dict[str, int]      # assigned + sp_gains (effective)
    feasible: bool


def enumerate_assignments(
    items: Sequence[Item], level: int, extra_str_dex_splits: int = 11
) -> list[SPAssignment]:
    """Produce candidate SP allocations for a (fixed) item set.

    Returns <= extra_str_dex_splits feasible allocations. Each dumps leftover
    points into a (str, dex) mix along a coarse grid. int/def/agi get only
    what's needed to meet requirements.
    """
    total_sp = level_to_skill_points(level)
    need = minimum_required_assignment(items)
    gains = items_sp_gains(items)
    used = sum(need.values())
    if used > total_sp:
        return []

    remaining = total_sp - used
    out: list[SPAssignment] = []
    for i in range(extra_str_dex_splits):
        frac_dex = i / (extra_str_dex_splits - 1) if extra_str_dex_splits > 1 else 0.0
        extra_dex = int(round(remaining * frac_dex))
        extra_str = remaining - extra_dex
        assigned = dict(need)
        assigned["strength"] = assigned.get("strength", 0) + extra_str
        assigned["dexterity"] = assigned.get("dexterity", 0) + extra_dex
        total = {s: assigned[s] + gains[s] for s in SKP_ORDER}
        # Double-check feasibility (items may require more than min_assignment
        # estimates if a single item demands the full value without help).
        feasible = total_reqs_met(items, assigned, gains)
        out.append(SPAssignment(assigned=assigned, total=total, feasible=feasible))
    return out
