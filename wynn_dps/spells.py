"""Spell definitions, ported from wynnbuilder atree_constants.json.

Only the four base spells per class (no ability-tree variants).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any


@dataclass
class SpellPart:
    name: str
    multipliers: tuple[float, ...] = (0, 0, 0, 0, 0, 0)  # NETWFA in %
    hits: dict[str, float] = field(default_factory=dict)  # composite parts
    use_str: bool = True
    display: bool = True
    is_total: bool = False


@dataclass
class Spell:
    name: str
    slot: int                     # 0=melee, 1..4 = the four spells
    base_cost: int
    display_part: str | None
    use_atkspd: bool | None
    scaling: str                  # "spell" or "melee"
    parts: list[SpellPart] = field(default_factory=list)
    # Spell properties (e.g. arrows_per_stream, attack_frequency). Used by the
    # ability tree to resolve string references like "Arrow Storm.attack_frequency"
    # in hits dicts.
    _properties: dict[str, float] = field(default_factory=dict)


def _parse_spell(raw: dict[str, Any]) -> Spell:
    spell_name = raw.get("name") or "spell"
    properties = dict(raw.get("properties", {}) or {})
    # Build a property lookup so string refs like "Arrow Storm.attack_frequency"
    # resolve at parse time.
    prop_lookup = {f"{spell_name}.{k}": float(v) for k, v in properties.items()}

    def _resolve(v: Any) -> float:
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            return prop_lookup.get(v, 0.0)
        return 0.0

    parts: list[SpellPart] = []
    for p in raw.get("parts", []):
        multipliers = tuple(p.get("multipliers", (0, 0, 0, 0, 0, 0)))
        if len(multipliers) < 6:
            multipliers = tuple(list(multipliers) + [0] * (6 - len(multipliers)))
        hits_raw = p.get("hits", {}) or {}
        hits = {k: _resolve(v) for k, v in hits_raw.items()}
        parts.append(SpellPart(
            name=p["name"],
            multipliers=multipliers,
            hits=hits,
            use_str=p.get("use_str", True),
            display=p.get("display", True),
            is_total=("hits" in p) or (p.get("type") == "total"),
        ))
    slot = int(raw.get("slot", 0))
    return Spell(
        name=spell_name,
        slot=slot,
        base_cost=int(raw.get("cost", 0) or 0),
        display_part=raw.get("display") or None,
        use_atkspd=raw.get("use_atkspd"),
        scaling=raw.get("scaling") or ("melee" if slot == 0 else "spell"),
        parts=parts,
        _properties={k: float(v) for k, v in properties.items()},
    )


def load_spells() -> dict[str, dict[int, Spell]]:
    """Return {class_name: {slot: Spell}} for all 5 classes."""
    raw = json.loads(files("wynn_dps.data").joinpath("spells.json").read_text())
    out: dict[str, dict[int, Spell]] = {}
    for cls, slots in raw.items():
        out[cls] = {int(k): _parse_spell(v) for k, v in slots.items()}
    return out
