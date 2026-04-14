"""Wynncraft Ability Tree engine.

Loads the bundled atree.json (ported from
wynnbuilder/js/builder/atree_constants.json) and applies a player-selected
node set to:
  1. Accumulate raw_stat bonuses (added to the build's effective ID totals).
  2. Mutate spells via add_spell_prop (multipliers, hits, cost, display).
  3. Replace whole spells via replace_spell.

stat_scaling effects (Frenzy hits, etc.) are slider/toggle-driven and are
**not** applied automatically — they require active-combat assumptions.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any

from .spells import Spell, SpellPart, _parse_spell


# ---------------------------------------------------------------------------
# Wynnbuilder short-name -> v3 API name translation
# ---------------------------------------------------------------------------

# Element prefixes used in wynnbuilder short keys.
_ELEM_LETTER_TO_NAME = {"e": "earth", "t": "thunder", "w": "water",
                        "f": "fire", "a": "air"}

WB_TO_V3 = {
    # Generic damage
    "mdPct": "mainAttackDamage", "mdRaw": "rawMainAttackDamage",
    "sdPct": "spellDamage",      "sdRaw": "rawSpellDamage",
    "damPct": "damPct",          "damRaw": "damRaw",
    "critDamPct": "criticalDamageBonus",
    "atkTier": "rawAttackSpeed",
    # Skill points
    "str": "rawStrength", "dex": "rawDexterity", "int": "rawIntelligence",
    "def": "rawDefence",  "agi": "rawAgility",
    # Mana
    "mr": "manaRegen", "ms": "manaSteal",
    # Per-element percent
    **{f"{l}DamPct": f"{n}Damage" for l, n in _ELEM_LETTER_TO_NAME.items()},
    **{f"{l}MdPct":  f"{n}MainAttackDamage" for l, n in _ELEM_LETTER_TO_NAME.items()},
    **{f"{l}SdPct":  f"{n}SpellDamage"      for l, n in _ELEM_LETTER_TO_NAME.items()},
    # Spell cost
    "spRaw1": "1stSpellCost", "spRaw2": "2ndSpellCost",
    "spRaw3": "3rdSpellCost", "spRaw4": "4thSpellCost",
    "spPct1Final": "1stSpellCostPctFinal", "spPct2Final": "2ndSpellCostPctFinal",
    "spPct3Final": "3rdSpellCostPctFinal", "spPct4Final": "4thSpellCostPctFinal",
    # Misc damage-relevant
    "poison": "poison",
}

# Class names in atree.json are TitleCase; our codebase uses lowercase.
CLASS_TITLE = {
    "warrior": "Warrior", "mage": "Mage", "archer": "Archer",
    "assassin": "Assassin", "shaman": "Shaman",
}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

@dataclass
class AtreeNode:
    name: str
    cls: str
    cost: int
    parents: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    archetype: str | None = None
    archetype_req: int = 0
    base_abil: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    effects: list[dict[str, Any]] = field(default_factory=list)


def load_atree() -> dict[str, dict[str, AtreeNode]]:
    """Return {class_lower: {node_display_name: AtreeNode}}."""
    raw = json.loads(files("wynn_dps.data").joinpath("atree.json").read_text())
    out: dict[str, dict[str, AtreeNode]] = {}
    for cls_title, nodes in raw.items():
        cls_lower = cls_title.lower()
        d: dict[str, AtreeNode] = {}
        for n in nodes:
            d[n["display_name"]] = AtreeNode(
                name=n["display_name"], cls=cls_lower,
                cost=int(n.get("cost", 0)),
                parents=n.get("parents", []),
                blockers=n.get("blockers", []),
                archetype=n.get("archetype"),
                archetype_req=int(n.get("archetype_req", 0) or 0),
                base_abil=n.get("base_abil"),
                properties=n.get("properties", {}) or {},
                effects=n.get("effects", []) or [],
            )
        out[cls_lower] = d
    # 'Any' nodes apply to all classes.
    if "any" in out:
        any_nodes = out.pop("any")
        for cls in out:
            for k, v in any_nodes.items():
                out[cls][k] = v
    return out


# ---------------------------------------------------------------------------
# Property resolution: spell properties may be referenced as strings like
# "Arrow Storm.arrows_per_stream"; resolve to numeric values before use.
# ---------------------------------------------------------------------------

def _resolve_props(value: Any, props_lookup: dict[str, float]) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(props_lookup.get(value, 0))
    return 0.0


# ---------------------------------------------------------------------------
# Applying effects
# ---------------------------------------------------------------------------

@dataclass
class AtreeApplied:
    stat_bonuses: dict[str, float] = field(default_factory=dict)   # v3 names
    raw_short_bonuses: dict[str, float] = field(default_factory=dict)  # un-mapped
    spells: dict[int, Spell] = field(default_factory=dict)         # slot -> spell
    spell_cost_delta: dict[int, float] = field(default_factory=dict)
    damage_mults: dict[str, float] = field(default_factory=dict)   # name -> +%
    properties: dict[str, float] = field(default_factory=dict)     # global props
    notes: list[str] = field(default_factory=list)


def _short_to_v3(short: str) -> str | None:
    if short in WB_TO_V3:
        return WB_TO_V3[short]
    return None


def _accumulate_raw_stat(applied: AtreeApplied, eff: dict[str, Any]) -> None:
    for b in eff.get("bonuses", []):
        if b.get("type") != "stat":
            continue
        name = b.get("name")
        value = b.get("value")
        if not isinstance(name, str):
            continue
        # Skip dynamic/conditional bonuses (string values reference props).
        if not isinstance(value, (int, float)):
            continue
        v = float(value)
        if name.startswith("damMult.") or name.startswith("defMult.") \
                or name.startswith("healMult."):
            applied.damage_mults[name] = applied.damage_mults.get(name, 0.0) + v
            continue
        v3 = _short_to_v3(name)
        if v3 is not None:
            applied.stat_bonuses[v3] = applied.stat_bonuses.get(v3, 0.0) + v
        else:
            # Keep flat-additive raw fields like tDamAddMin under their short name.
            applied.raw_short_bonuses[name] = applied.raw_short_bonuses.get(name, 0.0) + v


def _apply_spell_effects(
    applied: AtreeApplied,
    base_spells: dict[int, Spell],
    nodes: list[AtreeNode],
) -> None:
    """First handle replace_spell, then add_spell_prop deltas in node order."""
    spells: dict[int, Spell] = {s: copy.deepcopy(sp) for s, sp in base_spells.items()}
    # Build a global property lookup: "<spell_name>.<prop_key>" -> value
    prop_lookup: dict[str, float] = {}
    for sl, sp in spells.items():
        for k, v in (getattr(sp, "_properties", {}) or {}).items():
            prop_lookup[f"{sp.name}.{k}"] = v

    # Pass 1: replace_spell (also seeds properties).
    for node in nodes:
        for eff in node.effects:
            if eff.get("type") != "replace_spell":
                continue
            slot = int(eff.get("base_spell", 0) or 0)
            if slot < 1 or slot > 4:
                continue
            new_spell = _parse_spell({
                "name": eff.get("name"),
                "slot": slot,
                "cost": eff.get("cost"),
                "display": eff.get("display"),
                "use_atkspd": eff.get("use_atkspd"),
                "scaling": eff.get("scaling"),
                "parts": _resolve_parts(eff.get("parts", []), node.properties,
                                        eff.get("name")),
            })
            spells[slot] = new_spell
            # seed properties for this spell
            for k, v in node.properties.items():
                prop_lookup[f"{eff.get('name')}.{k}"] = v

    # Pass 2: add_spell_prop deltas.
    for node in nodes:
        # Update prop_lookup with this node's properties (additive).
        for k, v in node.properties.items():
            for slot, sp in spells.items():
                prop_lookup[f"{sp.name}.{k}"] = (
                    prop_lookup.get(f"{sp.name}.{k}", 0.0) + v
                )
        for eff in node.effects:
            if eff.get("type") != "add_spell_prop":
                continue
            slot = int(eff.get("base_spell", -1))
            if slot not in spells:
                continue
            sp = spells[slot]
            # Cost delta
            if "cost" in eff:
                applied.spell_cost_delta[slot] = (
                    applied.spell_cost_delta.get(slot, 0.0) + float(eff["cost"])
                )
            # Display change
            if "display" in eff and eff["display"]:
                sp.display_part = eff["display"]
            # Multipliers / hits delta on a target_part
            target = eff.get("target_part")
            if target is None:
                continue
            # Ensure target part exists; if not, create it.
            part = next((p for p in sp.parts if p.name == target), None)
            if part is None:
                part = SpellPart(name=target, multipliers=(0,)*6, hits={},
                                 is_total=("hits" in eff))
                sp.parts.append(part)
            modify = eff.get("behavior") == "modify"
            if "multipliers" in eff:
                if modify:
                    # Replace each non-zero multiplier (per wynnbuilder's modify behavior).
                    m = list(part.multipliers)
                    for i, v in enumerate(eff["multipliers"][:6]):
                        if v != 0:
                            m[i] = float(v)
                    part.multipliers = tuple(m)
                else:
                    m = list(part.multipliers)
                    for i, v in enumerate(eff["multipliers"][:6]):
                        m[i] += float(v)
                    part.multipliers = tuple(m)
            if "hits" in eff:
                for k, v in eff["hits"].items():
                    delta = _resolve_props(v, prop_lookup)
                    if modify:
                        part.hits[k] = delta
                    else:
                        part.hits[k] = part.hits.get(k, 0.0) + delta
                part.is_total = True

    applied.spells = spells


def _resolve_parts(parts: list[dict[str, Any]], props: dict[str, Any],
                   spell_name: str | None) -> list[dict[str, Any]]:
    """Inline string property references inside `hits` dicts of replace_spell parts."""
    if not spell_name:
        return parts
    lookup = {f"{spell_name}.{k}": v for k, v in (props or {}).items()}
    out = []
    for p in parts:
        new_p = dict(p)
        if "hits" in p:
            new_p["hits"] = {k: (lookup.get(v, v) if isinstance(v, str) else v)
                             for k, v in p["hits"].items()}
        out.append(new_p)
    return out


def apply_atree(
    cls: str, selected_node_names: list[str], base_spells: dict[int, Spell],
) -> AtreeApplied:
    """Apply the selected ability-tree nodes for a class."""
    atree = load_atree()
    if cls not in atree:
        raise ValueError(f"unknown class: {cls}")
    nodes_dict = atree[cls]
    selected = []
    missing = []
    for name in selected_node_names:
        if name in nodes_dict:
            selected.append(nodes_dict[name])
        else:
            missing.append(name)
    if missing:
        raise ValueError(
            f"unknown ability-tree nodes for {cls}: {missing}\n"
            f"Available: {sorted(nodes_dict.keys())[:30]}..."
        )

    applied = AtreeApplied()
    for node in selected:
        for eff in node.effects:
            if eff.get("type") == "raw_stat":
                _accumulate_raw_stat(applied, eff)

    _apply_spell_effects(applied, base_spells, selected)
    return applied


def total_ap(selected: list[AtreeNode]) -> int:
    return sum(n.cost for n in selected)


def list_class_nodes(cls: str) -> list[AtreeNode]:
    atree = load_atree()
    return list(atree.get(cls, {}).values())
