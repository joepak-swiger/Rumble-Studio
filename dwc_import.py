from __future__ import annotations

"""Digimon World Championship 40-GIF sprite-set importer.

Temporary convenience importer for the community sprite archive used while
Rumble Studio's generic/MUGEN import paths mature. It understands filenames of
this form:

    m201_agumon_main_0.gif ... m201_agumon_main_39.gif

Choose ONE GIF and the importer finds the other states beside it, or choose the
full ZIP to bulk-import a stage such as Rookie.
"""

import io
import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from PIL import Image


class DWCImportError(RuntimeError):
    pass


STATE_LABELS = {
    0: "Stand-Eyes Opened",
    1: "Stand-Eyes Closed",
    2: "Walk",
    3: "Run",
    4: "Surprised",
    5: "Hit",
    6: "Jump/Hit",
    7: "Attack 1",
    8: "Attack 2",
    9: "Attack 3",
    10: "Attack 4",
    11: "Run Scared",
    12: "Walk-Eyes Closed",
    13: "Sleep",
    14: "Eat",
    15: "Sleep2",
    16: "Tied Stand-Eyes Opened",
    17: "Tied Stand-Eyes Closed",
    18: "Tied Walk",
    19: "Tied Run",
    20: "Tied Surprised",
    21: "Tied Run Scared",
    22: "Tied Walk-Eyes Closed",
    23: "Tied Sleep",
    24: "Tied Eat",
    25: "Tied Sleep2",
    26: "Zapped",
    27: "Unknown 27",
    28: "Happy",
    29: "Angry",
    30: "Cheering",
    31: "Guard",
    32: "Train",
    33: "Shout",
    34: "Guard2",
    35: "Shout-Frame2",
    36: "Unknown 36",
    37: "Knocked Out",
    38: "Jump/Hit 2",
    39: "Unknown 39",
}

# Slots the current Rumble engine actually understands.
RUMBLE_SLOT_MAP = {
    "idle": 0,
    "walk": 2,
    "run": 3,
    "hit": 5,
    "jump": 6,
    "attack_1": 7,
    "attack_2": 8,
    "attack_3": 9,
    "attack_4": 10,
    "happy": 28,
    "cheer": 30,
    "guard": 31,
    "ko": 37,
    "victory_jump": 38,
    "victory": 30,
}

# Common English/localized display names for the archive's Rookie set.
DISPLAY_NAMES = {
    "agumon": "Agumon",
    "armadimon": "Armadillomon",
    "elecmon": "Elecmon",
    "otamamon": "Otamamon",
    "gaomon": "Gaomon",
    "gazimon": "Gazimon",
    "gabumon": "Gabumon",
    "kamemon": "Kamemon",
    "gizamon": "Gizamon",
    "candmon": "Candlemon",
    "kunemon": "Kunemon",
    "gottumon": "Gotsumon",
    "goburimon": "Goblimon",
    "gomamon": "Gomamon",
    "terriermon": "Terriermon",
    "tentomon": "Tentomon",
    "toyagumon": "ToyAgumon",
    "dracomon": "Dracomon",
    "bakumon": "Bakumon",
    "hagurumon": "Hagurumon",
    "patamon": "Patamon",
    "palmon": "Palmon",
    "picodevimon": "DemiDevimon",
    "piyomon": "Biyomon",
    "falcomon": "Falcomon",
    "plotmon": "Salamon",
    "floramon": "Floramon",
    "mushmon": "Mushmon",
    "yukiagumon": "SnowAgumon",
    "renamon": "Renamon",
    "coronamon": "Coronamon",
    "lunamon": "Lunamon",
    "kuroagumon": "BlackAgumon",
    "psychemon": "Psychemon",
    "alraumon": "Alraumon",
    "tsukaimon": "Tsukaimon",
    "vmon": "Veemon",
    "guilmon": "Guilmon",
    "graymon": "Greymon",
    "metalgreymon_vi": "MetalGreymon",
    "metalgreymon_va": "MetalGreymon (Virus)",
    "warglaymon": "WarGreymon",
}

FILE_RE = re.compile(r"(?P<prefix>m(?P<id>\d+)_(?P<slug>.+?)_main)_(?P<state>\d+)\.gif$", re.I)


@dataclass
class DWCSet:
    digimon_id: str
    slug: str
    stage: str
    states: Dict[int, bytes]
    source_label: str

    @property
    def display_name(self) -> str:
        if self.slug.lower() in DISPLAY_NAMES:
            return DISPLAY_NAMES[self.slug.lower()]
        return "".join(x.capitalize() for x in re.split(r"[_\- ]+", self.slug) if x)


