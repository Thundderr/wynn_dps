"""Parse Wynncraft v3 item JSON into ergonomic dataclasses."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .constants import (
    ACCESSORY_TYPES, ARMOR_TYPES, ATTACK_SPEEDS, SKP_ELEMENTS, SKP_ORDER,
    WEAPON_TYPES,
)

# Identification keys relevant to DPS. We use API names directly.
DAMAGE_IDS = {
    "rawMainAttackDamage", "mainAttackDamage",
    "rawSpellDamage", "spellDamage",
    "earthDamage", "thunderDamage", "waterDamage", "fireDamage", "airDamage",
    "earthMainAttackDamage", "thunderMainAttackDamage", "waterMainAttackDamage",
    "fireMainAttackDamage", "airMainAttackDamage",
    "earthSpellDamage", "thunderSpellDamage", "waterSpellDamage",
    "fireSpellDamage", "airSpellDamage",
    "rawAttackSpeed", "criticalDamageBonus",
    "rawStrength", "rawDexterity", "rawIntelligence", "rawDefence", "rawAgility",
    "strength", "dexterity", "intelligence", "defence", "agility",
    # Spell + sustain related.
    "manaRegen", "manaSteal", "poison",
    "1stSpellCost", "2ndSpellCost", "3rdSpellCost", "4thSpellCost",
    "raw1stSpellCost", "raw2ndSpellCost", "raw3rdSpellCost", "raw4thSpellCost",
    # Survivability / utility (used by BuildConstraints & summary).
    "lifeSteal", "healthRegen", "healthRegenRaw", "rawHealth", "hpBonus",
    "walkSpeed", "jumpHeight",
    "earthDefence", "thunderDefence", "waterDefence", "fireDefence", "airDefence",
    "reflection", "thorns",
}


def _max_roll(id_val: Any) -> float:
    """Extract max-roll value from an identification entry."""
    if id_val is None:
        return 0.0
    if isinstance(id_val, (int, float)):
        return float(id_val)
    if isinstance(id_val, dict):
        # API shape: {"min": ..., "raw": ..., "max": ...}
        return float(id_val.get("max", id_val.get("raw", 0)))
    return 0.0


def _normalize_attack_speed(s: str | None) -> str | None:
    if s is None:
        return None
    # API returns camelCase like "superSlow"; convert to snake_case.
    out = []
    for i, ch in enumerate(s):
        if ch.isupper() and i > 0:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


@dataclass
class Item:
    name: str
    type: str              # "weapon" | "armour" | "accessory"
    sub_type: str | None   # "spear", "helmet", "ring", ...
    tier: str
    level: int
    class_req: str | None
    skill_reqs: dict[str, int] = field(default_factory=dict)
    # Weapon base damage per element: element -> (min, max). 'neutral' key possible.
    base_damage: dict[str, tuple[int, int]] = field(default_factory=dict)
    attack_speed: str | None = None
    powder_slots: int = 0
    # Max-roll values for the identifications we care about.
    ids: dict[str, float] = field(default_factory=dict)
    # Armor-only: baseHealth (raw flat HP contribution).
    base_health: int = 0

    @property
    def is_weapon(self) -> bool:
        return self.type == "weapon"

    @property
    def weapon_class(self) -> str | None:
        return self.sub_type if self.is_weapon else None


def parse_item(name: str, raw: dict[str, Any]) -> Item | None:
    t = raw.get("type")
    if t not in ("weapon", "armour", "accessory"):
        return None
    sub = raw.get("subType")
    if t == "weapon" and sub not in WEAPON_TYPES:
        return None
    if t == "armour" and sub not in ARMOR_TYPES:
        return None
    if t == "accessory" and sub not in ACCESSORY_TYPES:
        return None

    reqs = raw.get("requirements", {}) or {}
    level = int(reqs.get("level", 1))
    class_req = reqs.get("classRequirement")
    skill_reqs = {k: int(reqs[k]) for k in SKP_ORDER if k in reqs}

    base_damage: dict[str, tuple[int, int]] = {}
    base = raw.get("base", {}) or {}
    if t == "weapon":
        # baseDamage -> neutral, baseEarthDamage etc -> element
        for key, val in base.items():
            if not key.startswith("base") or not key.endswith("Damage"):
                continue
            mid = key[len("base"):-len("Damage")].lower()  # "", "earth", ...
            elem = "neutral" if mid == "" else mid
            if elem not in ("neutral",) and elem not in SKP_ELEMENTS:
                continue
            if isinstance(val, dict):
                base_damage[elem] = (int(val.get("min", 0)), int(val.get("max", 0)))

    attack_speed = _normalize_attack_speed(raw.get("attackSpeed"))
    if attack_speed is not None and attack_speed not in ATTACK_SPEEDS:
        attack_speed = None

    powder_slots = int(raw.get("powderSlots", 0) or 0)
    base_health = 0
    if t == "armour":
        bh = base.get("baseHealth")
        if isinstance(bh, dict):
            base_health = int(bh.get("raw", bh.get("max", 0)))
        elif isinstance(bh, (int, float)):
            base_health = int(bh)

    ids: dict[str, float] = {}
    for k, v in (raw.get("identifications", {}) or {}).items():
        if k in DAMAGE_IDS:
            ids[k] = _max_roll(v)

    return Item(
        name=name,
        type=t,
        sub_type=sub,
        tier=raw.get("tier", "normal"),
        level=level,
        class_req=class_req,
        skill_reqs=skill_reqs,
        base_damage=base_damage,
        attack_speed=attack_speed,
        powder_slots=powder_slots,
        ids=ids,
        base_health=base_health,
    )


def parse_all(raw_db: dict[str, Any]) -> list[Item]:
    items: list[Item] = []
    for name, raw in raw_db.items():
        parsed = parse_item(name, raw)
        if parsed is not None:
            items.append(parsed)
    return items


def filter_for_class(items: Iterable[Item], wclass: str, max_level: int) -> list[Item]:
    """Return items usable by `wclass` at or below `max_level`."""
    out: list[Item] = []
    for it in items:
        if it.level > max_level:
            continue
        if it.class_req and it.class_req != wclass:
            continue
        if it.is_weapon and it.sub_type != _class_to_weapon(wclass):
            continue
        out.append(it)
    return out


def _class_to_weapon(wclass: str) -> str:
    return {
        "mage": "wand", "warrior": "spear", "archer": "bow",
        "assassin": "dagger", "shaman": "relik",
    }[wclass]


# ---------------------------------------------------------------------------
# Ingredients / recipes / crafted items
# ---------------------------------------------------------------------------

# Map from wynnbuilder-style short id keys (used in recipes/ingredients data)
# back to the v3 API identification names used by Item.ids.
INGREDIENT_ID_MAP = {
    "mdRaw": "rawMainAttackDamage",
    "mdPct": "mainAttackDamage",
    "sdRaw": "rawSpellDamage",
    "sdPct": "spellDamage",
    "eDamPct": "earthDamage", "tDamPct": "thunderDamage",
    "wDamPct": "waterDamage", "fDamPct": "fireDamage", "aDamPct": "airDamage",
    "str": "rawStrength", "dex": "rawDexterity", "int": "rawIntelligence",
    "def": "rawDefence", "agi": "rawAgility",
    "atkTier": "rawAttackSpeed",
    "critDamPct": "criticalDamageBonus",
}

DAMAGE_ID_WEIGHTS = {
    "rawMainAttackDamage": 1.0,
    "mainAttackDamage": 10.0,
    "earthDamage": 8.0, "thunderDamage": 8.0, "waterDamage": 8.0,
    "fireDamage": 8.0, "airDamage": 8.0,
    "earthMainAttackDamage": 8.0, "thunderMainAttackDamage": 8.0,
    "waterMainAttackDamage": 8.0, "fireMainAttackDamage": 8.0,
    "airMainAttackDamage": 8.0,
    "rawStrength": 3.0, "rawDexterity": 3.0,
    "criticalDamageBonus": 3.0,
    "rawAttackSpeed": -30.0,  # negative because lowering tier reduces DPS
}


@dataclass
class Ingredient:
    name: str
    tier: int                          # 0..3
    level: int
    skills: list[str]                  # ["WEAPONSMITHING", ...]
    # Max-roll ids, keyed by wynnbuilder short name (e.g. "mdRaw"); kept short
    # so the craft math is easy to port from craft.js.
    ids_max: dict[str, float] = field(default_factory=dict)
    ids_min: dict[str, float] = field(default_factory=dict)
    # Position modifiers: left/right/above/under/touching/notTouching.
    pos_mods: dict[str, int] = field(default_factory=dict)
    # itemIDs: dura, strReq, dexReq, intReq, defReq, agiReq.
    item_ids: dict[str, int] = field(default_factory=dict)
    is_powder: bool = False
    powder_element: str | None = None  # 'earth', 'thunder', ... if is_powder
    powder_tier: int | None = None


def _parse_ingredient_tier(t: Any) -> int:
    """API returns "TIER_0".."TIER_3"; legacy data returns int 0-3."""
    if isinstance(t, int):
        return t
    if isinstance(t, str) and t.startswith("TIER_"):
        return int(t[5:])
    return 0


def parse_ingredient(name: str, raw: dict[str, Any]) -> Ingredient | None:
    if raw.get("type") != "ingredient":
        return None
    reqs = raw.get("requirements", {}) or {}
    skills = [s.upper() for s in reqs.get("skills", [])]

    ids_min: dict[str, float] = {}
    ids_max: dict[str, float] = {}
    for k, v in (raw.get("identifications", {}) or {}).items():
        # API shape: {min,raw,max} or scalar
        if isinstance(v, dict):
            ids_min[k] = float(v.get("min", 0))
            ids_max[k] = float(v.get("max", 0))
        else:
            ids_min[k] = ids_max[k] = float(v)

    pos = raw.get("ingredientPositionModifiers", {}) or {}
    pos_mods = {k: int(pos.get(k, 0)) for k in
                ("left", "right", "above", "under", "touching", "notTouching")}

    item_raw = raw.get("itemOnlyIDs", {}) or {}
    item_ids = {
        "dura": int(item_raw.get("durabilityModifier", 0)),
        "strReq": int(item_raw.get("strengthRequirement", 0)),
        "dexReq": int(item_raw.get("dexterityRequirement", 0)),
        "intReq": int(item_raw.get("intelligenceRequirement", 0)),
        "defReq": int(item_raw.get("defenceRequirement", 0)),
        "agiReq": int(item_raw.get("agilityRequirement", 0)),
    }

    return Ingredient(
        name=name,
        tier=_parse_ingredient_tier(raw.get("tier")),
        level=int(reqs.get("level", 1)),
        skills=skills,
        ids_max=ids_max,
        ids_min=ids_min,
        pos_mods=pos_mods,
        item_ids=item_ids,
        is_powder=False,
    )


def parse_all_ingredients(raw_db: dict[str, Any]) -> list[Ingredient]:
    out: list[Ingredient] = []
    for name, raw in raw_db.items():
        parsed = parse_ingredient(name, raw)
        if parsed is not None:
            out.append(parsed)
    return out


@dataclass
class Recipe:
    name: str                  # e.g. "Spear-119-121"
    recipe_id: int
    item_type: str             # "SPEAR", "HELMET", ... (upper-case)
    skill: str                 # "WEAPONSMITHING" etc.
    level_min: int
    level_max: int
    base_low: int              # healthOrDamage minimum
    base_high: int             # healthOrDamage maximum
    durability_low: int
    durability_high: int
    material_amounts: tuple[int, int]

    @property
    def is_weapon(self) -> bool:
        return self.item_type in ("SPEAR", "WAND", "BOW", "DAGGER", "RELIK")

    @property
    def is_armor(self) -> bool:
        return self.item_type in ("HELMET", "CHESTPLATE", "LEGGINGS", "BOOTS")

    @property
    def is_accessory(self) -> bool:
        return self.item_type in ("RING", "BRACELET", "NECKLACE")

    @property
    def slot_name(self) -> str:
        return self.item_type.lower()


def parse_recipe(raw: dict[str, Any]) -> Recipe:
    hod = raw["healthOrDamage"]
    dura = raw.get("durability") or raw.get("duration") or {"minimum": 0, "maximum": 0}
    lvl = raw["lvl"]
    mats = raw.get("materials", [])
    amounts = (
        int(mats[0]["amount"]) if len(mats) > 0 else 1,
        int(mats[1]["amount"]) if len(mats) > 1 else 1,
    )
    return Recipe(
        name=raw["name"],
        recipe_id=int(raw.get("id", -1)),
        item_type=raw["type"],
        skill=raw.get("skill", ""),
        level_min=int(lvl["minimum"]),
        level_max=int(lvl["maximum"]),
        base_low=int(hod["minimum"]),
        base_high=int(hod["maximum"]),
        durability_low=int(dura["minimum"]),
        durability_high=int(dura["maximum"]),
        material_amounts=amounts,
    )


def load_recipes(include_non_gear: bool = False) -> list[Recipe]:
    """Load bundled recipes.json. By default filters to weapon/armor/accessory."""
    import json as _json
    from importlib.resources import files
    data = _json.loads(files("wynn_dps.data").joinpath("recipes.json").read_text())
    keep_types = {
        "SPEAR", "WAND", "BOW", "DAGGER", "RELIK",
        "HELMET", "CHESTPLATE", "LEGGINGS", "BOOTS",
        "RING", "BRACELET", "NECKLACE",
    }
    out: list[Recipe] = []
    for r in data["recipes"]:
        if not include_non_gear and r.get("type") not in keep_types:
            continue
        try:
            out.append(parse_recipe(r))
        except (KeyError, TypeError):
            continue
    return out


@dataclass
class CraftedItem(Item):
    """An Item synthesized from a recipe + ingredients. Slots into the same
    DPS pipeline as a regular Item."""
    recipe_name: str = ""
    ingredient_names: list[str] = field(default_factory=list)
    material_tiers: tuple[int, int] = (3, 3)
    crafted_atk_speed: str | None = None

