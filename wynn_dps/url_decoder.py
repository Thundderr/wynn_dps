"""Port of wynnbuilder's build URL decoder.

Given a URL hash like ``CN00QkSudTvHmXXH2HIE2HYboO...``, returns:
- equipment names (9 slots: helmet, chest, legs, boots, ring1, ring2,
  brace, neck, weapon)
- per-slot powders for the 5 powderable slots
- equipped tomes (14 slots)
- assigned skill points (5 stats)
- character level
- selected ability-tree node names

Limitations:
- Crafted/custom equipment hashes are returned as-is; we don't try to
  reconstruct the crafted item here (use ``wynn_dps.craft.build_crafted_item``
  with the ingredient list separately).
- Aspects are decoded but not yet plumbed into the engine.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

# Wynnbuilder's custom Base64 alphabet (utils.js:105).
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz+-"
DIGITS = {c: i for i, c in enumerate(ALPHABET)}

# Loaded lazily.
_DEC: dict[str, Any] | None = None
_ITEM_BY_ID: list[dict] | None = None
_TOME_BY_ID: list[dict] | None = None


def _load_consts() -> dict[str, Any]:
    global _DEC
    if _DEC is None:
        _DEC = json.loads(files("wynn_dps.data").joinpath("encoding_consts.json").read_text())
    return _DEC


def _load_item_id_map(items_json_path: Path | str) -> dict[int, str]:
    """Map wynnbuilder URL ID → item display/internal name.

    Source must be wynnbuilder's clean.json (or compress.json if it includes
    the per-item `id` field). Items use their own `id`, NOT array index.
    """
    global _ITEM_BY_ID
    if _ITEM_BY_ID is None:
        raw = json.loads(Path(items_json_path).read_text())["items"]
        _ITEM_BY_ID = {
            int(it["id"]): it.get("displayName") or it.get("name")
            for it in raw if "id" in it and it.get("remapID") is None
        }
    return _ITEM_BY_ID


def _load_tome_id_map(tomes_json_path: Path | str) -> dict[int, str]:
    global _TOME_BY_ID
    if _TOME_BY_ID is None:
        raw = json.loads(Path(tomes_json_path).read_text())["tomes"]
        _TOME_BY_ID = {
            int(t["id"]): t.get("displayName") or t.get("name")
            for t in raw if "id" in t
        }
    return _TOME_BY_ID


# ---------------------------------------------------------------------------
# BitVector cursor — port of utils.js BitVector / BitVectorCursor.
# Each base64 char is 6 bits, LSB-first within the char.
# ---------------------------------------------------------------------------

class _Cursor:
    def __init__(self, b64: str):
        # Pre-compute the underlying bit string in the same little-endian-per-char
        # ordering wynnbuilder uses.
        bits = []
        for c in b64:
            v = DIGITS[c]
            for i in range(6):
                bits.append((v >> i) & 1)
        self._bits = bits
        self._len = len(bits)
        self.pos = 0

    def advance(self) -> int:
        b = self._bits[self.pos]
        self.pos += 1
        return b

    def advance_by(self, n: int) -> int:
        if n == 0:
            return 0
        v = 0
        for i in range(n):
            v |= self._bits[self.pos + i] << i
        self.pos += n
        return v

    def read_bit(self, idx: int) -> int:
        return self._bits[idx]

    def remaining(self) -> int:
        return self._len - self.pos


# ---------------------------------------------------------------------------
# Powder decode — port of decodePowders.
# ---------------------------------------------------------------------------

POWDER_ELEMENTS = ["E", "T", "W", "F", "A"]


def _decode_powder_idx(raw: int, num_tiers: int) -> int:
    # Powder IDs are encoded as elem * num_tiers + tier (decode → tier 1-num_tiers)
    return raw  # interpret directly; we expose elem/tier separately below


def _powder_name(pid: int, num_tiers: int) -> str:
    elem = POWDER_ELEMENTS[pid // num_tiers]
    tier = (pid % num_tiers) + 1
    return f"{elem}{tier}"


def _decode_powders(cur: _Cursor, dec: dict[str, Any]) -> list[str]:
    powders = [_decode_powder_idx(cur.advance_by(dec["POWDER_ID_BITLEN"]), dec["POWDER_TIERS"])]
    while True:
        op = cur.advance_by(dec["POWDER_REPEAT_OP"]["BITLEN"])
        if op == dec["POWDER_REPEAT_OP"]["REPEAT"]:
            powders.append(powders[-1])
            continue
        # NO_REPEAT
        sub = cur.advance_by(dec["POWDER_REPEAT_TIER_OP"]["BITLEN"])
        if sub == dec["POWDER_REPEAT_TIER_OP"]["REPEAT_TIER"]:
            wrap = cur.advance_by(dec["POWDER_WRAPPER_BITLEN"])
            prev = powders[-1]
            prev_elem = prev // dec["POWDER_TIERS"]
            prev_tier = prev % dec["POWDER_TIERS"]
            new_elem = (prev_elem + wrap + 1) % len(dec["POWDER_ELEMENTS"])
            powders.append(new_elem * dec["POWDER_TIERS"] + prev_tier)
            continue
        # CHANGE_POWDER
        sub2 = cur.advance_by(dec["POWDER_CHANGE_OP"]["BITLEN"])
        if sub2 == dec["POWDER_CHANGE_OP"]["NEW_POWDER"]:
            powders.append(_decode_powder_idx(
                cur.advance_by(dec["POWDER_ID_BITLEN"]), dec["POWDER_TIERS"]))
            continue
        # NEW_ITEM — exit
        break
    return [_powder_name(p, dec["POWDER_TIERS"]) for p in powders]


# ---------------------------------------------------------------------------
# Atree decode — DFS traversal, 1 bit per visited child.
# ---------------------------------------------------------------------------

def _build_atree_tree(class_nodes: dict[str, dict]) -> dict:
    """Return root-rooted children map for a class's atree.

    Wynnbuilder treats atree as a graph rooted at the base ability (the
    node with no parents). Children are derived from each node's `parents`
    list.
    """
    children: dict[str, list[str]] = {n: [] for n in class_nodes}
    for name, n in class_nodes.items():
        for parent in n.get("parents", []):
            if parent in children:
                children[parent].append(name)
    # Find roots (nodes with no parents)
    roots = [n for n, node in class_nodes.items() if not node.get("parents")]
    if not roots:
        raise ValueError("no atree root found")
    return {"children": children, "root": roots[0]}


def _decode_atree(cur: _Cursor, class_nodes: dict[str, dict]) -> list[str]:
    """Return the list of selected node display names."""
    tree = _build_atree_tree(class_nodes)
    selected: list[str] = [tree["root"]]
    visited: set[str] = set()

    def traverse(head: str) -> None:
        for child in tree["children"][head]:
            if child in visited:
                continue
            visited.add(child)
            if cur.advance() == 1:
                selected.append(child)
                traverse(child)

    traverse(tree["root"])
    return selected


# ---------------------------------------------------------------------------
# Top-level decode
# ---------------------------------------------------------------------------

@dataclass
class DecodedBuild:
    version: int
    equipment: list[Any] = field(default_factory=list)         # 9 names or craft hashes
    powders: list[list[str]] = field(default_factory=list)     # per powderable slot
    tomes: list[str | None] = field(default_factory=list)
    skill_points: list[int | None] = field(default_factory=list)
    level: int = 106
    aspects: list[tuple[str, int] | None] = field(default_factory=list)
    atree_nodes: list[str] = field(default_factory=list)


def decode_build_url(
    hash_str: str,
    cls: str,
    compress_json_path: Path | str,
    tomes_json_path: Path | str,
    atree_path: Path | str | None = None,
) -> DecodedBuild:
    """Decode a wynnbuilder build URL hash. `hash_str` is the part after `#`.

    Returns equipment names (None for empty slots, "CR-..." for crafted),
    powders, tomes, skill points, level, aspects, and selected atree node
    display names.
    """
    if not hash_str:
        raise ValueError("empty hash")
    if DIGITS.get(hash_str[0], 0) <= 11:
        raise ValueError("legacy URL format (first char ≤ 'B') — not supported here")

    dec = _load_consts()
    items = _load_item_id_map(compress_json_path)
    tomes_db = _load_tome_id_map(tomes_json_path)

    cur = _Cursor(hash_str)

    # Header: 6-bit binary flag (always >11 = binary marker) + VERSION_BITLEN.
    cur.advance_by(6)
    version = cur.advance_by(10)  # VERSION_BITLEN

    out = DecodedBuild(version=version)

    # Equipment: 9 slots, slot kinds NORMAL/CRAFTED/CUSTOM. Powders interleaved
    # for the 5 powderable slots: 0,1,2,3 (helmet/chest/legs/boots) and 8 (weapon).
    powderables = {0, 1, 2, 3, 8}
    for slot in range(dec["EQUIPMENT_NUM"]):
        kind = cur.advance_by(dec["EQUIPMENT_KIND"]["BITLEN"])
        if kind == dec["EQUIPMENT_KIND"]["NORMAL"]:
            iid = cur.advance_by(dec["ITEM_ID_BITLEN"])
            if iid == 0:
                out.equipment.append(None)
            else:
                # Encoder adds +1 (build_encode_decode.js:213); decoder subtracts.
                real_id = iid - 1
                out.equipment.append(items.get(real_id, f"<item id {real_id}>"))
        elif kind == dec["EQUIPMENT_KIND"]["CRAFTED"]:
            # 102 bits for the craft hash. We just record the raw b64 representation.
            start = cur.pos
            cur.advance_by(102)
            # Reconstruct the b64 string from the raw bits.
            bits = [cur.read_bit(start + i) for i in range(102)]
            chars = []
            for ci in range(17):
                v = 0
                for bi in range(6):
                    v |= bits[ci * 6 + bi] << bi
                chars.append(ALPHABET[v])
            out.equipment.append("CR-" + "".join(chars))
        elif kind == dec["EQUIPMENT_KIND"]["CUSTOM"]:
            length_chars = cur.advance_by(8)
            cur.advance_by(length_chars * 6)
            out.equipment.append("<custom>")
        else:
            raise ValueError(f"unknown equipment kind {kind} at slot {slot}")

        if slot in powderables:
            flag = cur.advance_by(dec["EQUIPMENT_POWDERS_FLAG"]["BITLEN"])
            if flag == dec["EQUIPMENT_POWDERS_FLAG"]["HAS_POWDERS"]:
                out.powders.append(_decode_powders(cur, dec))
            else:
                out.powders.append([])

    # Tomes
    flag = cur.advance_by(dec["TOMES_FLAG"]["BITLEN"])
    if flag == dec["TOMES_FLAG"]["HAS_TOMES"]:
        for _ in range(dec["TOME_NUM"]):
            slot_flag = cur.advance_by(dec["TOME_SLOT_FLAG"]["BITLEN"])
            if slot_flag == dec["TOME_SLOT_FLAG"]["UNUSED"]:
                out.tomes.append(None)
            else:
                tid = cur.advance_by(dec["TOME_ID_BITLEN"])
                out.tomes.append(tomes_db.get(tid, f"<tome id {tid}>"))
    else:
        out.tomes = [None] * dec["TOME_NUM"]

    # Skill points
    sp_flag = cur.advance_by(dec["SP_FLAG"]["BITLEN"])
    if sp_flag == dec["SP_FLAG"]["AUTOMATIC"]:
        out.skill_points = [None] * dec["SP_TYPES"]
    else:
        for _ in range(dec["SP_TYPES"]):
            elem_flag = cur.advance_by(dec["SP_ELEMENT_FLAG"]["BITLEN"])
            if elem_flag == dec["SP_ELEMENT_FLAG"]["ELEMENT_UNASSIGNED"]:
                out.skill_points.append(None)
            else:
                bits = dec["MAX_SP_BITLEN"]
                raw = cur.advance_by(bits)
                # Sign-extend.
                sign_bit = 1 << (bits - 1)
                if raw & sign_bit:
                    raw -= 1 << bits
                out.skill_points.append(raw)

    # Level
    level_flag = cur.advance_by(dec["LEVEL_FLAG"]["BITLEN"])
    if level_flag == dec["LEVEL_FLAG"]["MAX"]:
        out.level = dec["MAX_LEVEL"]
    else:
        out.level = cur.advance_by(dec["LEVEL_BITLEN"])

    # Aspects
    aspect_flag = cur.advance_by(dec["ASPECTS_FLAG"]["BITLEN"])
    if aspect_flag == dec["ASPECTS_FLAG"]["HAS_ASPECTS"]:
        for _ in range(dec["NUM_ASPECTS"]):
            slot_flag = cur.advance_by(dec["ASPECT_SLOT_FLAG"]["BITLEN"])
            if slot_flag == dec["ASPECT_SLOT_FLAG"]["UNUSED"]:
                out.aspects.append(None)
            else:
                aid = cur.advance_by(dec["ASPECT_ID_BITLEN"])
                tier = cur.advance_by(dec["ASPECT_TIER_BITLEN"]) + 1
                out.aspects.append((f"aspect#{aid}", tier))
    else:
        out.aspects = [None] * dec["NUM_ASPECTS"]

    # Atree
    if atree_path is None:
        atree_data = json.loads(files("wynn_dps.data").joinpath("atree.json").read_text())
    else:
        atree_data = json.loads(Path(atree_path).read_text())
    cls_title = cls[0].upper() + cls[1:]
    class_nodes = {n["display_name"]: n for n in atree_data.get(cls_title, [])}
    if class_nodes:
        out.atree_nodes = _decode_atree(cur, class_nodes)
    else:
        out.atree_nodes = []

    return out
