"""War-build constraint system.

`BuildConstraints` lets the user set hard min-floors on survivability and
utility stats (mana regen, walk speed, EHP, life steal, etc.). The optimizer
prunes during search when even the optimistic remaining slots can't satisfy
the floors.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Sequence

from .constants import SKP_ELEMENTS, SKP_ORDER

if TYPE_CHECKING:
    from .dps import Build
    from .models import Item


@dataclass
class BuildConstraints:
    """Hard minimums for non-DPS stats. Any None field is unconstrained."""
    min_mana_regen: float | None = None       # per 5s (the wynn unit)
    min_mana_steal: float | None = None       # per 3s (wynn unit)
    min_health_regen_raw: float | None = None
    min_health_regen_pct: float | None = None
    min_life_steal: float | None = None       # per 3s
    min_walk_speed: float | None = None       # %
    min_ehp: float | None = None              # effective HP after defenses
    min_hp: float | None = None               # raw HP
    min_poison: float | None = None           # poison damage stat
    min_per_element_damage: dict[str, float] = field(default_factory=dict)
    min_per_stat: dict[str, int] = field(default_factory=dict)  # str/dex/int/def/agi
    max_skill_points_assigned: int = 200

    def is_empty(self) -> bool:
        return all(getattr(self, f) in (None, 0, {}, 200)
                   for f in ("min_mana_regen", "min_mana_steal",
                              "min_health_regen_raw", "min_health_regen_pct",
                              "min_life_steal", "min_walk_speed", "min_ehp",
                              "min_hp", "min_poison",
                              "min_per_element_damage", "min_per_stat"))


# ---------------------------------------------------------------------------
# Build summary: derived stats used by constraint checks
# ---------------------------------------------------------------------------

def evaluate_build_summary(build: "Build") -> dict[str, float]:
    """Compute every stat a constraint can reference for a fully-specified
    build. Reuses dps._build_ids() and adds derived metrics (HP, EHP, etc.).
    """
    from .dps import _build_ids, _effective_skillpoints

    ids = _build_ids(build)
    eff = _effective_skillpoints(build)

    # HP: class-level base + rawHealth + hpBonus + sum of armor baseHealth.
    raw_hp = ids.get("rawHealth", 0) + ids.get("hpBonus", 0)
    item_base_hp = sum(getattr(it, "base_health", 0) for it in build.armor)
    char_level = getattr(build, "level", 106) or 106
    base_hp = 5 * char_level + 5
    hp = max(1.0, base_hp + raw_hp + item_base_hp)

    # EHP = HP × (1 + average_defense_pct). Use simplified average across the
    # 5 elements; tank builds care about per-element so callers can also use
    # the per_element_def_pct fields below.
    elem_defs = {e: ids.get(f"{e}Defence", 0) for e in SKP_ELEMENTS}
    raw_def = ids.get("rawDefence", 0) + sum(elem_defs.values()) / 5
    ehp = hp * (1.0 + max(0, raw_def) / 100.0)

    summary: dict[str, float] = {
        # Sustain
        "mana_regen": ids.get("manaRegen", 0),
        "mana_steal": ids.get("manaSteal", 0),
        "life_steal": ids.get("lifeSteal", 0),
        "health_regen_raw": ids.get("healthRegenRaw", 0),
        "health_regen_pct": ids.get("healthRegen", 0),
        "poison": ids.get("poison", 0),
        # Mobility
        "walk_speed": ids.get("walkSpeed", 0),
        "jump_height": ids.get("jumpHeight", 0),
        # Survivability
        "hp": hp,
        "ehp": ehp,
        "raw_defence": ids.get("rawDefence", 0),
        # Skill points (effective: assigned + raw stat IDs)
        "strength": eff["strength"],
        "dexterity": eff["dexterity"],
        "intelligence": eff["intelligence"],
        "defence": eff["defence"],
        "agility": eff["agility"],
        # Per-element defenses
        **{f"{e}_defence": elem_defs[e] for e in SKP_ELEMENTS},
        # Per-element damage %s
        **{f"{e}_damage_pct": ids.get(f"{e}Damage", 0) for e in SKP_ELEMENTS},
    }
    # Total assigned skill points
    summary["skill_points_assigned"] = sum(build.skillpoints.values())
    return summary


def meets_constraints(build: "Build", c: BuildConstraints | None) -> bool:
    """Fast pass/fail against a fully-specified build."""
    if c is None or c.is_empty():
        return True
    s = evaluate_build_summary(build)
    if c.min_mana_regen is not None and s["mana_regen"] < c.min_mana_regen:
        return False
    if c.min_mana_steal is not None and s["mana_steal"] < c.min_mana_steal:
        return False
    if c.min_health_regen_raw is not None and s["health_regen_raw"] < c.min_health_regen_raw:
        return False
    if c.min_health_regen_pct is not None and s["health_regen_pct"] < c.min_health_regen_pct:
        return False
    if c.min_life_steal is not None and s["life_steal"] < c.min_life_steal:
        return False
    if c.min_walk_speed is not None and s["walk_speed"] < c.min_walk_speed:
        return False
    if c.min_ehp is not None and s["ehp"] < c.min_ehp:
        return False
    if c.min_hp is not None and s["hp"] < c.min_hp:
        return False
    if c.min_poison is not None and s["poison"] < c.min_poison:
        return False
    for elem, floor in c.min_per_element_damage.items():
        if s.get(f"{elem}_damage_pct", 0) < floor:
            return False
    for stat, floor in c.min_per_stat.items():
        if s.get(stat, 0) < floor:
            return False
    if s["skill_points_assigned"] > c.max_skill_points_assigned:
        return False
    return True


# ---------------------------------------------------------------------------
# Upper-bound pruning support
# ---------------------------------------------------------------------------

# These keys are summed across items as integer/float ID values. The pruner
# assumes additive contributions with no scaling — true for everything except
# percentage-based metrics applied multiplicatively (those still work
# conservatively as upper bounds).
_CONSTRAINT_TO_IDS: dict[str, tuple[str, ...]] = {
    "mana_regen": ("manaRegen",),
    "mana_steal": ("manaSteal",),
    "life_steal": ("lifeSteal",),
    "health_regen_raw": ("healthRegenRaw",),
    "health_regen_pct": ("healthRegen",),
    "walk_speed": ("walkSpeed",),
    "poison": ("poison",),
    "raw_defence": ("rawDefence",),
}


def _slot_max_for_id(pool: Sequence["Item"], id_name: str) -> float:
    """Maximum contribution to `id_name` from any item in this slot's pool."""
    if not pool:
        return 0.0
    return max((it.ids.get(id_name, 0) for it in pool), default=0.0)