def _safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '', name).strip().rstrip('.')
    return re.sub(r'\s+', ' ', name)[:80] or 'Digimon Fighter'


def _stage_from_path(path: str) -> str:
    first = path.replace('\\', '/').split('/')[0]
    m = re.match(r"\s*\d+\s*-\s*(.+?)\s*$", first)
    return m.group(1) if m else first or "Unknown"


def _collect_zip(path: Path, stage_filter: Optional[str]) -> List[DWCSet]:
    grouped: Dict[Tuple[str, str, str], Dict[int, bytes]] = {}
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.lower().endswith('.gif'):
                continue
            base = Path(info.filename).name
            m = FILE_RE.match(base)
            if not m:
                continue
            stage = _stage_from_path(info.filename)
            if stage_filter and stage.lower() != stage_filter.lower():
                continue
            key = (m.group('id'), m.group('slug'), stage)
            state = int(m.group('state'))
            if 0 <= state <= 39:
                grouped.setdefault(key, {})[state] = zf.read(info)
    return [DWCSet(i, slug, stage, states, path.name) for (i, slug, stage), states in grouped.items()]


def _collect_single_gif(path: Path) -> List[DWCSet]:
    m = FILE_RE.match(path.name)
    if not m:
        raise DWCImportError(
            "That GIF does not look like a Digimon World Championship 40-state file.\n\n"
            "Expected something like: m201_agumon_main_0.gif"
        )
    prefix = m.group('prefix')
    states: Dict[int, bytes] = {}
    for p in path.parent.iterdir():
        if not p.is_file():
            continue
        mm = FILE_RE.match(p.name)
        if mm and mm.group('prefix').lower() == prefix.lower():
            state = int(mm.group('state'))
            if 0 <= state <= 39:
                states[state] = p.read_bytes()
    return [DWCSet(m.group('id'), m.group('slug'), _stage_from_path(path.parent.name), states, str(path.parent))]


def _collect_folder(path: Path, stage_filter: Optional[str]) -> List[DWCSet]:
    grouped: Dict[Tuple[str, str, str], Dict[int, bytes]] = {}
    for p in path.rglob('*.gif'):
        m = FILE_RE.match(p.name)
        if not m:
            continue
        try:
            rel = str(p.relative_to(path)).replace('\\', '/')
        except Exception:
            rel = str(p)
        stage = _stage_from_path(rel)
        if stage_filter and stage.lower() != stage_filter.lower():
            continue
        key = (m.group('id'), m.group('slug'), stage)
        state = int(m.group('state'))
        if 0 <= state <= 39:
            grouped.setdefault(key, {})[state] = p.read_bytes()
    return [DWCSet(i, slug, stage, states, str(path)) for (i, slug, stage), states in grouped.items()]


def scan_dwc_source(source: Path, stage_filter: Optional[str] = None) -> List[DWCSet]:
    source = source.resolve()
    if source.is_dir():
        sets = _collect_folder(source, stage_filter)
    elif source.suffix.lower() == '.zip':
        sets = _collect_zip(source, stage_filter)
    elif source.suffix.lower() == '.gif':
        sets = _collect_single_gif(source)
    else:
        raise DWCImportError("Choose a DWC GIF, a folder of DWC GIFs, or the full sprite ZIP.")
    sets.sort(key=lambda s: (s.stage.lower(), int(s.digimon_id), s.slug.lower()))
    return sets


def _default_attacks() -> List[dict]:
    # Generic behavior only. Users can rename/retype moves in Fighter Editor.
    return [
        {"name": "Attack 1", "type": "melee", "animation": "attack_1", "damage": 11, "range": 58,
         "cooldown": 0.82, "knockback": 10, "effect_color": "#ffd250", "weight": 1.0},
        {"name": "Attack 2", "type": "projectile", "animation": "attack_2", "damage": 14, "range": 320,
         "cooldown": 1.25, "knockback": 12, "projectile_speed": 300, "hit_radius": 20,
         "min_range": 65, "effect_color": "#6ecbff", "weight": 0.85},
        {"name": "Attack 3", "type": "beam", "animation": "attack_3", "damage": 15, "range": 330,
         "cooldown": 1.45, "knockback": 10, "min_range": 75, "effect_color": "#f06cff", "weight": 0.65},
        {"name": "Attack 4", "type": "melee", "animation": "attack_4", "damage": 16, "range": 68,
         "cooldown": 1.15, "knockback": 16, "effect_color": "#ff9b4d", "weight": 0.75},
    ]


