
from __future__ import annotations

"""Final Fantasy Brave Exvius sprite-sheet importer for Rumble Studio.

Designed around The Spriters Resource FFBE ZIPs whose animation PNGs use names
such as unit_idle_..., unit_atk_..., unit_limit_atk_..., unit_dead_..., etc.

The importer:
- scans a ZIP or extracted folder for character variants
- splits each FFBE PNG sheet into animation frames
- converts those frames to lossless transparent animated PNGs (APNG)
- maps the resulting animations into Rumble Studio slots
- preserves every source PNG inside the fighter folder for later remapping
"""

import io
import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image


class FFBEImportError(RuntimeError):
    pass


ACTION_KEYS = (
    "atk",
    "dead",
    "dying",
    "idle",
    "jump",
    "limit_atk",
    "magic_atk",
    "magic_standby",
    "move",
    "standby",
    "super_limit_atk_before",
    "brave_shift",
    "win",
    "win_before",
)

# Files that are usually one still frame rather than a 3-column sprite sheet.
SINGLE_FRAME_ACTIONS = {"dead", "dying", "jump", "move"}

# Source action -> Rumble animation slot.
SLOT_SOURCE = {
    "idle": "idle",
    "entrance": "win_before",
    "walk": "move",
    "run": "move",
    "hit": "dying",
    "guard": "standby",
    "ko": "dead",
    "attack_1": "atk",
    "attack_2": "magic_atk",
    "attack_3": "limit_atk",
    "jump": "jump",
    "happy": "win",
    "cheer": "win_before",
    "victory_jump": "win_before",
    "victory": "win",
}

ACTION_DURATION_MS = {
    "idle": 140,
    "standby": 140,
    "move": 100,
    "jump": 120,
    "dying": 120,
    "dead": 180,
    "atk": 70,
    "magic_atk": 75,
    "magic_standby": 100,
    "limit_atk": 55,
    "super_limit_atk_before": 65,
    "brave_shift": 65,
    "win": 110,
    "win_before": 85,
}


@dataclass
class FFBEVariant:
    label: str
    display_hint: str
    files: Dict[str, bytes]
    source_label: str
    source_prefix: str = ""

    @property
    def richness(self) -> Tuple[int, int]:
        preferred = sum(1 for k in ("idle", "atk", "move", "dying", "dead", "limit_atk", "win") if k in self.files)
        total = len(self.files)
        return (preferred, total)


def _safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "", name).strip().rstrip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:90] or "Final Fantasy Fighter"


def _archive_display_name(path: Path) -> str:
    stem = path.stem
    # Spriters Resource downloads often look like:
    # Mobile - Final Fantasy_ Brave Exvius - Characters_ Final Fantasy - Warrior of Light
    if " - " in stem:
        tail = stem.split(" - ")[-1].strip()
        if tail:
            return tail
    return stem


def _action_from_name(name: str) -> Optional[str]:
    stem = Path(name).stem.lower()

    # Strip common numeric unit IDs from either front or tail.
    stem = re.sub(r"^\d+[_-]", "", stem)
    stem = re.sub(r"^unit_", "", stem)
    stem = re.sub(r"[_-]\d+(?:\(\d+\))?$", "", stem)

    # Longest/specific actions first so limit_atk does not become atk.
    ordered = (
        "super_limit_atk_before",
        "magic_standby",
        "magic_atk",
        "limit_atk",
        "brave_shift",
        "win_before",
        "standby",
        "dying",
        "dead",
        "idle",
        "jump",
        "move",
        "win",
        "atk",
    )
    for action in ordered:
        if stem == action or stem.endswith("_" + action) or ("_" + action + "_") in ("_" + stem + "_"):
            return action
    return None


def _variant_display_from_dir(dir_name: str, archive_name: str) -> str:
    leaf = Path(dir_name.replace("\\", "/")).name.strip()
    if not leaf:
        return archive_name
    # Prefer meaningful labels such as "Fledgling Warrior NV". Generic numeric
    # folders inherit the archive display name instead.
    if re.fullmatch(r"\d+", leaf):
        return archive_name
    return re.sub(r"\s+", " ", leaf)


def _collect_zip(path: Path) -> List[FFBEVariant]:
    grouped: Dict[str, Dict[str, bytes]] = {}
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".png"):
                continue
            action = _action_from_name(info.filename)
            if not action:
                continue
            parent = str(Path(info.filename).parent).replace("\\", "/")
            grouped.setdefault(parent, {})[action] = zf.read(info.filename)

    archive_name = _archive_display_name(path)
    variants = []
    for parent, files in grouped.items():
        if "idle" not in files:
            continue
        label = parent or archive_name
        variants.append(
            FFBEVariant(
                label=label,
                display_hint=_variant_display_from_dir(parent, archive_name),
                files=files,
                source_label=path.name,
                source_prefix=parent,
            )
        )
    variants.sort(key=lambda v: (v.label.lower(), v.richness), reverse=False)
    return variants


