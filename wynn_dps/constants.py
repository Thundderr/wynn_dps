"""Constants extracted from wynnbuilder (github.com/wynnbuilder/wynnbuilder.github.io)."""

SKP_ORDER = ["strength", "dexterity", "intelligence", "defence", "agility"]
SKP_ELEMENTS = ["earth", "thunder", "water", "fire", "air"]
ELEM_LETTERS = ["e", "t", "w", "f", "a"]
ELEM_TO_SKILL = dict(zip(SKP_ELEMENTS, SKP_ORDER))

ATTACK_SPEEDS = [
    "super_slow", "very_slow", "slow", "normal", "fast", "very_fast", "super_fast"
]
ATTACK_SPEED_MULT = {
    "super_slow": 0.51, "very_slow": 0.83, "slow": 1.5, "normal": 2.05,
    "fast": 2.5, "very_fast": 3.1, "super_fast": 4.3,
}

# str, dex, int, def, agi — only str & dex meaningfully boost damage; def/agi mults
# are kept for parity with wynnbuilder but only affect non-str/dex elemental scaling.
SKILLPOINT_DAMAGE_MULT = [1.0, 1.0, 1.0, 0.867, 0.951]


def skill_points_to_pct(skp: int) -> float:
    if skp <= 0:
        return 0.0
    skp = min(skp, 150)
    r = 0.9908
    return (r / (1 - r) * (1 - r ** skp)) / 100.0


def level_to_skill_points(level: int) -> int:
    if level < 1:
        return 0
    if level >= 101:
        return 200
    return (level - 1) * 2


# Powder stats per element per tier: (dmg_min, dmg_max, convert_pct, def_plus, def_minus)
# Order: [Earth, Thunder, Water, Fire, Air] × tiers 1..6
_POWDER_RAW = {
    "earth":   [(3,6,17,2,1),(5,8,21,4,2),(6,10,25,8,3),(7,10,31,14,5),(9,11,38,22,9),(11,13,46,30,13)],
    "thunder": [(1,8,9,3,1),(1,12,11,5,1),(2,15,13,9,2),(3,15,17,14,4),(4,17,22,20,7),(5,20,28,28,10)],
    "water":   [(3,4,13,3,1),(4,6,15,6,1),(5,8,17,11,2),(6,8,21,18,4),(7,10,26,28,7),(9,11,32,40,10)],
    "fire":    [(2,5,14,3,1),(4,8,16,5,2),(5,9,19,9,3),(6,9,24,16,5),(8,10,30,25,9),(10,12,37,36,13)],
    "air":     [(2,6,11,3,1),(3,10,14,6,2),(4,11,17,10,3),(5,11,22,16,5),(7,12,28,24,9),(8,14,35,34,13)],
}


def powder_stats(element: str, tier: int):
    """Return (min_dmg, max_dmg, convert_pct, def_plus, def_minus) for 1-indexed tier."""
    return _POWDER_RAW[element][tier - 1]


ARMOR_TYPES = ["helmet", "chestplate", "leggings", "boots"]
ACCESSORY_TYPES = ["ring", "bracelet", "necklace"]
WEAPON_TYPES = ["wand", "spear", "bow", "dagger", "relik"]
WEAPON_TO_CLASS = {
    "wand": "mage", "spear": "warrior", "bow": "archer",
    "dagger": "assassin", "relik": "shaman",
}
CLASS_TO_WEAPON = {v: k for k, v in WEAPON_TO_CLASS.items()}

# Crafting constants (from wynnbuilder_ref/js/craft.js:310,329-336).
MATERIAL_TIER_MULT = [0, 1.0, 1.25, 1.4]
ATKSPEED_CRAFT_RATIO = {
    "slow": 2.05 / 1.5,    # 1.3667
    "normal": 1.0,
    "fast": 2.05 / 2.5,    # 0.82
}
# 6 ingredient slots arranged as 3 rows × 2 cols (craft.js:411-412).
INGREDIENT_SLOT_GRID = [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1)]

# Which crafting skill is used for each recipe item-type (craft.js recipe data).
RECIPE_TYPE_TO_SKILL = {
    "HELMET": "ARMOURING", "CHESTPLATE": "ARMOURING",
    "LEGGINGS": "TAILORING", "BOOTS": "TAILORING",
    "RING": "JEWELING", "BRACELET": "JEWELING", "NECKLACE": "JEWELING",
    "WAND": "WOODWORKING", "BOW": "WOODWORKING", "RELIK": "WOODWORKING",
    "SPEAR": "WEAPONSMITHING", "DAGGER": "WEAPONSMITHING",
}
