"""Spell-rotation simulator and mana-sustain analyzer.

Models a build's combat *cycle*: a repeating sequence of melee strikes and
spell casts. For each candidate rotation we compute mean DPS and check that
mana income (regen + mana steal per melee) covers spell expenditure.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Sequence

from .constants import ATTACK_SPEED_MULT, ATTACK_SPEEDS
from .dps import (
    Build, _build_ids, compute_melee_dps, compute_spell_cost, evaluate_spell,
)
from .spells import Spell

# Approximate cast times (seconds). Wynncraft does not expose these in items;
# values are widely-quoted community numbers and adequate for sustain check.
SPELL_CAST_TIME = {0: None, 1: 0.4, 2: 0.4, 3: 0.4, 4: 0.4}


def melee_hits_per_second(build: Build) -> float:
    ids = _build_ids(build)
    idx = ATTACK_SPEEDS.index(build.weapon.attack_speed or "normal")
    shift = int(ids.get("rawAttackSpeed", 0))
    idx = max(0, min(len(ATTACK_SPEEDS) - 1, idx + shift))
    return ATTACK_SPEED_MULT[ATTACK_SPEEDS[idx]]


def mana_per_second(build: Build, melee_per_sec: float) -> float:
    """`mr` ticks: 1 point = +5 mana / 5s = +1 mana/s.
    `ms` per melee: ms mana per hit, scaled by hits/sec.
    """
    ids = _build_ids(build)
    mr = ids.get("manaRegen", 0.0)
    ms = ids.get("manaSteal", 0.0)
    return mr * 1.0 + ms * melee_per_sec


@dataclass
class RotationResult:
    rotation: tuple[str, ...]      # e.g. ("spell1", "melee", "spell1")
    duration_s: float
    total_damage: float
    total_mana_cost: float
    dps: float
    mana_per_sec: float
    sustainable: bool
    mana_deficit_per_sec: float    # 0 if sustainable, else how short we are


def _spell_total_damage(build: Build, spell: Spell) -> float:
    """The 'Total Damage' part if present, else sum of base parts."""
    parts = evaluate_spell(build, spell)
    if spell.display_part and spell.display_part in parts:
        return parts[spell.display_part]
    if "Total Damage" in parts:
        return parts["Total Damage"]
    # Fallback: sum every base (non-total) part.
    base_parts = [p.name for p in spell.parts if not p.is_total]
    return sum(parts.get(n, 0.0) for n in base_parts)


def simulate_rotation(
    build: Build, spells: dict[int, Spell], rotation: Sequence[str],
) -> RotationResult:
    melee_aps = melee_hits_per_second(build)
    if melee_aps <= 0:
        melee_aps = 1.0
    melee_per_hit_dmg = compute_melee_dps(build) / melee_aps  # per hit, not /s

    duration = 0.0
    damage = 0.0
    mana_cost = 0.0
    for action in rotation:
        if action == "melee":
            duration += 1.0 / melee_aps
            damage += melee_per_hit_dmg
        elif action.startswith("spell"):
            slot = int(action[5:])
            spell = spells.get(slot)
            if spell is None:
                continue
            duration += SPELL_CAST_TIME.get(slot, 0.4) or 0.4
            damage += _spell_total_damage(build, spell)
            mana_cost += compute_spell_cost(build, slot, spell.base_cost)

    if duration <= 0:
        return RotationResult(tuple(rotation), 0, 0, 0, 0, 0, False, 0)
    mana_per_s = mana_per_second(build, melee_aps)
    sustained_mana = mana_per_s * duration
    sustainable = sustained_mana >= mana_cost
    deficit = max(0.0, (mana_cost - sustained_mana) / duration)
    return RotationResult(
        rotation=tuple(rotation),
        duration_s=duration,
        total_damage=damage,
        total_mana_cost=mana_cost,
        dps=damage / duration,
        mana_per_sec=mana_per_s,
        sustainable=sustainable,
        mana_deficit_per_sec=deficit,
    )


def optimal_cycle(
    build: Build, spells: dict[int, Spell], damage_slots: tuple[int, ...] = (1, 3),
) -> RotationResult:
    """Pick the highest sustainable DPS from a small set of canonical rotations.

    `damage_slots` are the spell slots considered as primary DPS spells
    (default: slot 1 + slot 3, the two most common archer/warrior big hits).
    """
    candidates: list[tuple[str, ...]] = []
    # all-melee
    candidates.append(("melee",) * 4)
    # spam each damage spell solo (always-on if mana allows)
    for s in damage_slots:
        for n in (1, 2, 3):
            candidates.append((f"spell{s}",) * n)
    # alternating melee/spell to refill mana
    for s in damage_slots:
        candidates.append((f"spell{s}", "melee"))
        candidates.append((f"spell{s}", "melee", "melee"))
        candidates.append((f"spell{s}", "melee", f"spell{s}", "melee"))
    # combo of two damage spells with melee filler
    if len(damage_slots) >= 2:
        a, b = damage_slots[0], damage_slots[1]
        candidates.append((f"spell{a}", "melee", f"spell{b}", "melee"))
        candidates.append((f"spell{a}", f"spell{b}", "melee", "melee"))

    results = [simulate_rotation(build, spells, r) for r in candidates]
    # Prefer sustainable; fall back to highest-DPS rotation regardless.
    sustained = [r for r in results if r.sustainable]
    pool = sustained if sustained else results
    return max(pool, key=lambda r: r.dps)