def _collect_folder(path: Path) -> List[FFBEVariant]:
    grouped: Dict[str, Dict[str, bytes]] = {}
    for p in path.rglob("*.png"):
        action = _action_from_name(p.name)
        if not action:
            continue
        parent = str(p.parent.relative_to(path)).replace("\\", "/")
        grouped.setdefault(parent, {})[action] = p.read_bytes()

    archive_name = path.name
    variants = []
    for parent, files in grouped.items():
        if "idle" not in files:
            continue
        variants.append(
            FFBEVariant(
                label=parent or archive_name,
                display_hint=_variant_display_from_dir(parent, archive_name),
                files=files,
                source_label=str(path),
                source_prefix=parent,
            )
        )
    variants.sort(key=lambda v: v.label.lower())
    return variants


def scan_ffbe_source(source: Path) -> List[FFBEVariant]:
    source = source.resolve()
    if source.is_dir():
        variants = _collect_folder(source)
    elif source.suffix.lower() == ".zip":
        variants = _collect_zip(source)
    else:
        raise FFBEImportError("Choose an FFBE Spriters Resource ZIP or an extracted FFBE sprite folder.")
    if not variants:
        raise FFBEImportError(
            "No FFBE character variants were found. Expected PNG names such as "
            "unit_idle_..., unit_atk_..., unit_limit_atk_..., or 201000106_idle.png."
        )
    return variants


def _alpha_mask(im: Image.Image) -> np.ndarray:
    return np.asarray(im.getchannel("A"), dtype=np.uint8) > 0