def slot_max_summary(
    pools: dict[str, list["Item"]], weapon: "Item",
) -> dict[str, dict[str, float]]:
    """Return {slot: {constraint_key: max_contribution}} for pruning."""
    out: dict[str, dict[str, float]] = {}
    for slot, pool in pools.items():
        slot_caps: dict[str, float] = {}
        for ckey, id_names in _CONSTRAINT_TO_IDS.items():
            slot_caps[ckey] = sum(_slot_max_for_id(pool, n) for n in id_names)
        # Per-element damage % cap
        for e in SKP_ELEMENTS:
            slot_caps[f"{e}_damage_pct"] = _slot_max_for_id(pool, f"{e}Damage")
        # Per-stat raw bonuses
        for stat in SKP_ORDER:
            raw_key = "raw" + stat[0].upper() + stat[1:]
            slot_caps[stat] = _slot_max_for_id(pool, raw_key)
        out[slot] = slot_caps
    # Weapon contribution stays separate; caller adds it as the fixed term.
    return out


def upper_bound_meets(
    weapon: "Item", selected: Sequence["Item"], remaining_slots: Sequence[str],
    slot_caps: dict[str, dict[str, float]], c: BuildConstraints | None,
    sp_assigned_so_far: int = 0,
) -> bool:
    """Optimistic feasibility check: assume remaining slots contribute their
    per-slot max for each constraint metric. If even that can't satisfy the
    floor, prune.

    Returns True if the constraints could *possibly* still be met.
    """
    if c is None or c.is_empty():
        return True

    def _sum_for(key: str) -> float:
        cur = sum(it.ids.get(_CONSTRAINT_TO_IDS.get(key, (key,))[0], 0)
                  for it in selected)
        cur += weapon.ids.get(_CONSTRAINT_TO_IDS.get(key, (key,))[0], 0)
        for slot in remaining_slots:
            cur += slot_caps.get(slot, {}).get(key, 0)
        return cur

    if c.min_mana_regen is not None and _sum_for("mana_regen") < c.min_mana_regen:
        return False
    if c.min_mana_steal is not None and _sum_for("mana_steal") < c.min_mana_steal:
        return False
    if c.min_life_steal is not None and _sum_for("life_steal") < c.min_life_steal:
        return False
    if c.min_health_regen_raw is not None and _sum_for("health_regen_raw") < c.min_health_regen_raw:
        return False
    if c.min_health_regen_pct is not None and _sum_for("health_regen_pct") < c.min_health_regen_pct:
        return False
    if c.min_walk_speed is not None and _sum_for("walk_speed") < c.min_walk_speed:
        return False
    if c.min_poison is not None and _sum_for("poison") < c.min_poison:
        return False
    # Per-element damage % UB
    for elem, floor in c.min_per_element_damage.items():
        cur = sum(it.ids.get(f"{elem}Damage", 0) for it in selected)
        cur += weapon.ids.get(f"{elem}Damage", 0)
        for slot in remaining_slots:
            cur += slot_caps.get(slot, {}).get(f"{elem}_damage_pct", 0)
        if cur < floor:
            return False
    # Per-stat UB (incl. SP that the player can manually assign — up to the
    # remaining budget).
    sp_remaining = max(0, c.max_skill_points_assigned - sp_assigned_so_far)
    for stat, floor in c.min_per_stat.items():
        raw_key = "raw" + stat[0].upper() + stat[1:]
        cur = sum(it.ids.get(raw_key, 0) for it in selected)
        cur += weapon.ids.get(raw_key, 0)
        for slot in remaining_slots:
            cur += slot_caps.get(slot, {}).get(stat, 0)
        cur += sp_remaining  # could put all remaining SP into this stat
        if cur < floor:
            return False
    return True