def import_dwc_set(dset: DWCSet, fighters_dir: Path, overwrite: bool = False) -> Path:
    if not dset.states:
        raise DWCImportError(f"No states were found for {dset.slug}.")
    # State 0 is the normal idle pose and is the minimum useful sprite.
    if 0 not in dset.states:
        raise DWCImportError(f"{dset.slug} is missing state 0 (Stand-Eyes Opened).")

    display = dset.display_name
    target = fighters_dir / _safe_name(display)
    if target.exists() and not overwrite:
        # If this is already one of our DWC packs, updating it is safe and useful.
        old = {}
        try:
            old = json.loads((target / 'fighter.json').read_text(encoding='utf-8'))
        except Exception:
            pass
        if old.get('source', {}).get('format') != 'Digimon World Championship GIF Set':
            base = target
            n = 2
            while target.exists():
                target = fighters_dir / f"{base.name} DWC {n}"
                n += 1
    target.mkdir(parents=True, exist_ok=True)

    # Preserve every 0-39 source state, not just the slots the engine currently uses.
    for state, blob in sorted(dset.states.items()):
        out = target / f"{state}.gif"
        out.write_bytes(blob)
        # Validate early so a corrupt archive entry does not create a broken fighter.
        try:
            with Image.open(io.BytesIO(blob)) as im:
                im.seek(0)
                im.convert('RGBA')
        except Exception as e:
            out.unlink(missing_ok=True)
            raise DWCImportError(f"State {state} for {display} is not a readable GIF: {e}")

    animations = {
        slot: f"{state}.gif"
        for slot, state in RUMBLE_SLOT_MAP.items()
        if state in dset.states
    }
    # Keep any custom attacks/AI from a previous fighter definition when refreshing bundled rookies.
    old = {}
    try:
        old = json.loads((target / 'fighter.json').read_text(encoding='utf-8'))
    except Exception:
        pass

    meta = {
        "name": display,
        "franchise": "Digimon",
        "stage": dset.stage,
        "native_facing": old.get("native_facing", "left"),
        "allow_horizontal_flip": old.get("allow_horizontal_flip", True),
        "scale": old.get("scale", 4.0),
        "stats": old.get("stats", {"hp": 100, "speed": 1.0, "attack": 1.0, "defense": 1.0}),
        "animations": animations,
        "attacks": old.get("attacks") or _default_attacks(),
        "ai": old.get("ai") or {"profile": "auto", "retaliation": 0.55, "evasion": 0.28},
        "transformations": old.get("transformations", []),
        "tags": ["Digimon", dset.stage, "Digimon World Championship"],
        "source": {
            "format": "Digimon World Championship GIF Set",
            "stage": dset.stage,
            "digimon_id": dset.digimon_id,
            "slug": dset.slug,
            "state_count": len(dset.states),
            "source_label": dset.source_label,
            "states": {str(i): STATE_LABELS[i] for i in sorted(dset.states)},
        },
        "dwc_states": {str(i): f"{i}.gif" for i in sorted(dset.states)},
    }
    (target / 'fighter.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')

    lines = [
        "RUMBLE STUDIO - DIGIMON WORLD CHAMPIONSHIP IMPORT",
        "=================================================",
        f"Fighter: {display}",
        f"Stage: {dset.stage}",
        f"States found: {len(dset.states)}/40",
        "",
        "Rumble slot mapping:",
    ]
    for slot, state in RUMBLE_SLOT_MAP.items():
        status = 'OK' if state in dset.states else 'MISSING'
        lines.append(f"  {slot:14s} <- {state:2d} {STATE_LABELS[state]} [{status}]")
    lines += ["", "All source states:"]
    for i in range(40):
        lines.append(f"  {i:2d} {STATE_LABELS[i]}" + ("" if i in dset.states else " [MISSING]"))
    (target / 'DWC_IMPORT_REPORT.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return target


def import_dwc_source(source: Path, fighters_dir: Path, stage_filter: Optional[str] = None,
                      overwrite: bool = False) -> dict:
    sets = scan_dwc_source(source, stage_filter=stage_filter)
    if not sets:
        hint = f" for stage '{stage_filter}'" if stage_filter else ""
        raise DWCImportError(f"No Digimon World Championship 0-39 GIF sets were found{hint}.")
    imported = []
    skipped = []
    for dset in sets:
        try:
            folder = import_dwc_set(dset, fighters_dir, overwrite=overwrite)
            imported.append((dset, folder))
        except Exception as e:
            skipped.append((dset, str(e)))
    if not imported:
        detail = skipped[0][1] if skipped else 'No complete sets were importable.'
        raise DWCImportError(detail)
    return {
        "imported": imported,
        "skipped": skipped,
        "count": len(imported),
        "first_folder": imported[0][1],
    }