def _infer_rows(im: Image.Image, min_cell_h: int = 48, max_rows: int = 80) -> int:
    """Infer the FFBE sheet row count.

    Multi-frame FFBE sheets are normally 3 columns wide with transparent
    boundaries between equal-height rows. We choose the largest exact row count
    whose boundaries are transparent and whose every row contains artwork.
    """
    alpha = _alpha_mask(im)
    h, _ = alpha.shape
    total = max(1, int(alpha.sum()))
    candidates = []

    upper = min(max_rows, max(1, h // min_cell_h))
    for rows in range(1, upper + 1):
        if h % rows:
            continue
        cell_h = h // rows
        row_counts = [
            int(alpha[r * cell_h : (r + 1) * cell_h, :].sum())
            for r in range(rows)
        ]
        if not row_counts or min(row_counts) < 8:
            continue

        boundary_pixels = 0
        for r in range(1, rows):
            y = r * cell_h
            boundary_pixels += int(alpha[max(0, y - 1) : min(h, y + 2), :].sum())
        ratio = boundary_pixels / total
        candidates.append((ratio, rows))

    if not candidates:
        return 1

    # Exact/near-exact transparent boundaries are strong evidence. Prefer more
    # rows among those candidates because 1,2,4,... can all be trivially blank.
    # Effects such as sword trails, spell rays, and Limit Burst flashes can
    # cross a nominal row boundary by a few pixels. A slightly tolerant gutter
    # score still cleanly separates real FFBE rows while avoiding the old failure
    # mode where a long animation became three giant vertical strips.
    near_zero = [x for x in candidates if x[0] <= 0.0035]
    if near_zero:
        return max(near_zero, key=lambda x: x[1])[1]

    # Otherwise use the cleanest boundary split.
    return min(candidates, key=lambda x: (x[0], -x[1]))[1]


def _sheet_frames(blob: bytes, action: str) -> List[Image.Image]:
    try:
        im = Image.open(io.BytesIO(blob)).convert("RGBA")
    except Exception as e:
        raise FFBEImportError(f"Could not decode FFBE PNG for {action}: {e}")

    alpha = _alpha_mask(im)
    if not alpha.any():
        return [im]

    if action in SINGLE_FRAME_ACTIONS:
        bbox = im.getbbox()
        return [im.crop(bbox)] if bbox else [im]

    # FFBE animation sheets from these asset packs use three columns.
    cols = 3
    rows = _infer_rows(im)
    cell_w = im.width // cols if im.width % cols == 0 else round(im.width / cols)
    cell_h = im.height // rows if rows and im.height % rows == 0 else round(im.height / max(1, rows))

    frames: List[Image.Image] = []
    for r in range(rows):
        y0 = r * cell_h
        y1 = im.height if r == rows - 1 else min(im.height, (r + 1) * cell_h)
        for c in range(cols):
            x0 = c * cell_w
            x1 = im.width if c == cols - 1 else min(im.width, (c + 1) * cell_w)
            tile = im.crop((x0, y0, x1, y1))
            if tile.getbbox():
                frames.append(tile)

    if not frames:
        bbox = im.getbbox()
        return [im.crop(bbox)] if bbox else [im]

    # Crop all frames to one shared union rectangle. This removes giant empty
    # margins without creating per-frame jitter.
    union = None
    for frame in frames:
        box = frame.getbbox()
        if not box:
            continue
        if union is None:
            union = list(box)
        else:
            union[0] = min(union[0], box[0])
            union[1] = min(union[1], box[1])
            union[2] = max(union[2], box[2])
            union[3] = max(union[3], box[3])
    if union:
        pad = 3
        x0 = max(0, union[0] - pad)
        y0 = max(0, union[1] - pad)
        x1 = min(frames[0].width, union[2] + pad)
        y1 = min(frames[0].height, union[3] + pad)
        frames = [f.crop((x0, y0, x1, y1)) for f in frames]

    return frames


def _save_animation(blob: bytes, action: str, path: Path) -> int:
    """Save FFBE animation as APNG so per-frame transparency stays lossless."""
    frames = _sheet_frames(blob, action)
    duration = ACTION_DURATION_MS.get(action, 90)
    path.parent.mkdir(parents=True, exist_ok=True)

    first, rest = frames[0], frames[1:]
    if rest:
        first.save(
            path,
            format="PNG",
            save_all=True,
            append_images=rest,
            duration=duration,
            loop=0,
            disposal=1,
            blend=0,
            optimize=False,
        )
    else:
        first.save(path, format="PNG", optimize=False)
    return len(frames)


def _default_attacks(available: Dict[str, str]) -> List[dict]:
    attacks = []
    if "attack_1" in available:
        attacks.append({
            "name": "Attack",
            "type": "melee",
            "animation": "attack_1",
            "damage": 12,
            "range": 62,
            "cooldown": 0.90,
            "knockback": 11,
            "effect_color": "#FFD45A",
            "weight": 1.0,
        })
    if "attack_2" in available:
        attacks.append({
            "name": "Magic",
            "type": "projectile",
            "animation": "attack_2",
            "damage": 15,
            "range": 320,
            "cooldown": 1.30,
            "knockback": 12,
            "projectile_speed": 310,
            "hit_radius": 20,
            "min_range": 65,
            "effect_color": "#78CFFF",
            "weight": 0.85,
        })
    if "attack_3" in available:
        attacks.append({
            "name": "Limit Burst",
            "type": "beam",
            "animation": "attack_3",
            "damage": 20,
            "range": 360,
            "cooldown": 2.00,
            "knockback": 18,
            "min_range": 75,
            "effect_color": "#FFD060",
            "weight": 0.45,
        })
    if "attack_4" in available:
        attacks.append({
            "name": "Special",
            "type": "melee",
            "animation": "attack_4",
            "damage": 18,
            "range": 70,
            "cooldown": 1.65,
            "knockback": 15,
            "effect_color": "#FF9A58",
            "weight": 0.55,
        })
    return attacks


def import_ffbe_variant(
    variant: FFBEVariant,
    fighters_dir: Path,
    display_name: Optional[str] = None,
    overwrite: bool = False,
) -> Path:
    display = _safe_name(display_name or variant.display_hint)
    target = fighters_dir / display

    if target.exists() and not overwrite:
        old = {}
        try:
            old = json.loads((target / "fighter.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        if old.get("source", {}).get("format") != "FFBE Spriters Resource Sprite Sheets":
            base = target
            n = 2
            while target.exists():
                target = fighters_dir / f"{base.name} FFBE {n}"
                n += 1

    target.mkdir(parents=True, exist_ok=True)
    src_dir = target / "ffbe_source"
    src_dir.mkdir(parents=True, exist_ok=True)

    # Preserve source sheets and convert each recognized animation to GIF.
    generated: Dict[str, str] = {}
    frame_counts: Dict[str, int] = {}
    for action, blob in sorted(variant.files.items()):
        source_png = src_dir / f"{action}.png"
        source_png.write_bytes(blob)
        # v0.16.2: APNG avoids GIF palette/disposal artifacts that could turn
        # transparent attack frames into large black rectangles.
        anim_name = f"ffbe_{action}.png"
        count = _save_animation(blob, action, target / anim_name)
        generated[action] = anim_name
        frame_counts[action] = count

        # Safe cleanup of the older generated FFBE GIF for this same action.
        # Original source sheets remain preserved in ffbe_source/.
        (target / f"ffbe_{action}.gif").unlink(missing_ok=True)

    animations: Dict[str, str] = {}
    for slot, source_action in SLOT_SOURCE.items():
        if source_action in generated:
            animations[slot] = generated[source_action]

    # Optional fourth attack source for packs that include an extra animation.
    extra_action = None
    for candidate in ("super_limit_atk_before", "brave_shift"):
        if candidate in generated:
            extra_action = candidate
            break
    if extra_action:
        animations["attack_4"] = generated[extra_action]

    # Fallbacks keep sparse packs usable.
    if "idle" not in animations:
        raise FFBEImportError(f"{display} is missing an idle animation.")
    animations.setdefault("walk", animations["idle"])
    animations.setdefault("run", animations.get("walk", animations["idle"]))
    animations.setdefault("hit", animations["idle"])
    animations.setdefault("guard", animations["idle"])
    animations.setdefault("ko", animations["idle"])
    animations.setdefault("happy", animations["idle"])
    animations.setdefault("cheer", animations.get("happy", animations["idle"]))
    animations.setdefault("victory_jump", animations.get("happy", animations["idle"]))
    animations.setdefault("victory", animations.get("happy", animations["idle"]))

    old = {}
    try:
        old = json.loads((target / "fighter.json").read_text(encoding="utf-8"))
    except Exception:
        pass

    attacks = old.get("attacks") or _default_attacks(animations)

    meta = {
        "name": display,
        "franchise": old.get("franchise", "Final Fantasy"),
        "series_game": old.get("series_game", ""),
        "character_family": old.get("character_family", display),
        "stage": old.get("stage", ""),
        "source_tier": old.get("source_tier", _ffbe_source_tier(variant.label)),
        "native_facing": old.get("native_facing", "left"),
        "allow_horizontal_flip": old.get("allow_horizontal_flip", True),
        "scale": old.get("scale", 1.6),
        "stats": old.get("stats", {"hp": 100, "speed": 1.0, "attack": 1.0, "defense": 1.0}),
        "animations": animations,
        "attacks": attacks,
        "ai": old.get("ai") or {"profile": "auto", "retaliation": 0.55, "evasion": 0.28},
        "transformations": old.get("transformations", []),
        "tags": ["Final Fantasy", "FFBE", "The Spriters Resource"],
        "source": {
            "format": "FFBE Spriters Resource Sprite Sheets",
            "source_label": variant.source_label,
            "variant": variant.label,
            "frame_counts": frame_counts,
            "source_actions": sorted(variant.files.keys()),
        },
        "mugen_locks": old.get("mugen_locks", {"animations": [], "attacks": []}),
    }
    (target / "fighter.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    lines = [
        "RUMBLE STUDIO - FINAL FANTASY BRAVE EXVIUS IMPORT",
        "=================================================",
        f"Fighter: {display}",
        f"Source: {variant.source_label}",
        f"Variant: {variant.label}",
        "",
        "Detected source actions:",
    ]
    for action in sorted(variant.files):
        lines.append(f"  {action:24s} -> {frame_counts.get(action, 0)} frame(s)")
    lines += ["", "Rumble animation mapping:"]
    for slot, filename in animations.items():
        lines.append(f"  {slot:14s} <- {filename}")
    lines += [
        "",
        "Notes:",
        "- Every recognized source PNG is preserved in ffbe_source/.",
        "- Generic Attack / Magic / Limit Burst move names are placeholders.",
        "- Edit stats, AI, move names/types, and transformations in Fighter Editor.",
    ]
    (target / "FFBE_IMPORT_REPORT.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def import_ffbe_source(
    source: Path,
    fighters_dir: Path,
    variant_indexes: Optional[List[int]] = None,
    overwrite: bool = False,
) -> dict:
    variants = scan_ffbe_source(source)

    if variant_indexes is None:
        # Default to the richest variant if the caller did not explicitly choose.
        best_i = max(range(len(variants)), key=lambda i: (variants[i].richness, i))
        variant_indexes = [best_i]

    chosen = []
    for i in variant_indexes:
        if i < 0 or i >= len(variants):
            raise FFBEImportError(f"Variant index {i + 1} is out of range.")
        chosen.append((i, variants[i]))

    imported = []
    skipped = []
    multi = len(chosen) > 1
    archive_name = _archive_display_name(source)

    for _, variant in chosen:
        try:
            display = variant.display_hint if multi else archive_name
            folder = import_ffbe_variant(variant, fighters_dir, display_name=display, overwrite=overwrite)
            imported.append((variant, folder))
        except Exception as e:
            skipped.append((variant, str(e)))

    if not imported:
        detail = skipped[0][1] if skipped else "No FFBE variants were importable."
        raise FFBEImportError(detail)

    return {
        "variants": variants,
        "imported": imported,
        "skipped": skipped,
        "count": len(imported),
        "first_folder": imported[0][1],
    }
