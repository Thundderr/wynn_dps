"""DPS calculator, ported from wynnbuilder/js/damage_calc.js.

Melee DPS only for the MVP. Uses max-roll identifications.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .constants import (
    ATTACK_SPEEDS, ATTACK_SPEED_MULT, ELEM_TO_SKILL, SKILLPOINT_DAMAGE_MULT,
    SKP_ELEMENTS, SKP_ORDER, powder_stats, skill_points_to_pct,
)
from .models import Item

ELEMENTS_WITH_NEUTRAL = ["neutral"] + SKP_ELEMENTS  # 6 entries


@dataclass
class Build:
    weapon: Item
    armor: list[Item]           # helmet, chest, legs, boots (any subset)
    accessories: list[Item]     # up to 2 rings, 1 brace, 1 necklace
    powders: list[tuple[str, int]]  # [(element, tier), ...] for weapon, len<=slots
    skillpoints: dict[str, int]     # assigned skill points per stat
    # Optional: ability-tree additions (pre-computed via wynn_dps.atree).
    atree_bonuses: dict[str, float] = field(default_factory=dict)        # v3 ID names
    atree_short_bonuses: dict[str, float] = field(default_factory=dict)  # short keys (e.g. tDamAddMin)
    atree_spells: dict[int, "object"] = field(default_factory=dict)      # slot -> Spell
    atree_spell_cost_delta: dict[int, float] = field(default_factory=dict)
    atree_damage_mults: dict[str, float] = field(default_factory=dict)

    def all_items(self) -> list[Item]:
        return [self.weapon, *self.armor, *self.accessories]


def _sum_ids(items: Sequence[Item], extra: dict[str, float] | None = None) -> dict[str, float]:
    total: dict[str, float] = {}
    for it in items:
        for k, v in it.ids.items():
            total[k] = total.get(k, 0.0) + v
    if extra:
        for k, v in extra.items():
            total[k] = total.get(k, 0.0) + v
    return total


def _build_ids(build: "Build") -> dict[str, float]:
    """Sum item IDs + ability-tree bonuses."""
    return _sum_ids(build.all_items(), getattr(build, "atree_bonuses", None))


def _apply_powders(
    base: dict[str, tuple[float, float]], powders: list[tuple[str, int]]
) -> dict[str, tuple[float, float]]:
    """Apply weapon powders to base damage (element -> (min,max)).

    Stacks same-element flats and conversions (per wynnbuilder). Conversion
    consumes from remaining neutral damage.
    """
    # Start with full 6-element vector.
    dmg: dict[str, list[float]] = {
        e: list(base.get(e, (0, 0))) for e in ELEMENTS_WITH_NEUTRAL
    }
    neutral_remaining = list(dmg["neutral"])

    groups: dict[str, dict] = {}
    order: list[str] = []
    for elem, tier in powders:
        mn, mx, conv, _, _ = powder_stats(elem, tier)
        if elem in groups:
            g = groups[elem]
            g["min"] += mn
            g["max"] += mx
            g["conv"] += conv / 100
        else:
            groups[elem] = {"min": mn, "max": mx, "conv": conv / 100}
            order.append(elem)

    for elem in order:
        g = groups[elem]
        min_diff = min(neutral_remaining[0], g["conv"] * neutral_remaining[0])
        max_diff = min(neutral_remaining[1], g["conv"] * neutral_remaining[1])
        neutral_remaining[0] -= min_diff
        neutral_remaining[1] -= max_diff
        dmg[elem][0] += min_diff + g["min"]
        dmg[elem][1] += max_diff + g["max"]

    dmg["neutral"] = neutral_remaining
    return {k: (v[0], v[1]) for k, v in dmg.items()}


def _effective_skillpoints(build: Build) -> dict[str, int]:
    """Assigned skill points + raw<Skill> bonuses from items."""
    ids = _build_ids(build)
    eff: dict[str, int] = {}
    for stat in SKP_ORDER:
        raw_key = "raw" + stat[0].upper() + stat[1:]
        eff[stat] = int(build.skillpoints.get(stat, 0) + ids.get(raw_key, 0))
    return eff


def _attack_speed_mult(weapon: Item, ids_total: dict[str, float]) -> float:
    idx = ATTACK_SPEEDS.index(weapon.attack_speed or "normal")
    shift = int(ids_total.get("rawAttackSpeed", 0))
    idx = max(0, min(len(ATTACK_SPEEDS) - 1, idx + shift))
    return ATTACK_SPEED_MULT[ATTACK_SPEEDS[idx]]


def compute_melee_dps(build: Build) -> float:
    weapon = build.weapon
    ids_total = _build_ids(build)

    # 1. Base weapon damage + powders
    powdered = _apply_powders(weapon.base_damage, build.powders)

    # 2. Skill-point %damage per element
    eff_skp = _effective_skillpoints(build)
    skill_pct = [0.0]  # neutral
    for i, stat in enumerate(SKP_ORDER):
        skill_pct.append(
            skill_points_to_pct(eff_skp[stat]) * SKILLPOINT_DAMAGE_MULT[i]
        )
    # damage_elements order: neutral, earth(=str[0]), thunder(=dex[1]),
    # water(=int[2]), fire(=def[3]), air(=agi[4])
    # skill_pct is already in that order because SKP_ORDER matches SKP_ELEMENTS.

    # 3. ID-based multipliers
    raw_main = ids_total.get("rawMainAttackDamage", 0.0)
    main_pct = ids_total.get("mainAttackDamage", 0.0) / 100.0

    per_elem_main_pct = {
        e: ids_total.get(f"{e}MainAttackDamage", 0.0) / 100.0 for e in SKP_ELEMENTS
    }
    per_elem_pct = {
        e: ids_total.get(f"{e}Damage", 0.0) / 100.0 for e in SKP_ELEMENTS
    }

    # Strength applies to ALL elements as an outer multiplier; per wynnbuilder.
    str_boost = 1.0 + skill_pct[1]  # skill_pct index 1 = earth = strength

    # 4. Compute per-element post-scaling damage (min+max averaged)
    # Add rawMainAttackDamage distributed proportionally across present elements.
    weapon_total_min = sum(v[0] for v in powdered.values())
    weapon_total_max = sum(v[1] for v in powdered.values())
    total_weapon = (weapon_total_min + weapon_total_max) / 2
    if total_weapon <= 0:
        return 0.0

    total_hit = 0.0
    for i, elem in enumerate(ELEMENTS_WITH_NEUTRAL):
        dmin, dmax = powdered.get(elem, (0.0, 0.0))
        if dmax <= 0 and dmin <= 0:
            continue

        # Proportional share of raw main attack damage (flat)
        elem_mid = (dmin + dmax) / 2
        share = elem_mid / total_weapon if total_weapon > 0 else 0
        flat = raw_main * share

        boost = 1.0 + skill_pct[i] + main_pct
        if elem != "neutral":
            boost += per_elem_main_pct[elem] + per_elem_pct[elem]

        hit = (elem_mid + flat) * boost
        total_hit += hit

    total_hit *= str_boost

    # 5. Crit: dexterity gives crit chance, critDamageBonus scales crit bonus
    crit_chance = skill_points_to_pct(eff_skp["dexterity"])
    crit_dmg_bonus = ids_total.get("criticalDamageBonus", 0.0) / 100.0
    avg_hit = total_hit * (1 + crit_chance * (1 + crit_dmg_bonus))

    # 6. Attack speed
    aps = _attack_speed_mult(weapon, ids_total)
    return avg_hit * aps


def requirements_met(build: Build) -> bool:
    eff = _effective_skillpoints(build)
    for it in build.all_items():
        for stat, req in it.skill_reqs.items():
            if eff.get(stat, 0) < req:
                return False
    return True


# ---------------------------------------------------------------------------
# Spell DPS (port of wynnbuilder/js/damage_calc.js calculateSpellDamage)
# ---------------------------------------------------------------------------

def _compute_part_damage(
    build: Build,
    multipliers: Sequence[float],            # NETWFA, in %
    use_spell_damage: bool,
    use_atkspd: bool = True,
    use_str: bool = True,
    ignored_mults: Sequence[str] = (),
) -> float:
    """Average per-cast damage for one spell part (or melee).

    Faithful port of wynnbuilder/js/damage_calc.js calculateSpellDamage:
    tracks per-element [min, max] throughout (no early collapse to mid)
    so float results match wynnbuilder to the same precision.
    """
    weapon = build.weapon
    ids_total = _build_ids(build)
    short = getattr(build, "atree_short_bonuses", {}) or {}
    powdered = _apply_powders(weapon.base_damage, build.powders)

    # Per-element [min, max] arrays — order: neutral, earth, thunder, water, fire, air.
    weapon_dam = []
    for elem in ELEMENTS_WITH_NEUTRAL:
        dmin, dmax = powdered.get(elem, (0.0, 0.0))
        weapon_dam.append([float(dmin), float(dmax)])

    weapon_min = sum(d[0] for d in weapon_dam)
    weapon_max = sum(d[1] for d in weapon_dam)
    if weapon_max <= 0 and weapon_min <= 0:
        return 0.0

    # 1. Spell multipliers as conversions (port of damage_calc.js:67-95).
    neutral_convert = multipliers[0] / 100.0
    damages = [[d[0] * neutral_convert, d[1] * neutral_convert] for d in weapon_dam]
    total_convert = neutral_convert
    present = [d[1] > 0 for d in weapon_dam]
    if neutral_convert == 0:
        present = [False] * 6
    for i in range(1, 6):
        f = multipliers[i] / 100.0
        if f > 0:
            damages[i][0] += f * weapon_min
            damages[i][1] += f * weapon_max
            present[i] = True
            total_convert += f

    # 2. Attack speed (port of damage_calc.js:97-104). Uses BASE atkSpd (not
    # adjusted) per wynnbuilder. atkTier shift only affects the displayed DPS
    # multiplier in cli, NOT the per-attack damage.
    if use_atkspd:
        base_idx = ATTACK_SPEEDS.index(weapon.attack_speed or "normal")
        aps = ATTACK_SPEED_MULT[ATTACK_SPEEDS[base_idx]]
        for i in range(6):
            damages[i][0] *= aps
            damages[i][1] *= aps

    # 3. DamAdd (port of damage_calc.js:106-112).
    elem_letter = ["n", "e", "t", "w", "f", "a"]
    for i in range(6):
        if present[i]:
            damages[i][0] += float(short.get(f"{elem_letter[i]}DamAddMin", 0))
            damages[i][1] += float(short.get(f"{elem_letter[i]}DamAddMax", 0))

    # 4. Skill-point %damage per element (port of damage_calc.js:120-124).
    eff_skp = _effective_skillpoints(build)
    skill_boost = [0.0]
    for i, stat in enumerate(SKP_ORDER):
        skill_boost.append(skill_points_to_pct(eff_skp[stat]) * SKILLPOINT_DAMAGE_MULT[i])

    # 5. % boost per element (port of damage_calc.js:115-145).
    # Convert wynnbuilder short stat names to v3 API names so we can look them up.
    spec = "Sd" if use_spell_damage else "Md"
    if use_spell_damage:
        flat_pct_key, flat_raw_key = "spellDamage", "rawSpellDamage"
    else:
        flat_pct_key, flat_raw_key = "mainAttackDamage", "rawMainAttackDamage"
    spec_pct_per_elem = {  # e.g. tMdPct -> thunderMainAttackDamage
        "e": "earth", "t": "thunder", "w": "water", "f": "fire", "a": "air"
    }
    static_boost = (ids_total.get(flat_pct_key, 0) + ids_total.get("damPct", 0)) / 100.0
    rainbow_pct = (ids_total.get("rainbow" + spec + "Pct", 0) + ids_total.get("rainbowDamage", 0)) / 100.0

    save_prop = [(d[0], d[1]) for d in damages]
    total_min = sum(d[0] for d in damages)
    total_max = sum(d[1] for d in damages)
    elem_pct_v3 = {"e": "earthDamage", "t": "thunderDamage", "w": "waterDamage",
                   "f": "fireDamage", "a": "airDamage"}
    spec_per_elem_v3 = {  # e.g. "t" -> "thunderMainAttackDamage" or "thunderSpellDamage"
        l: spec_pct_per_elem[l] + ("SpellDamage" if use_spell_damage else "MainAttackDamage")
        for l in spec_pct_per_elem
    }
    for i in range(6):
        l = elem_letter[i]
        if l == "n":
            spec_id = 0
            elem_dam_id = 0
        else:
            spec_id = ids_total.get(spec_per_elem_v3[l], 0)
            elem_dam_id = ids_total.get(elem_pct_v3[l], 0)
        damage_boost = 1.0 + skill_boost[i] + static_boost + (spec_id + elem_dam_id) / 100.0
        if i > 0:
            damage_boost += rainbow_pct
        damages[i][0] *= damage_boost
        damages[i][1] *= damage_boost

    total_elem_min = total_min - save_prop[0][0]
    total_elem_max = total_max - save_prop[0][1]

    # 6. Raw application (port of damage_calc.js:150-187).
    prop_raw = ids_total.get(flat_raw_key, 0) + ids_total.get("damRaw", 0)
    rainbow_raw = ids_total.get("rainbow" + spec + "Raw", 0) + ids_total.get("rainbowDamageRaw", 0)
    spec_raw_per_elem_v3 = {
        l: spec_pct_per_elem[l] + ("SpellDamageRaw" if use_spell_damage else "MainAttackDamageRaw")
        for l in spec_pct_per_elem
    }
    for i in range(6):
        l = elem_letter[i]
        save_obj = save_prop[i]
        raw_boost = 0.0
        if present[i] and l != "n":
            raw_boost += ids_total.get(spec_raw_per_elem_v3[l], 0) + ids_total.get(
                elem_pct_v3[l] + "Raw", 0)
        min_boost = raw_boost
        max_boost = raw_boost
        if total_max > 0:
            if total_min == 0:
                min_boost += (save_obj[1] / total_max) * prop_raw
            else:
                min_boost += (save_obj[0] / total_min) * prop_raw
            max_boost += (save_obj[1] / total_max) * prop_raw
        if i != 0 and total_elem_max > 0:
            if total_elem_min == 0:
                min_boost += (save_obj[1] / total_elem_max) * rainbow_raw
            else:
                min_boost += (save_obj[0] / total_elem_min) * rainbow_raw
            max_boost += (save_obj[1] / total_elem_max) * rainbow_raw
        damages[i][0] += min_boost * total_convert
        damages[i][1] += max_boost * total_convert

    # 7. Strength outer multiplier + damMult.* (port of damage_calc.js:189-229).
    str_boost = 1.0 if not use_str else 1.0 + skill_boost[1]
    damage_mult = 1.0
    ele_damage_mult = [1.0] * 6
    mults = getattr(build, "atree_damage_mults", {}) or {}
    for k, v in mults.items():
        if k in ignored_mults:
            continue
        if ":" in k:
            # part-scoped, handled per-spell-part in caller (skip here)
            continue
        if ";" in k:
            elem_match = elem_letter.index(k.split(";")[1]) if k.split(";")[1] in elem_letter else -1
            if elem_match >= 0:
                ele_damage_mult[elem_match] *= 1.0 + v / 100.0
            continue
        damage_mult *= 1.0 + v / 100.0

    crit_dmg_bonus = ids_total.get("criticalDamageBonus", 0) / 100.0
    crit_mult_factor = 0.0 if not use_str else 1.0 + crit_dmg_bonus

    # 8. Apply ele mults, clamp negatives, compute crit-weighted average.
    for i in range(6):
        damages[i][0] *= ele_damage_mult[i]
        damages[i][1] *= ele_damage_mult[i]
        if damages[i][0] < 0:
            damages[i][0] = 0
        if damages[i][1] < 0:
            damages[i][1] = 0

    total_norm_min = sum(d[0] * str_boost * damage_mult for d in damages)
    total_norm_max = sum(d[1] * str_boost * damage_mult for d in damages)
    total_crit_min = sum(d[0] * (str_boost + crit_mult_factor) * damage_mult for d in damages)
    total_crit_max = sum(d[1] * (str_boost + crit_mult_factor) * damage_mult for d in damages)

    non_crit_avg = (total_norm_min + total_norm_max) / 2.0
    crit_avg = (total_crit_min + total_crit_max) / 2.0
    crit_chance = skill_points_to_pct(eff_skp["dexterity"])
    return (1 - crit_chance) * non_crit_avg + crit_chance * crit_avg


def evaluate_spell(build: Build, spell: "Spell") -> dict[str, float]:
    """Return {part_name: damage} for every part of `spell`. Composite parts
    (with `hits`) are resolved by chained multiplication.

    If the build has an ability-tree-modified spell for this slot, that
    overrides the passed-in spell (so callers can pass the base spell freely).
    """
    from .spells import Spell  # noqa: F401
    overrides = getattr(build, "atree_spells", {}) or {}
    if spell.slot in overrides:
        spell = overrides[spell.slot]

    use_spell_dmg = (spell.scaling == "spell")
    use_atkspd = True if spell.use_atkspd is None else bool(spell.use_atkspd)

    results: dict[str, float] = {}
    for part in spell.parts:
        if part.is_total:
            total = 0.0
            for src_name, mult in part.hits.items():
                total += results.get(src_name, 0.0) * float(mult)
            results[part.name] = total
        else:
            d = _compute_part_damage(
                build, part.multipliers,
                use_spell_damage=use_spell_dmg,
                use_atkspd=use_atkspd,
                use_str=part.use_str,
            )
            results[part.name] = d
    return results


def compute_spell_cost(build: Build, spell_slot: int, base_cost: int) -> float:
    """Mana cost after intelligence + atree cost delta + item spRaw<n> + spPct<n>
    reductions, then a final spPct<n>Final pass. Mirrors display.js:1456-1473.
    """
    if spell_slot < 1 or spell_slot > 4:
        return 0.0
    eff_skp = _effective_skillpoints(build)
    int_pct = skill_points_to_pct(eff_skp["intelligence"])
    ids_total = _build_ids(build)
    # Apply ability-tree cost delta to the base before int reduction.
    base_with_atree = base_cost + getattr(build, "atree_spell_cost_delta", {}).get(spell_slot, 0.0)
    cost = base_with_atree * (1.0 - int_pct * 0.5 / skill_points_to_pct(150))
    # Per-spell raw + pct cost reduction from items.
    cost += ids_total.get(f"{_ordinal(spell_slot)}SpellCost", 0.0)
    # Final percent layer (atree spPctNFinal).
    final_pct = ids_total.get(f"{_ordinal(spell_slot)}SpellCostPctFinal", 0.0)
    cost = cost * (1.0 + final_pct / 100.0)
    return max(1.0, cost)


def _ordinal(n: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}.get(n, str(n))


def compute_poison_dps(build: Build) -> float:
    """Wynncraft poison: floor(poison/3) per tick, ticks every 3s for 9s."""
    ids_total = _build_ids(build)
    poison = ids_total.get("poison", 0.0)
    if poison <= 0:
        return 0.0
    tick = poison // 3
    return tick * 3 / 9.0  # 3 ticks over 9s = 1 tick/3s, sustained = tick/3
