"""Wynnbuilder build URL encoder — dual of url_decoder.

Faithful port of build_encode_decode.js encoders. Produces wynnbuilder-
compatible URL hashes that round-trip with our decoder and load correctly
in https://wynnbuilder.github.io/builder/#<hash>.
"""
from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from .url_decoder import (
    ALPHABET, DIGITS, _build_atree_tree, _load_consts, _load_item_id_map,
    _load_tome_id_map,
)

VECTOR_FLAG = 0xC
VERSION_BITLEN = 10
ENCODING_VERSION = 23  # matches v2.1.6.0


# ---------------------------------------------------------------------------
# Bit writer (LSB-first within each base64 char, mirroring the decoder).
# ---------------------------------------------------------------------------

class _Writer:
    def __init__(self) -> None:
        self.bits: list[int] = []

    def append(self, value: int, n_bits: int) -> None:
        for i in range(n_bits):
            self.bits.append((value >> i) & 1)

    def to_b64(self) -> str:
        # Pad to a multiple of 6 with zeros.
        rem = len(self.bits) % 6
        if rem:
            self.bits.extend([0] * (6 - rem))
        out = []
        for i in range(0, len(self.bits), 6):
            v = 0
            for j in range(6):
                v |= self.bits[i + j] << j
            out.append(ALPHABET[v])
        return "".join(out)


# ---------------------------------------------------------------------------
# Element-aware powder encoding (port of collectPowders + encodePowders).
# ---------------------------------------------------------------------------

POWDER_ELEMENTS = ["E", "T", "W", "F", "A"]
POWDER_TIERS = 6


def _powder_to_id(powder_str: str) -> int:
    """Convert "T6" string back to the integer powder ID."""
    elem_letter = powder_str[0].upper()
    tier = int(powder_str[1:])
    elem_idx = POWDER_ELEMENTS.index(elem_letter)
    return elem_idx * POWDER_TIERS + (tier - 1)


def _collect_powders(powders: list[int]) -> list[list[int]]:
    """Group same-element powders together while preserving first-seen order."""
    chunks: list[list[int]] = [[] for _ in POWDER_ELEMENTS]
    order: list[int] = [-1] * len(POWDER_ELEMENTS)
    curr_order = 0
    for p in powders:
        elem_idx = p // POWDER_TIERS
        if order[elem_idx] < 0:
            chunks[curr_order].append(p)
            order[elem_idx] = curr_order
            curr_order += 1
        else:
            chunks[order[elem_idx]].append(p)
    return chunks


def _encode_powders(w: _Writer, powders: list[int], dec: dict[str, Any]) -> None:
    if not powders:
        w.append(dec["EQUIPMENT_POWDERS_FLAG"]["NO_POWDERS"],
                 dec["EQUIPMENT_POWDERS_FLAG"]["BITLEN"])
        return
    w.append(dec["EQUIPMENT_POWDERS_FLAG"]["HAS_POWDERS"],
             dec["EQUIPMENT_POWDERS_FLAG"]["BITLEN"])

    collected = _collect_powders(powders)
    prev = -1
    for chunk in collected:
        i = 0
        while i < len(chunk):
            powder = chunk[i]
            if prev >= 0:
                w.append(dec["POWDER_REPEAT_OP"]["NO_REPEAT"],
                         dec["POWDER_REPEAT_OP"]["BITLEN"])
                if powder % POWDER_TIERS == prev % POWDER_TIERS:
                    w.append(dec["POWDER_REPEAT_TIER_OP"]["REPEAT_TIER"],
                             dec["POWDER_REPEAT_TIER_OP"]["BITLEN"])
                    n_elems = len(POWDER_ELEMENTS)
                    elem = powder % n_elems
                    prev_elem = prev % n_elems
                    wrap = (elem - prev_elem) % n_elems - 1
                    w.append(wrap, dec["POWDER_WRAPPER_BITLEN"])
                else:
                    w.append(dec["POWDER_REPEAT_TIER_OP"]["CHANGE_POWDER"],
                             dec["POWDER_REPEAT_TIER_OP"]["BITLEN"])
                    w.append(dec["POWDER_CHANGE_OP"]["NEW_POWDER"],
                             dec["POWDER_CHANGE_OP"]["BITLEN"])
                    w.append(powder, dec["POWDER_ID_BITLEN"])
            else:
                w.append(powder, dec["POWDER_ID_BITLEN"])
            i += 1
            while i < len(chunk) and chunk[i] == powder:
                w.append(dec["POWDER_REPEAT_OP"]["REPEAT"],
                         dec["POWDER_REPEAT_OP"]["BITLEN"])
                i += 1
            prev = powder

    # End-of-powders sentinel.
    w.append(dec["POWDER_REPEAT_OP"]["NO_REPEAT"], dec["POWDER_REPEAT_OP"]["BITLEN"])
    w.append(dec["POWDER_REPEAT_TIER_OP"]["CHANGE_POWDER"],
             dec["POWDER_REPEAT_TIER_OP"]["BITLEN"])
    w.append(dec["POWDER_CHANGE_OP"]["NEW_ITEM"], dec["POWDER_CHANGE_OP"]["BITLEN"])


