"""Compute backend dispatcher: CPU / CUDA / Apple MPS.

Auto-detects the best available accelerator at import time. Provides a
batched DPS function used by the two-stage optimizer's Stage B inner loop
when the candidate count crosses a threshold (default 1024) — below that,
the existing scalar path stays on CPU to avoid GPU-upload overhead.

If `torch` is not installed, falls back to a NumPy CPU implementation. If
NumPy is also unavailable, falls back to pure-Python scalar (no batching).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from .dps import Build
    from .models import Item


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------

_BACKEND: str = "cpu"          # "cuda" | "mps" | "cpu" | "scalar"
_TORCH = None
_NUMPY = None

try:
    import torch as _t          # type: ignore
    _TORCH = _t
    if _t.cuda.is_available():
        _BACKEND = "cuda"
    elif hasattr(_t.backends, "mps") and _t.backends.mps.is_available():
        _BACKEND = "mps"
    else:
        _BACKEND = "cpu"
except ImportError:
    _TORCH = None
    try:
        import numpy as _np      # type: ignore
        _NUMPY = _np
        _BACKEND = "cpu"
    except ImportError:
        _NUMPY = None
        _BACKEND = "scalar"


def backend() -> str:
    """Return the currently-active backend identifier."""
    return _BACKEND


def device():
    """Return the torch.device if torch is available, else None."""
    if _TORCH is None:
        return None
    if _BACKEND == "cuda":
        return _TORCH.device("cuda")
    if _BACKEND == "mps":
        return _TORCH.device("mps")
    return _TORCH.device("cpu")


# ---------------------------------------------------------------------------
# Batched DPS — vectorised port of dps._compute_part_damage for the
# common case (melee per-attack with a single multiplier vector).
#
# Inputs are pre-aggregated stat vectors so the GPU just does the math.
# ---------------------------------------------------------------------------

def dps_batch(
    weapon_dam: Sequence[Sequence[float]],   # (B, 6, 2) - per-build, per-element [min,max]
    md_raw: Sequence[float],                  # (B,)
    md_pct: Sequence[float],                  # (B,) percent
    elem_dam_pct: Sequence[Sequence[float]],  # (B, 5)  — earth..air
    skill_pct: Sequence[Sequence[float]],     # (B, 6)  — neutral, str, dex, int, def, agi (the boost vector)
    str_boost: Sequence[float],               # (B,)
    crit_chance: Sequence[float],             # (B,)
    crit_dmg_bonus: Sequence[float],          # (B,)  fraction (e.g. 0.30 for +30%)
    multipliers: Sequence[float] = (100, 0, 0, 0, 0, 0),
) -> list[float]:
    """Return per-build crit-weighted average per-attack damage.

    Shapes: B = number of candidate builds in the batch.

    The math is a straight tensorisation of `dps._compute_part_damage`
    for the no-power-shift, no-aps, melee-style case. Useful when Stage
    B is enumerating thousands of crafted-ingredient permutations per
    slot.
    """
    if _BACKEND == "scalar" or len(weapon_dam) == 0:
        return _scalar_dps_batch(weapon_dam, md_raw, md_pct, elem_dam_pct,
                                  skill_pct, str_boost, crit_chance, crit_dmg_bonus,
                                  multipliers)
    if _TORCH is not None:
        return _torch_dps_batch(weapon_dam, md_raw, md_pct, elem_dam_pct,
                                 skill_pct, str_boost, crit_chance, crit_dmg_bonus,
                                 multipliers)
    return _numpy_dps_batch(weapon_dam, md_raw, md_pct, elem_dam_pct,
                             skill_pct, str_boost, crit_chance, crit_dmg_bonus,
                             multipliers)


def _scalar_dps_batch(weapon_dam, md_raw, md_pct, elem_dam_pct,
                       skill_pct, str_boost, crit_chance, crit_dmg_bonus,
                       multipliers):
    """Pure-Python fallback. Slow but always works."""
    out = []
    n_b = len(weapon_dam)
    for b in range(n_b):
        wd = weapon_dam[b]
        # weapon_min/max per element [neutral, e, t, w, f, a]
        weapon_min = sum(d[0] for d in wd)
        weapon_max = sum(d[1] for d in wd)
        damages = [[d[0] * multipliers[0] / 100.0, d[1] * multipliers[0] / 100.0]
                   for d in wd]
        for i in range(1, 6):
            f = multipliers[i] / 100.0
            damages[i][0] += f * weapon_min
            damages[i][1] += f * weapon_max
        save = [(d[0], d[1]) for d in damages]
        total_min = sum(d[0] for d in damages)
        total_max = sum(d[1] for d in damages)
        for i in range(6):
            if i == 0:
                boost = 1.0 + skill_pct[b][0] + md_pct[b] / 100.0
            else:
                boost = 1.0 + skill_pct[b][i] + md_pct[b] / 100.0 + elem_dam_pct[b][i - 1] / 100.0
            damages[i][0] *= boost
            damages[i][1] *= boost
        # Raw distribution
        for i in range(6):
            if total_min > 0:
                damages[i][0] += (save[i][0] / total_min) * md_raw[b] * (multipliers[0] / 100.0)
            if total_max > 0:
                damages[i][1] += (save[i][1] / total_max) * md_raw[b] * (multipliers[0] / 100.0)
        sb = str_boost[b]
        crit_mf = 1.0 + crit_dmg_bonus[b]
        dsum_min = sum(d[0] for d in damages)
        dsum_max = sum(d[1] for d in damages)
        nm = dsum_min * sb
        nx = dsum_max * sb
        cm = dsum_min * (sb + crit_mf)
        cx = dsum_max * (sb + crit_mf)
        non_crit = (nm + nx) / 2
        crit = (cm + cx) / 2
        out.append((1 - crit_chance[b]) * non_crit + crit_chance[b] * crit)
    return out


def _numpy_dps_batch(weapon_dam, md_raw, md_pct, elem_dam_pct,
                      skill_pct, str_boost, crit_chance, crit_dmg_bonus,
                      multipliers):
    np = _NUMPY
    wd = np.asarray(weapon_dam, dtype=np.float32)        # (B, 6, 2)
    md_raw = np.asarray(md_raw, dtype=np.float32)        # (B,)
    md_pct = np.asarray(md_pct, dtype=np.float32)        # (B,)
    edp = np.asarray(elem_dam_pct, dtype=np.float32)     # (B, 5)
    sk = np.asarray(skill_pct, dtype=np.float32)         # (B, 6)
    sb = np.asarray(str_boost, dtype=np.float32)         # (B,)
    cc = np.asarray(crit_chance, dtype=np.float32)       # (B,)
    cdb = np.asarray(crit_dmg_bonus, dtype=np.float32)   # (B,)
    mults = np.asarray(multipliers, dtype=np.float32)    # (6,)
    return _np_compute(wd, md_raw, md_pct, edp, sk, sb, cc, cdb, mults).tolist()


def _np_compute(wd, md_raw, md_pct, edp, sk, sb, cc, cdb, mults):
    np = _NUMPY
    B = wd.shape[0]
    weapon_min = wd[:, :, 0].sum(axis=1)         # (B,)
    weapon_max = wd[:, :, 1].sum(axis=1)         # (B,)
    n_conv = mults[0] / 100.0
    dmin = wd[:, :, 0] * n_conv                  # (B, 6)
    dmax = wd[:, :, 1] * n_conv
    for i in range(1, 6):
        f = mults[i] / 100.0
        dmin[:, i] += f * weapon_min
        dmax[:, i] += f * weapon_max
    save_min = dmin.copy()
    save_max = dmax.copy()
    total_min = dmin.sum(axis=1)
    total_max = dmax.sum(axis=1)

    # boost per element
    static_pct = (md_pct / 100.0).reshape(B, 1)             # (B, 1)
    elem_pct_full = np.zeros_like(dmin)
    elem_pct_full[:, 1:] = edp / 100.0
    boost = 1.0 + sk + static_pct + elem_pct_full           # (B, 6)
    dmin_b = dmin * boost
    dmax_b = dmax * boost
    # Raw distribution × total_convert (n_conv).
    safe_min = np.where(total_min > 0, total_min, 1.0)
    safe_max = np.where(total_max > 0, total_max, 1.0)
    raw_share_min = save_min / safe_min[:, None]
    raw_share_max = save_max / safe_max[:, None]
    dmin_b += raw_share_min * md_raw[:, None] * n_conv
    dmax_b += raw_share_max * md_raw[:, None] * n_conv

    dsum_min = dmin_b.sum(axis=1)
    dsum_max = dmax_b.sum(axis=1)
    crit_mf = 1.0 + cdb
    nm = dsum_min * sb
    nx = dsum_max * sb
    cm = dsum_min * (sb + crit_mf)
    cx = dsum_max * (sb + crit_mf)
    non_crit = (nm + nx) / 2
    crit = (cm + cx) / 2
    return (1 - cc) * non_crit + cc * crit


def _torch_dps_batch(weapon_dam, md_raw, md_pct, elem_dam_pct,
                      skill_pct, str_boost, crit_chance, crit_dmg_bonus,
                      multipliers):
    t = _TORCH
    dev = device()
    wd = t.tensor(weapon_dam, dtype=t.float32, device=dev)
    md_raw = t.tensor(md_raw, dtype=t.float32, device=dev)
    md_pct = t.tensor(md_pct, dtype=t.float32, device=dev)
    edp = t.tensor(elem_dam_pct, dtype=t.float32, device=dev)
    sk = t.tensor(skill_pct, dtype=t.float32, device=dev)
    sb = t.tensor(str_boost, dtype=t.float32, device=dev)
    cc = t.tensor(crit_chance, dtype=t.float32, device=dev)
    cdb = t.tensor(crit_dmg_bonus, dtype=t.float32, device=dev)
    mults = t.tensor(multipliers, dtype=t.float32, device=dev)

    B = wd.shape[0]
    weapon_min = wd[:, :, 0].sum(dim=1)
    weapon_max = wd[:, :, 1].sum(dim=1)
    n_conv = mults[0] / 100.0
    dmin = wd[:, :, 0] * n_conv
    dmax = wd[:, :, 1] * n_conv
    for i in range(1, 6):
        f = mults[i] / 100.0
        dmin[:, i] = dmin[:, i] + f * weapon_min
        dmax[:, i] = dmax[:, i] + f * weapon_max
    save_min = dmin.clone()
    save_max = dmax.clone()
    total_min = dmin.sum(dim=1)
    total_max = dmax.sum(dim=1)

    static_pct = (md_pct / 100.0).unsqueeze(1)
    elem_pct_full = t.zeros_like(dmin)
    elem_pct_full[:, 1:] = edp / 100.0
    boost = 1.0 + sk + static_pct + elem_pct_full
    dmin_b = dmin * boost
    dmax_b = dmax * boost
    safe_min = t.where(total_min > 0, total_min, t.ones_like(total_min))
    safe_max = t.where(total_max > 0, total_max, t.ones_like(total_max))
    raw_share_min = save_min / safe_min.unsqueeze(1)
    raw_share_max = save_max / safe_max.unsqueeze(1)
    dmin_b = dmin_b + raw_share_min * md_raw.unsqueeze(1) * n_conv
    dmax_b = dmax_b + raw_share_max * md_raw.unsqueeze(1) * n_conv

    dsum_min = dmin_b.sum(dim=1)
    dsum_max = dmax_b.sum(dim=1)
    crit_mf = 1.0 + cdb
    nm = dsum_min * sb
    nx = dsum_max * sb
    cm = dsum_min * (sb + crit_mf)
    cx = dsum_max * (sb + crit_mf)
    non_crit = (nm + nx) / 2
    crit = (cm + cx) / 2
    out = (1 - cc) * non_crit + cc * crit
    return out.cpu().tolist()