# ---------------------------------------------------------------------------
# Atree encoding — DFS traversal, 1 bit per visited child.
# ---------------------------------------------------------------------------

def _encode_atree(
    w: _Writer, selected: list[str], cls: str,
    atree_path: Path | str | None = None,
) -> None:
    if atree_path is None:
        atree_data = json.loads(files("wynn_dps.data").joinpath("atree.json").read_text())
    else:
        atree_data = json.loads(Path(atree_path).read_text())
    cls_title = cls[0].upper() + cls[1:]
    class_nodes = {n["display_name"]: n for n in atree_data.get(cls_title, [])}
    if not class_nodes:
        return

    tree = _build_atree_tree(class_nodes)
    selected_set = set(selected)
    visited: set[str] = set()

    def traverse(head: str) -> None:
        for child in tree["children"][head]:
            if child in visited:
                continue
            visited.add(child)
            if child in selected_set:
                w.append(1, 1)
                traverse(child)
            else:
                w.append(0, 1)

    traverse(tree["root"])


# ---------------------------------------------------------------------------
# Top-level encoder
# ---------------------------------------------------------------------------

def encode_build_url(
    cls: str,
    equipment: list[str | None],          # 9 slots: helmet,...,weapon. None = empty.
    powders: list[list[str]],             # 5 powderable slots, each list of "T6"-style strings
    tomes: list[str | None] | None = None,
    skill_points: list[int | None] | None = None,
    level: int = 106,
    aspects: list[tuple[int, int] | None] | None = None,  # (aspect_id, tier)
    atree_nodes: list[str] | None = None,
    items_json_path: Path | str | None = None,
    tomes_json_path: Path | str | None = None,
    atree_path: Path | str | None = None,
) -> str:
    """Encode a wynnbuilder-compatible build URL hash (without leading '#').

    Equipment names match wynnbuilder's `displayName` (or `name`). Crafted
    items must be passed as their full "CR-..." 20-char hash (the encoder
    detects the prefix and emits the CRAFTED kind).
    """
    dec = _load_consts()
    if items_json_path is None:
        items_json_path = "/Users/aidensmith/test-projects/wynnbuilder_ref/clean.json"
    items_by_id = _load_item_id_map(items_json_path)
    name_to_id = {name: iid for iid, name in items_by_id.items()}

    if tomes_json_path is None:
        tomes_json_path = "/Users/aidensmith/test-projects/wynnbuilder_ref/data/2.1.6.0/tomes.json"
    tomes_by_id = _load_tome_id_map(tomes_json_path)
    tome_name_to_id = {n: tid for tid, n in tomes_by_id.items()}

    w = _Writer()

    # 1. Header.
    w.append(VECTOR_FLAG, 6)
    w.append(ENCODING_VERSION, VERSION_BITLEN)

    # 2. Equipment + powders.
    powderables = {0: 0, 1: 1, 2: 2, 3: 3, 8: 4}  # slot_idx -> powders[] idx
    if len(equipment) != dec["EQUIPMENT_NUM"]:
        raise ValueError(f"need {dec['EQUIPMENT_NUM']} equipment slots, got {len(equipment)}")
    for slot_idx, eq in enumerate(equipment):
        if eq is None:
            # Empty NORMAL slot, id=0.
            w.append(dec["EQUIPMENT_KIND"]["NORMAL"], dec["EQUIPMENT_KIND"]["BITLEN"])
            w.append(0, dec["ITEM_ID_BITLEN"])
        elif isinstance(eq, str) and eq.startswith("CR-"):
            # CRAFTED: 17-char base64 = 102 bits.
            w.append(dec["EQUIPMENT_KIND"]["CRAFTED"], dec["EQUIPMENT_KIND"]["BITLEN"])
            craft_b64 = eq[3:]  # strip "CR-"
            if len(craft_b64) != 17:
                raise ValueError(f"crafted hash must be 17 chars, got {len(craft_b64)}: {eq!r}")
            for ch in craft_b64:
                v = DIGITS[ch]
                w.append(v, 6)
        else:
            # NORMAL item: look up id, encode id+1 (encoder convention).
            if eq not in name_to_id:
                raise ValueError(f"item {eq!r} not in items DB")
            real_id = name_to_id[eq]
            w.append(dec["EQUIPMENT_KIND"]["NORMAL"], dec["EQUIPMENT_KIND"]["BITLEN"])
            w.append(real_id + 1, dec["ITEM_ID_BITLEN"])
        # Powders for powderable slots.
        if slot_idx in powderables:
            p_idx = powderables[slot_idx]
            slot_powders = powders[p_idx] if p_idx < len(powders) else []
            powder_ids = [_powder_to_id(p) for p in slot_powders]
            _encode_powders(w, powder_ids, dec)

    # 3. Tomes.
    tomes = tomes or [None] * dec["TOME_NUM"]
    if all(t is None for t in tomes):
        w.append(dec["TOMES_FLAG"]["NO_TOMES"], dec["TOMES_FLAG"]["BITLEN"])
    else:
        w.append(dec["TOMES_FLAG"]["HAS_TOMES"], dec["TOMES_FLAG"]["BITLEN"])
        for t in tomes[:dec["TOME_NUM"]]:
            if t is None:
                w.append(dec["TOME_SLOT_FLAG"]["UNUSED"], dec["TOME_SLOT_FLAG"]["BITLEN"])
            else:
                if t not in tome_name_to_id:
                    raise ValueError(f"tome {t!r} not in tome DB")
                w.append(dec["TOME_SLOT_FLAG"]["USED"], dec["TOME_SLOT_FLAG"]["BITLEN"])
                w.append(tome_name_to_id[t], dec["TOME_ID_BITLEN"])

    # 4. Skill points.
    skill_points = skill_points or [None] * dec["SP_TYPES"]
    if all(s is None for s in skill_points):
        w.append(dec["SP_FLAG"]["AUTOMATIC"], dec["SP_FLAG"]["BITLEN"])
    else:
        w.append(dec["SP_FLAG"]["ASSIGNED"], dec["SP_FLAG"]["BITLEN"])
        for sp in skill_points:
            if sp is None:
                w.append(dec["SP_ELEMENT_FLAG"]["ELEMENT_UNASSIGNED"],
                         dec["SP_ELEMENT_FLAG"]["BITLEN"])
            else:
                w.append(dec["SP_ELEMENT_FLAG"]["ELEMENT_ASSIGNED"],
                         dec["SP_ELEMENT_FLAG"]["BITLEN"])
                # Two's complement truncation to MAX_SP_BITLEN bits.
                bits = dec["MAX_SP_BITLEN"]
                trunc = sp & ((1 << bits) - 1)
                w.append(trunc, bits)

    # 5. Level.
    if level == dec["MAX_LEVEL"]:
        w.append(dec["LEVEL_FLAG"]["MAX"], dec["LEVEL_FLAG"]["BITLEN"])
    else:
        w.append(dec["LEVEL_FLAG"]["OTHER"], dec["LEVEL_FLAG"]["BITLEN"])
        w.append(level, dec["LEVEL_BITLEN"])

    # 6. Aspects.
    aspects = aspects or [None] * dec["NUM_ASPECTS"]
    if all(a is None for a in aspects):
        w.append(dec["ASPECTS_FLAG"]["NO_ASPECTS"], dec["ASPECTS_FLAG"]["BITLEN"])
    else:
        w.append(dec["ASPECTS_FLAG"]["HAS_ASPECTS"], dec["ASPECTS_FLAG"]["BITLEN"])
        for a in aspects[:dec["NUM_ASPECTS"]]:
            if a is None:
                w.append(dec["ASPECT_SLOT_FLAG"]["UNUSED"], dec["ASPECT_SLOT_FLAG"]["BITLEN"])
            else:
                aid, tier = a
                w.append(dec["ASPECT_SLOT_FLAG"]["USED"], dec["ASPECT_SLOT_FLAG"]["BITLEN"])
                w.append(aid, dec["ASPECT_ID_BITLEN"])
                w.append(tier - 1, dec["ASPECT_TIER_BITLEN"])

    # 7. Atree.
    if atree_nodes:
        _encode_atree(w, atree_nodes, cls, atree_path)

    return w.to_b64()
