from __future__ import annotations

import json
import os
import re
import random
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk
from PIL import Image, ImageSequence, ImageTk

from mugen_import import MugenImportError, import_mugen_character
from dwc_import import DWCImportError, import_dwc_source

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets" / "fighters"
ROSTER_FILE = ROOT / "roster.json"
RENDERS = ROOT / "renders"

ANIMATION_SLOTS = [
    ("idle", "Idle"),
    ("walk", "Walk"),
    ("run", "Run"),
    ("hit", "Hit / Hurt"),
    ("guard", "Guard"),
    ("ko", "KO / Defeated"),
    ("attack_1", "Attack 1"),
    ("attack_2", "Attack 2"),
    ("attack_3", "Attack 3"),
    ("attack_4", "Attack 4"),
    ("jump", "Jump"),
    ("happy", "Happy"),
    ("cheer", "Cheer"),
    ("victory_jump", "Victory Jump"),
    ("victory", "Dedicated Victory"),
]

SUPPORTED_IMAGES = {".png", ".gif", ".webp", ".jpg", ".jpeg", ".bmp", ".apng"}
AI_PROFILES = ["auto", "aggressive", "brawler", "ranged", "defensive", "opportunist", "skirmisher", "chaotic"]
ATTACK_TYPES = ["melee", "projectile", "beam"]
ATTACK_ANIMS = ["attack_1", "attack_2", "attack_3", "attack_4", "idle"]


def safe_pack_name(name: str) -> str:
    """Turn a display name into a Windows-safe fighter folder name."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "", name).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = "Custom Fighter"
    # Avoid awkward Windows reserved device names.
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
    if cleaned.upper() in reserved:
        cleaned = f"{cleaned} Fighter"
    return cleaned[:80]


def read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def list_fighter_packs() -> List[str]:
    ASSETS.mkdir(parents=True, exist_ok=True)
    out = []
    for p in ASSETS.iterdir():
        if p.is_dir() and (p / "fighter.json").exists():
            out.append(p.name)
    return sorted(out, key=str.lower)


def fighter_summary(pack: str) -> dict:
    """Small normalized metadata record used by search/filter/random roster tools."""
    meta = read_json(ASSETS / pack / "fighter.json", {})
    source = meta.get("source", {}) if isinstance(meta.get("source", {}), dict) else {}
    stage = str(meta.get("stage", "") or source.get("stage", "") or "").strip()
    return {
        "pack": pack,
        "name": str(meta.get("name", pack)),
        "franchise": str(meta.get("franchise", "Custom") or "Custom"),
        "stage": stage,
        "meta": meta,
    }


def all_fighter_summaries() -> List[dict]:
    return [fighter_summary(p) for p in list_fighter_packs()]


def matches_filters(info: dict, search: str = "", franchise: str = "", stage: str = "") -> bool:
    q = (search or "").strip().lower()
    if q:
        haystack = " ".join([info.get("name", ""), info.get("franchise", ""), info.get("stage", ""), info.get("pack", "")]).lower()
        if q not in haystack:
            return False
    if franchise and franchise not in ("All franchises", "MULTIVERSE (spread franchises)"):
        if info.get("franchise", "").lower() != franchise.lower():
            return False
    if stage and stage not in ("All stages", "Any stage"):
        if info.get("stage", "").lower() != stage.lower():
            return False
    return True


def load_roster() -> dict:
    data = read_json(ROSTER_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("studio", {"title": "RUMBLE STUDIO", "subtitle": "v0.13 • Mapping Locks"})
    data.setdefault("fighters", [])
    return data


def write_roster(pack_names: List[str]) -> None:
    if len(pack_names) < 2:
        raise ValueError("A rumble needs at least two fighters.")
    data = load_roster()
    data["studio"] = {
        "title": data.get("studio", {}).get("title", "RUMBLE STUDIO"),
        "subtitle": "v0.13 • Mapping Locks",
    }
    data["fighters"] = [{"pack": p} for p in pack_names]
    ROSTER_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def image_is_usable(path: Path) -> bool:
    if not path.exists() or not path.is_file() or path.suffix.lower() not in SUPPORTED_IMAGES:
        return False
    try:
        with Image.open(path) as im:
            im.seek(0)
            im.convert("RGBA")
        return True
    except Exception:
        return False


def unique_asset_name(slot: str, src: Path, used: set[str]) -> str:
    base = f"{slot}{src.suffix.lower()}"
    if base not in used:
        used.add(base)
        return base
    n = 2
    while f"{slot}_{n}{src.suffix.lower()}" in used:
        n += 1
    out = f"{slot}_{n}{src.suffix.lower()}"
    used.add(out)
    return out


def save_fighter_pack(
    display_name: str,
    franchise: str,
    stage_form: str,
    native_facing: str,
    allow_flip: bool,
    scale: float,
    stats: dict,
    ai: dict,
    animation_sources: Dict[str, str],
    attacks: List[dict],
    transformations: Optional[List[dict]] = None,
    mugen_locks: Optional[dict] = None,
    existing_folder: Optional[Path] = None,
) -> Path:
    """Create/update a fighter pack. Used by the GUI and by smoke tests."""
    display_name = display_name.strip()
    if not display_name:
        raise ValueError("Fighter name is required.")
    if native_facing not in ("left", "right"):
        raise ValueError("Native facing must be left or right.")
    if scale <= 0:
        raise ValueError("Scale must be greater than zero.")

    usable = {k: Path(v) for k, v in animation_sources.items() if v and image_is_usable(Path(v))}
    if not usable:
        raise ValueError("Choose at least one usable sprite image or GIF.")

    if not attacks:
        attacks = [{
            "name": "Basic Strike",
            "type": "melee",
            "animation": "attack_1",
            "damage": 12,
            "range": 54,
            "cooldown": 1.0,
            "knockback": 10,
            "effect_color": "#ffd250",
            "weight": 1.0,
        }]

    target = existing_folder if existing_folder is not None else ASSETS / safe_pack_name(display_name)
    target.mkdir(parents=True, exist_ok=True)

    # Preserve only files still referenced by the definition plus fighter.json. We do not delete
    # unknown files automatically because users may keep notes/source art inside a fighter folder.
    animations = {}
    used_names: set[str] = set()
    for slot, src in usable.items():
        try:
            if src.resolve().parent == target.resolve().parent and src.resolve().parent == target.resolve():
                dest_name = src.name
            else:
                dest_name = unique_asset_name(slot, src, used_names)
                dest = target / dest_name
                if src.resolve() != dest.resolve():
                    shutil.copy2(src, dest)
        except FileNotFoundError:
            dest_name = unique_asset_name(slot, src, used_names)
            shutil.copy2(src, target / dest_name)
        animations[slot] = dest_name

    old_meta = read_json(target / "fighter.json", {}) if (target / "fighter.json").exists() else {}
    meta = {
        "name": display_name,
        "franchise": franchise.strip() or "Custom",
        "stage": stage_form.strip(),
        "native_facing": native_facing,
        "allow_horizontal_flip": bool(allow_flip),
        "scale": round(float(scale), 3),
        "stats": {
            "hp": round(float(stats.get("hp", 100)), 2),
            "speed": round(float(stats.get("speed", 1.0)), 3),
            "attack": round(float(stats.get("attack", 1.0)), 3),
            "defense": round(float(stats.get("defense", 1.0)), 3),
        },
        "animations": animations,
        "attacks": attacks,
        "ai": ai,
    }
    for extra_key in ("source", "tags", "dwc_states"):
        if extra_key in old_meta:
            meta[extra_key] = old_meta[extra_key]
    if mugen_locks is None:
        if "mugen_locks" in old_meta:
            meta["mugen_locks"] = old_meta["mugen_locks"]
    else:
        meta["mugen_locks"] = {
            "animations": sorted({str(x) for x in mugen_locks.get("animations", []) if str(x)}),
            "attacks": sorted({str(x) for x in mugen_locks.get("attacks", []) if str(x)}),
        }
    if transformations is None:
        if "transformations" in old_meta:
            meta["transformations"] = old_meta["transformations"]
    else:
        meta["transformations"] = [dict(x) for x in transformations if isinstance(x, dict)]
    (target / "fighter.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return target


def discover_mugen_sources(folder: Path) -> List[Path]:
    """Find importable MUGEN archives and one representative DEF per character folder."""
    folder = Path(folder)
    archives = sorted(
        [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in {".zip", ".rar", ".7z"}],
        key=lambda p: str(p).lower(),
    )
    by_dir: Dict[Path, List[Path]] = {}
    for d in folder.rglob("*.def"):
        if d.is_file():
            by_dir.setdefault(d.parent, []).append(d)
    defs: List[Path] = []
    for parent, candidates in sorted(by_dir.items(), key=lambda kv: str(kv[0]).lower()):
        candidates = sorted(candidates, key=lambda x: x.name.lower())
        preferred = next((d for d in candidates if d.stem.lower() == parent.name.lower()), candidates[0])
        defs.append(preferred)
    # Folder scans often contain either packed characters OR a normal MUGEN chars folder.
    # If archives exist, prefer them so an extracted copy sitting beside its archive isn't imported twice.
    return archives if archives else defs


@dataclass
class PreviewAnimation:
    frames: List[Image.Image]
    durations: List[int]
    index: int = 0


class AttackDialog(tk.Toplevel):
    def __init__(self, parent, attack: Optional[dict] = None):
        super().__init__(parent)
        self.title("Attack Editor")
        self.resizable(False, False)
        self.result = None
        self.transient(parent)
        self.grab_set()
        attack = dict(attack or {})

        self.vars = {
            "name": tk.StringVar(value=attack.get("name", "New Attack")),
            "type": tk.StringVar(value=attack.get("type", "melee")),
            "animation": tk.StringVar(value=attack.get("animation", "attack_1")),
            "damage": tk.StringVar(value=str(attack.get("damage", 12))),
            "range": tk.StringVar(value=str(attack.get("range", 54))),
            "cooldown": tk.StringVar(value=str(attack.get("cooldown", 1.0))),
            "knockback": tk.StringVar(value=str(attack.get("knockback", 10))),
            "projectile_speed": tk.StringVar(value=str(attack.get("projectile_speed", 280))),
            "hit_radius": tk.StringVar(value=str(attack.get("hit_radius", 20))),
            "min_range": tk.StringVar(value=str(attack.get("min_range", 0))),
            "weight": tk.StringVar(value=str(attack.get("weight", 1.0))),
            "effect_color": tk.StringVar(value=attack.get("effect_color", "#ffd250")),
        }

        body = ttk.Frame(self, padding=14)
        body.grid(sticky="nsew")
        labels = [
            ("Attack name", "name"), ("Type", "type"), ("Animation", "animation"),
            ("Damage", "damage"), ("Range", "range"), ("Cooldown (sec)", "cooldown"),
            ("Knockback", "knockback"), ("Projectile speed", "projectile_speed"),
            ("Hit radius", "hit_radius"), ("Minimum range", "min_range"),
            ("Selection weight", "weight"), ("Effect color", "effect_color"),
        ]
        for r, (label, key) in enumerate(labels):
            ttk.Label(body, text=label).grid(row=r, column=0, sticky="w", padx=(0, 10), pady=4)
            if key == "type":
                w = ttk.Combobox(body, textvariable=self.vars[key], values=ATTACK_TYPES, state="readonly", width=20)
            elif key == "animation":
                w = ttk.Combobox(body, textvariable=self.vars[key], values=ATTACK_ANIMS, state="readonly", width=20)
            else:
                w = ttk.Entry(body, textvariable=self.vars[key], width=23)
            w.grid(row=r, column=1, sticky="ew", pady=4)
            if key == "effect_color":
                ttk.Button(body, text="Pick", command=self.pick_color, width=7).grid(row=r, column=2, padx=(6, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=len(labels), column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="Save Attack", command=self.save).pack(side="right")
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Return>", lambda e: self.save())
        self.wait_visibility()
        self.focus_force()

    def pick_color(self):
        chosen = colorchooser.askcolor(color=self.vars["effect_color"].get(), parent=self)
        if chosen and chosen[1]:
            self.vars["effect_color"].set(chosen[1])

    def save(self):
        try:
            values = {k: v.get().strip() for k, v in self.vars.items()}
            if not values["name"]:
                raise ValueError("Attack name is required.")
            for k in ("damage", "range", "cooldown", "knockback", "projectile_speed", "hit_radius", "min_range", "weight"):
                values[k] = float(values[k])
            if values["cooldown"] <= 0 or values["range"] < 0 or values["damage"] < 0:
                raise ValueError("Damage/range must be non-negative and cooldown must be greater than zero.")
            self.result = values
            self.destroy()
        except ValueError as e:
            messagebox.showerror("Attack settings", str(e), parent=self)


class TransformationDialog(tk.Toplevel):
    def __init__(self, parent, rule: Optional[dict] = None, current_pack: Optional[str] = None):
        super().__init__(parent)
        self.title("Transformation Rule")
        self.resizable(False, False)
        self.result = None
        self.transient(parent)
        self.grab_set()
        rule = dict(rule or {})

        packs = [p for p in list_fighter_packs() if p != current_pack]
        self.target_var = tk.StringVar(value=str(rule.get("target_pack", packs[0] if packs else "")))
        self.chance_var = tk.StringVar(value=str(rule.get("chance", 35)))
        self.heal_var = tk.StringVar(value=str(rule.get("heal_percent", 0)))
        self.label_var = tk.StringVar(value=str(rule.get("label", "TRANSFORM!")))

        body = ttk.Frame(self, padding=14)
        body.grid(sticky="nsew")
        ttk.Label(body, text="Transform into").grid(row=0, column=0, sticky="w", pady=4)
        self.target_combo = ttk.Combobox(body, textvariable=self.target_var, values=packs, state="readonly", width=34)
        self.target_combo.grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(body, text="Chance after each KO (%)").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.chance_var, width=12).grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(body, text="Heal on transform (% max HP)").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.heal_var, width=12).grid(row=2, column=1, sticky="w", pady=4)
        ttk.Label(body, text="Announcement").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.label_var, width=28).grid(row=3, column=1, sticky="ew", pady=4)
        ttk.Label(
            body,
            text="HP percentage carries into the new form. Heal % is added after that.\nMultiple rules create branching forms.",
            foreground="#666",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(7,8))
        btns = ttk.Frame(body)
        btns.grid(row=5, column=0, columnspan=2, sticky="e")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Save Rule", command=self._save).pack(side="right", padx=(0,6))

    def _save(self):
        try:
            target = self.target_var.get().strip()
            if not target:
                raise ValueError("Choose a target fighter/form first.")
            if not (ASSETS / target / "fighter.json").exists():
                raise ValueError("That target fighter is no longer in the library.")
            chance = float(self.chance_var.get())
            heal = float(self.heal_var.get())
            if chance < 0 or chance > 100:
                raise ValueError("Chance must be between 0 and 100.")
            if heal < 0 or heal > 100:
                raise ValueError("Heal must be between 0 and 100.")
            self.result = {
                "target_pack": target,
                "chance": round(chance, 2),
                "heal_percent": round(heal, 2),
                "label": (self.label_var.get().strip() or "TRANSFORM!")[:28],
            }
            self.destroy()
        except Exception as e:
            messagebox.showerror("Transformation rule", str(e), parent=self)


class StudioApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Rumble Studio v0.13 — Mapping Locks")
        self.root.geometry("1220x830")
        self.root.minsize(1050, 720)

        self.current_folder: Optional[Path] = None
        self.anim_paths: Dict[str, tk.StringVar] = {key: tk.StringVar() for key, _ in ANIMATION_SLOTS}
        self.anim_lock_vars: Dict[str, tk.BooleanVar] = {key: tk.BooleanVar(value=False) for key, _ in ANIMATION_SLOTS}
        self.attack_lock_slots: set[str] = set()
        self.preview_anim: Optional[PreviewAnimation] = None
        self.preview_job = None
        self.attack_data: List[dict] = []
        self.transformation_data: List[dict] = []

        # v0.13 Transformation Lab: edit an entire connected evolution/form graph
        # without bouncing back through Fighter Editor for every source form.
        self.transform_lab_rules: Dict[str, List[dict]] = {}
        self.transform_lab_dirty: set[str] = set()
        self.transform_lab_root_var = tk.StringVar()
        self.transform_lab_source_var = tk.StringVar()
        self.transform_lab_summary_var = tk.StringVar(value="Choose a source form to edit its branches.")
        self.transform_lab_dirty_var = tk.StringVar(value="No unsaved line changes.")
        self.transform_line_pack_by_iid: Dict[str, str] = {}
        self.chain_vars = [tk.StringVar() for _ in range(5)]
        self.chain_chance_var = tk.StringVar(value="100")
        self.chain_heal_var = tk.StringVar(value="0")
        self.chain_label_var = tk.StringVar(value="TRANSFORM!")
        self.chain_replace_var = tk.BooleanVar(value=True)

        self.roster_vars: Dict[str, tk.BooleanVar] = {}
        self.roster_initialized = False

        self.name_var = tk.StringVar()
        self.franchise_var = tk.StringVar(value="Custom")
        self.stage_var = tk.StringVar(value="")
        self.facing_var = tk.StringVar(value="right")

        # Library + roster search/filter controls. These are intentionally shared
        # concepts so large crossover libraries stay manageable.
        self.library_search_var = tk.StringVar()
        self.library_franchise_filter_var = tk.StringVar(value="All franchises")
        self.library_stage_filter_var = tk.StringVar(value="All stages")
        self.roster_search_var = tk.StringVar()
        self.roster_franchise_filter_var = tk.StringVar(value="All franchises")
        self.roster_stage_filter_var = tk.StringVar(value="All stages")
        self.random_count_var = tk.StringVar(value="8")
        self.random_franchise_var = tk.StringVar(value="MULTIVERSE (spread franchises)")
        self.random_stage_var = tk.StringVar(value="Any stage")
        self.flip_var = tk.BooleanVar(value=True)
        self.scale_var = tk.StringVar(value="4.0")
        self.hp_var = tk.StringVar(value="100")
        self.speed_var = tk.StringVar(value="1.0")
        self.attack_mult_var = tk.StringVar(value="1.0")
        self.defense_var = tk.StringVar(value="1.0")
        self.ai_profile_var = tk.StringVar(value="auto")
        self.retaliation_var = tk.StringVar(value="0.55")
        self.evasion_var = tk.StringVar(value="0.25")
        self.add_roster_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Ready. Create a fighter or choose one from the library.")

        self._build_ui()
        self.refresh_library()
        self.refresh_roster()

        autoclose = os.environ.get("RUMBLE_STUDIO_AUTOCLOSE_MS")
        if autoclose:
            try:
                self.root.after(int(autoclose), self.root.destroy)
            except Exception:
                pass

    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("vista" if sys.platform.startswith("win") else "clam")
        except Exception:
            pass

        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)

        # Library sidebar
        side = ttk.LabelFrame(outer, text="Fighter Library", padding=8)
        side.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        side.rowconfigure(5, weight=1)

        ttk.Label(side, text="Search saved fighters").grid(row=0, column=0, columnspan=2, sticky="w")
        lib_search = ttk.Entry(side, textvariable=self.library_search_var, width=25)
        lib_search.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 5))
        lib_search.bind("<KeyRelease>", lambda e: self.refresh_library())

        self.library_franchise_combo = ttk.Combobox(
            side, textvariable=self.library_franchise_filter_var, state="readonly", width=22
        )
        self.library_franchise_combo.grid(row=2, column=0, columnspan=2, sticky="ew", pady=2)
        self.library_franchise_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_library())
        self.library_stage_combo = ttk.Combobox(
            side, textvariable=self.library_stage_filter_var, state="readonly", width=22
        )
        self.library_stage_combo.grid(row=3, column=0, columnspan=2, sticky="ew", pady=2)
        self.library_stage_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_library())

        ttk.Label(side, text="Saved fighters").grid(row=4, column=0, sticky="w", pady=(5, 0))
        self.library = tk.Listbox(side, width=27, exportselection=False)
        self.library.grid(row=5, column=0, sticky="nsew", pady=6)
        self.library.bind("<<ListboxSelect>>", self.on_library_select)
        sb = ttk.Scrollbar(side, orient="vertical", command=self.library.yview)
        sb.grid(row=5, column=1, sticky="ns", pady=6)
        self.library.configure(yscrollcommand=sb.set)
        ttk.Button(side, text="+ New Fighter", command=self.new_fighter).grid(row=6, column=0, columnspan=2, sticky="ew", pady=(2, 4))
        mugen_btn = ttk.Button(side, text="⚡ IMPORT MUGEN CHARACTER", command=self.import_mugen_gui)
        mugen_btn.grid(row=7, column=0, columnspan=2, sticky="ew", pady=3)
        ttk.Button(side, text="⚡ BATCH MUGEN FILES", command=self.import_mugen_batch_files_gui).grid(row=8, column=0, columnspan=2, sticky="ew", pady=3)
        ttk.Button(side, text="📁 SCAN MUGEN FOLDER", command=self.import_mugen_batch_folder_gui).grid(row=9, column=0, columnspan=2, sticky="ew", pady=3)
        dwc_btn = ttk.Button(side, text="🦖 IMPORT DWC SPRITE SET", command=self.import_dwc_gui)
        dwc_btn.grid(row=10, column=0, columnspan=2, sticky="ew", pady=3)
        ttk.Button(side, text="Duplicate Fighter", command=self.duplicate_fighter).grid(row=11, column=0, columnspan=2, sticky="ew", pady=3)
        ttk.Button(side, text="Open Fighter Folder", command=self.open_fighter_folder).grid(row=12, column=0, columnspan=2, sticky="ew", pady=3)

        # Main tabs
        self.tabs = ttk.Notebook(outer)
        self.tabs.grid(row=0, column=1, sticky="nsew")
        self.editor_tab = ttk.Frame(self.tabs, padding=10)
        self.transform_tab = ttk.Frame(self.tabs, padding=12)
        self.roster_tab = ttk.Frame(self.tabs, padding=10)
        self.help_tab = ttk.Frame(self.tabs, padding=14)
        self.tabs.add(self.editor_tab, text="Fighter Editor")
        self.tabs.add(self.transform_tab, text="Transformation Lab")
        self.tabs.add(self.roster_tab, text="Battle Roster")
        self.tabs.add(self.help_tab, text="Quick Guide")
        self._build_editor()
        self._build_transform_tab()
        self._build_roster_tab()
        self._build_help_tab()

        status = ttk.Label(self.root, textvariable=self.status_var, anchor="w", relief="sunken", padding=(8, 4))
        status.pack(fill="x", side="bottom")

    def _build_editor(self):
        self.editor_tab.columnconfigure(0, weight=1)
        top = ttk.LabelFrame(self.editor_tab, text="Identity & Stats", padding=10)
        top.grid(row=0, column=0, sticky="ew")
        for c in (1, 3, 5, 7):
            top.columnconfigure(c, weight=1)

        fields = [
            ("Name", self.name_var), ("Franchise / Pack", self.franchise_var),
            ("Scale", self.scale_var), ("HP", self.hp_var),
            ("Speed", self.speed_var), ("Attack multiplier", self.attack_mult_var),
            ("Defense multiplier", self.defense_var),
        ]
        positions = [(0,0),(0,2),(0,4),(1,0),(1,2),(1,4),(1,6)]
        for (label, var), (r,c) in zip(fields, positions):
            ttk.Label(top, text=label).grid(row=r, column=c, sticky="w", padx=(0,5), pady=3)
            ttk.Entry(top, textvariable=var, width=15).grid(row=r, column=c+1, sticky="ew", padx=(0,12), pady=3)

        ttk.Label(top, text="Native facing").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Combobox(top, textvariable=self.facing_var, values=["left", "right"], state="readonly", width=10).grid(row=2, column=1, sticky="w")
        ttk.Checkbutton(top, text="Allow horizontal flip", variable=self.flip_var).grid(row=2, column=2, columnspan=2, sticky="w")
        ttk.Label(top, text="Stage / Form").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Entry(top, textvariable=self.stage_var, width=15).grid(row=3, column=1, sticky="ew", padx=(0,12), pady=3)
        ttk.Label(top, text="Examples: Rookie, Mega, Base, Super Saiyan", foreground="#666").grid(row=3, column=2, columnspan=4, sticky="w")
        ttk.Label(top, text="AI profile").grid(row=2, column=4, sticky="w")
        ttk.Combobox(top, textvariable=self.ai_profile_var, values=AI_PROFILES, state="readonly", width=13).grid(row=2, column=5, sticky="w")
        ttk.Label(top, text="Retaliation").grid(row=2, column=6, sticky="w")
        ttk.Entry(top, textvariable=self.retaliation_var, width=8).grid(row=2, column=7, sticky="w")
        ttk.Label(top, text="Evasion").grid(row=3, column=6, sticky="w")
        ttk.Entry(top, textvariable=self.evasion_var, width=8).grid(row=3, column=7, sticky="w")

        mid = ttk.Panedwindow(self.editor_tab, orient="horizontal")
        mid.grid(row=1, column=0, sticky="nsew", pady=10)
        self.editor_tab.rowconfigure(1, weight=1)

        # Animation mappings
        anim_box = ttk.LabelFrame(mid, text="Animation Mapping", padding=8)
        mid.add(anim_box, weight=3)
        anim_box.columnconfigure(1, weight=1)
        ttk.Label(anim_box, text="Lock any mapping you like. MUGEN rescan only replaces UNLOCKED slots.", foreground="#555").grid(row=0, column=0, columnspan=5, sticky="w", pady=(0,6))
        for r, (slot, label) in enumerate(ANIMATION_SLOTS, start=1):
            ttk.Label(anim_box, text=label, width=18).grid(row=r, column=0, sticky="w", pady=2)
            e = ttk.Entry(anim_box, textvariable=self.anim_paths[slot], state="readonly", width=31)
            e.grid(row=r, column=1, sticky="ew", padx=4)
            ttk.Button(anim_box, text="Browse", width=8, command=lambda s=slot: self.browse_animation(s)).grid(row=r, column=2, padx=2)
            ttk.Button(anim_box, text="Preview", width=8, command=lambda s=slot: self.preview_animation(s)).grid(row=r, column=3, padx=2)
            ttk.Checkbutton(
                anim_box, text="Lock", variable=self.anim_lock_vars[slot],
                command=lambda s=slot: self._animation_lock_changed(s)
            ).grid(row=r, column=4, padx=(5,0), sticky="w")
        lock_row = len(ANIMATION_SLOTS) + 1
        lock_btns = ttk.Frame(anim_box)
        lock_btns.grid(row=lock_row, column=0, columnspan=5, sticky="ew", pady=(7,0))
        ttk.Button(lock_btns, text="Lock all mapped", command=self.lock_all_mapped).pack(side="left")
        ttk.Button(lock_btns, text="Unlock all", command=self.unlock_all_mappings).pack(side="left", padx=5)

        # Preview and attacks
        right = ttk.Frame(mid)
        mid.add(right, weight=2)
        preview_box = ttk.LabelFrame(right, text="Sprite Preview", padding=8)
        preview_box.pack(fill="x")
        self.preview_label = ttk.Label(preview_box, text="Choose an animation\nto preview", anchor="center", width=28)
        self.preview_label.pack(fill="both", expand=True, ipady=55)

        attack_box = ttk.LabelFrame(right, text="Attacks", padding=8)
        attack_box.pack(fill="both", expand=True, pady=(10,0))
        cols = ("locked","name","type","anim","damage","range","cooldown")
        self.attack_tree = ttk.Treeview(attack_box, columns=cols, show="headings", height=8)
        headings = {"locked":"Lock","name":"Name","type":"Type","anim":"Animation","damage":"DMG","range":"Range","cooldown":"CD"}
        widths = {"locked":45,"name":105,"type":62,"anim":70,"damage":42,"range":48,"cooldown":42}
        for c in cols:
            self.attack_tree.heading(c, text=headings[c])
            self.attack_tree.column(c, width=widths[c], anchor="center" if c != "name" else "w")
        self.attack_tree.pack(fill="both", expand=True)
        self.attack_tree.bind("<Double-1>", lambda e: self.edit_attack())
        btns = ttk.Frame(attack_box)
        btns.pack(fill="x", pady=(6,0))
        ttk.Button(btns, text="+ Add", command=self.add_attack).pack(side="left")
        ttk.Button(btns, text="Edit", command=self.edit_attack).pack(side="left", padx=4)
        ttk.Button(btns, text="Remove", command=self.remove_attack).pack(side="left")
        ttk.Button(btns, text="Lock / Unlock", command=self.toggle_selected_attack_lock).pack(side="left", padx=(8,0))

        bottom = ttk.Frame(self.editor_tab)
        bottom.grid(row=2, column=0, sticky="ew")
        ttk.Checkbutton(bottom, text="Add/update this fighter in the current battle roster", variable=self.add_roster_var).pack(side="left")
        ttk.Button(bottom, text="DELETE FIGHTER", command=self.delete_current_fighter).pack(side="right", padx=(6,0), ipadx=6, ipady=4)
        ttk.Button(bottom, text="RESET TO SAVED", command=self.reset_current_fighter).pack(side="right", padx=(6,0), ipadx=6, ipady=4)
        ttk.Button(bottom, text="↻ RESCAN UNLOCKED MUGEN", command=self.rescan_mugen_current).pack(side="right", padx=(6,0), ipadx=6, ipady=4)
        ttk.Button(bottom, text="SAVE FIGHTER", command=self.save_fighter).pack(side="right", ipadx=16, ipady=4)

    def _build_transform_tab(self):
        """Whole-line transformation editor.

        A transformation rule still belongs to its source fighter JSON, but the Lab stages
        rules for many source fighters at once. This makes Pokemon-style linear chains and
        Digimon/DBZ branching trees editable without leaving this page.
        """
        self.transform_tab.columnconfigure(0, weight=1)

        ttk.Label(
            self.transform_tab,
            text="Transformation Lab — Build the Entire Line Here",
            font=("Arial", 12, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            self.transform_tab,
            text=("Pick a root, then click any form in the Line Map to edit what THAT form can become. "
                  "You can build Agumon → Greymon → MetalGreymon → WarGreymon, a three-stage Pokemon line, "
                  "or a branching DBZ form tree without changing tabs."),
            foreground="#555", wraplength=920, justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(3,8))

        top = ttk.LabelFrame(self.transform_tab, text="Line Controls", padding=8)
        top.grid(row=2, column=0, sticky="ew", pady=(0,8))
        top.columnconfigure(1, weight=1)
        top.columnconfigure(3, weight=1)
        ttk.Label(top, text="Root / starting form").grid(row=0, column=0, sticky="w")
        self.transform_root_combo = ttk.Combobox(top, textvariable=self.transform_lab_root_var, state="readonly", width=20)
        self.transform_root_combo.grid(row=0, column=1, sticky="ew", padx=(5,12))
        self.transform_root_combo.bind("<<ComboboxSelected>>", lambda e: self._transform_root_changed())
        ttk.Label(top, text="Currently editing").grid(row=0, column=2, sticky="w")
        self.transform_source_combo = ttk.Combobox(top, textvariable=self.transform_lab_source_var, state="readonly", width=20)
        self.transform_source_combo.grid(row=0, column=3, sticky="ew", padx=(5,12))
        self.transform_source_combo.bind("<<ComboboxSelected>>", lambda e: self._transform_source_changed())
        ttk.Button(top, text="SAVE ALL LINE CHANGES", command=self.save_transform_lab).grid(row=0, column=4, sticky="e", ipadx=4)
        ttk.Label(top, textvariable=self.transform_lab_dirty_var, foreground="#8a5a00").grid(row=1, column=0, columnspan=5, sticky="w", pady=(5,0))

        work = ttk.Panedwindow(self.transform_tab, orient="horizontal")
        work.grid(row=3, column=0, sticky="nsew")
        self.transform_tab.rowconfigure(3, weight=1)

        # Left: connected line/tree map.
        map_box = ttk.LabelFrame(work, text="Line Map — click a form to edit its outgoing branches", padding=8)
        work.add(map_box, weight=2)
        map_box.columnconfigure(0, weight=1)
        map_box.rowconfigure(0, weight=1)
        mcols = ("stage", "chance", "heal")
        self.transform_line_tree = ttk.Treeview(map_box, columns=mcols, show="tree headings", height=15)
        self.transform_line_tree.heading("#0", text="Fighter / Form")
        self.transform_line_tree.column("#0", width=190, stretch=True)
        self.transform_line_tree.heading("stage", text="Stage")
        self.transform_line_tree.column("stage", width=82, anchor="center")
        self.transform_line_tree.heading("chance", text="Chance")
        self.transform_line_tree.column("chance", width=78, anchor="center")
        self.transform_line_tree.heading("heal", text="Heal")
        self.transform_line_tree.column("heal", width=52, anchor="center")
        self.transform_line_tree.grid(row=0, column=0, sticky="nsew")
        self.transform_line_tree.bind("<<TreeviewSelect>>", self._transform_line_selected)
        msb = ttk.Scrollbar(map_box, orient="vertical", command=self.transform_line_tree.yview)
        msb.grid(row=0, column=1, sticky="ns")
        self.transform_line_tree.configure(yscrollcommand=msb.set)
        map_btns = ttk.Frame(map_box)
        map_btns.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(7,0))
        ttk.Button(map_btns, text="Refresh Map", command=self.refresh_transform_line_tree).pack(side="left")
        ttk.Button(map_btns, text="Reload Saved Rules", command=self.reload_transform_lab).pack(side="left", padx=5)

        # Right: branches for whichever source form is selected.
        branch_box = ttk.LabelFrame(work, text="Branches From Selected Form", padding=8)
        work.add(branch_box, weight=3)
        branch_box.columnconfigure(0, weight=1)
        branch_box.rowconfigure(1, weight=1)
        self.transform_current_label = ttk.Label(branch_box, text="Choose a form in the Line Map.", font=("Arial", 10, "bold"))
        self.transform_current_label.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0,6))
        cols = ("target", "stage", "chance", "heal", "label")
        self.transform_tree = ttk.Treeview(branch_box, columns=cols, show="headings", height=12)
        labels = {"target":"Target Fighter / Form", "stage":"Stage", "chance":"Chance", "heal":"Heal", "label":"Announce"}
        widths = {"target":165, "stage":82, "chance":62, "heal":52, "label":92}
        for c in cols:
            self.transform_tree.heading(c, text=labels[c])
            self.transform_tree.column(c, width=widths[c], anchor="w" if c in ("target","stage","label") else "center")
        self.transform_tree.grid(row=1, column=0, sticky="nsew")
        self.transform_tree.bind("<Double-1>", lambda e: self.edit_transformation())
        tsb = ttk.Scrollbar(branch_box, orient="vertical", command=self.transform_tree.yview)
        tsb.grid(row=1, column=1, sticky="ns")
        self.transform_tree.configure(yscrollcommand=tsb.set)
        btns = ttk.Frame(branch_box)
        btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8,0))
        ttk.Button(btns, text="+ Branch", command=self.add_transformation).pack(side="left")
        ttk.Button(btns, text="Edit", command=self.edit_transformation).pack(side="left", padx=4)
        ttk.Button(btns, text="Remove", command=self.remove_transformation).pack(side="left")
        ttk.Button(btns, text="Follow →", command=self.follow_transform_target).pack(side="left", padx=(12,4))
        ttk.Button(btns, text="Clear", command=self.clear_transformations).pack(side="left")
        ttk.Label(branch_box, textvariable=self.transform_lab_summary_var, foreground="#666", wraplength=520, justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(8,0)
        )

        # Quick linear builder for three-stage Pokemon and straightforward evolution lines.
        quick = ttk.LabelFrame(self.transform_tab, text="Quick Linear Chain Builder", padding=8)
        quick.grid(row=4, column=0, sticky="ew", pady=(8,0))
        ttk.Label(quick, text="Great for Pokemon or a simple Digimon line:").grid(row=0, column=0, columnspan=12, sticky="w", pady=(0,5))
        self.chain_combos = []
        for i, var in enumerate(self.chain_vars):
            combo = ttk.Combobox(quick, textvariable=var, state="readonly", width=13)
            combo.grid(row=1, column=i*2, sticky="ew")
            self.chain_combos.append(combo)
            if i < len(self.chain_vars)-1:
                ttk.Label(quick, text="→").grid(row=1, column=i*2+1, padx=3)
        opts = ttk.Frame(quick)
        opts.grid(row=2, column=0, columnspan=12, sticky="ew", pady=(7,0))
        ttk.Label(opts, text="Chance/link").pack(side="left")
        ttk.Entry(opts, textvariable=self.chain_chance_var, width=6).pack(side="left", padx=(4,10))
        ttk.Label(opts, text="Heal %").pack(side="left")
        ttk.Entry(opts, textvariable=self.chain_heal_var, width=6).pack(side="left", padx=(4,10))
        ttk.Label(opts, text="Announcement").pack(side="left")
        ttk.Entry(opts, textvariable=self.chain_label_var, width=16).pack(side="left", padx=(4,10))
        ttk.Checkbutton(opts, text="Replace existing branches for these source forms", variable=self.chain_replace_var).pack(side="left", padx=(4,10))
        ttk.Button(opts, text="APPLY CHAIN", command=self.apply_quick_transform_chain).pack(side="right", ipadx=8)

        ttk.Label(
            self.transform_tab,
            text=("Branch math: if a form has 35% SSJ1 + 20% SSJ2 + 5% Ultra Instinct, it has a 60% chance to transform after each KO, "
                  "and 40% to stay put. Totals at/above 100% guarantee a transformation; the values then act as branch weights. "
                  "Direct jumps are allowed, so Base Goku can have separate chances for SSJ1, SSJ2, SSJ3, God, UI, etc."),
            foreground="#666", wraplength=930, justify="left",
        ).grid(row=5, column=0, sticky="w", pady=(7,0))

        self.refresh_transform_lab_choices()

    def _build_roster_tab(self):
        self.roster_tab.columnconfigure(0, weight=1)
        self.roster_tab.rowconfigure(3, weight=1)
        ttk.Label(
            self.roster_tab,
            text="Choose any saved fighters for the next rumble. Search, filter, or let Studio pick a random cast.",
            font=("Arial", 11, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0,8))

        filters = ttk.LabelFrame(self.roster_tab, text="Find Fighters", padding=8)
        filters.grid(row=1, column=0, sticky="ew", pady=(0,8))
        filters.columnconfigure(1, weight=1)
        ttk.Label(filters, text="Search").grid(row=0, column=0, sticky="w", padx=(0,5))
        roster_search = ttk.Entry(filters, textvariable=self.roster_search_var)
        roster_search.grid(row=0, column=1, sticky="ew", padx=(0,10))
        roster_search.bind("<KeyRelease>", lambda e: self.refresh_roster())
        self.roster_franchise_combo = ttk.Combobox(
            filters, textvariable=self.roster_franchise_filter_var, state="readonly", width=20
        )
        self.roster_franchise_combo.grid(row=0, column=2, padx=4)
        self.roster_franchise_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_roster())
        self.roster_stage_combo = ttk.Combobox(
            filters, textvariable=self.roster_stage_filter_var, state="readonly", width=16
        )
        self.roster_stage_combo.grid(row=0, column=3, padx=4)
        self.roster_stage_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_roster())
        ttk.Button(filters, text="Reset Filters", command=self.reset_roster_filters).grid(row=0, column=4, padx=(8,0))

        random_box = ttk.LabelFrame(self.roster_tab, text="Random Fighter Selector", padding=8)
        random_box.grid(row=2, column=0, sticky="ew", pady=(0,8))
        ttk.Label(random_box, text="Pick").pack(side="left")
        ttk.Spinbox(random_box, from_=2, to=128, textvariable=self.random_count_var, width=5).pack(side="left", padx=(4,10))
        ttk.Label(random_box, text="from").pack(side="left")
        self.random_franchise_combo = ttk.Combobox(
            random_box, textvariable=self.random_franchise_var, state="readonly", width=25
        )
        self.random_franchise_combo.pack(side="left", padx=6)
        self.random_stage_combo = ttk.Combobox(
            random_box, textvariable=self.random_stage_var, state="readonly", width=16
        )
        self.random_stage_combo.pack(side="left", padx=6)
        ttk.Button(random_box, text="🎲 PICK RANDOM", command=self.pick_random_roster).pack(side="left", padx=(8,4))
        ttk.Label(
            random_box, text="Tip: Digimon + Rookie = random Rookie rumble; Multiverse = everyone.", foreground="#666"
        ).pack(side="left", padx=(8,0))

        container = ttk.LabelFrame(self.roster_tab, text="Current Battle Roster", padding=10)
        container.grid(row=3, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)
        self.roster_canvas = tk.Canvas(container, highlightthickness=0)
        self.roster_canvas.grid(row=0, column=0, sticky="nsew")
        rsb = ttk.Scrollbar(container, orient="vertical", command=self.roster_canvas.yview)
        rsb.grid(row=0, column=1, sticky="ns")
        self.roster_canvas.configure(yscrollcommand=rsb.set)
        self.roster_inner = ttk.Frame(self.roster_canvas)
        self.roster_window = self.roster_canvas.create_window((0,0), window=self.roster_inner, anchor="nw")
        self.roster_inner.bind("<Configure>", lambda e: self.roster_canvas.configure(scrollregion=self.roster_canvas.bbox("all")))
        self.roster_canvas.bind("<Configure>", lambda e: self.roster_canvas.itemconfigure(self.roster_window, width=e.width))

        controls = ttk.Frame(self.roster_tab)
        controls.grid(row=4, column=0, sticky="ew", pady=(10,0))
        ttk.Button(controls, text="Select All", command=self.select_all_roster).pack(side="left")
        ttk.Button(controls, text="Clear", command=self.clear_roster).pack(side="left", padx=4)
        ttk.Button(controls, text="Select DWC Rookies", command=self.select_dwc_rookies).pack(side="left", padx=4)
        ttk.Button(controls, text="Save Roster", command=self.save_roster_gui).pack(side="left", padx=(12,4))
        ttk.Button(controls, text="▶ PREVIEW BATTLE", command=self.launch_preview).pack(side="right", padx=(6,0), ipadx=8)
        ttk.Button(controls, text="🎥 RENDER VIDEO", command=self.launch_render).pack(side="right", ipadx=8)

    def _build_help_tab(self):
        text = (
            "RUMBLE STUDIO v0.13 — QUICK GUIDE\n\n"
            "CLEAN KOs\n"
            "Defeated fighters show their KO animation briefly, fade, then disappear from the arena. "
            "They stay crossed out in the HUD/results, so large rumbles stay readable without corpse piles.\n\n"
            "FINDING FIGHTERS\n"
            "Both the Fighter Library and Battle Roster have Search + Franchise + Stage/Form filters. "
            "Stage/Form can be Rookie, Champion, Mega, Base, Super Saiyan, Phase 2, or any label you choose.\n\n"
            "RANDOM FIGHTER SELECTOR\n"
            "Choose how many fighters you want, a franchise pool, and an optional Stage/Form. "
            "Digimon + Rookie gives a random Rookie-only battle. MULTIVERSE deliberately spreads picks "
            "across franchises when possible so one giant pack does not dominate every crossover.\n\n"
            "MUGEN IMPORT + BATCH IMPORT\n"
            "Import one character as before, choose ⚡ BATCH MUGEN FILES to select many .def/.zip/.rar/.7z files at once, "
            "or 📁 SCAN MUGEN FOLDER to point Studio at a collection/characters folder. Batch mode can use Quick import "
            "(only core mapped animations) or Full import (preserve hundreds of AIR actions for manual remapping).\n\n"
            "FIGHTER MAINTENANCE\n"
            "RESET TO SAVED discards unsaved edits and reloads the last fighter.json. DELETE FIGHTER permanently removes the fighter, removes it from the roster, and cleans transformation links that point to it. "
            "For MUGEN fighters, use the Lock checkboxes beside animation mappings and Lock / Unlock on attack rows to protect mappings you like. ↻ RESCAN UNLOCKED MUGEN rereads DEF/AIR/CMD/CNS files but only replaces unlocked mappings; locked paths/attacks stay fixed.\n\n"
            "DIGIMON WORLD CHAMPIONSHIP IMPORT\n"
            "Click 🦖 IMPORT DWC SPRITE SET. Choose ANY one 0-39 GIF for one Digimon, or the full ZIP for bulk import. "
            "The imported evolution stage is automatically written into Stage/Form for filtering.\n\n"
            "TRANSFORMATIONS\n"
            "Open Transformation Lab. Pick a root, then click forms in the Line Map and add one or more target fighters/forms with an after-KO "
            "chance, optional healing, and an announcement such as DIGIVOLVE! or POWER UP! HP percentage carries over, "
            "kills/position/target are preserved, and the target fighter's sprites, stats, attacks and AI take over instantly. "
            "Because every form is just another fighter pack, branches and long chains work across Digimon, Pokémon, Dragon Ball, bosses, or original characters.\n"
        )
        box = tk.Text(self.help_tab, wrap="word", font=("Segoe UI", 11), padx=10, pady=10)
        box.insert("1.0", text)
        box.configure(state="disabled")
        box.pack(fill="both", expand=True)

    def set_status(self, text: str):
        self.status_var.set(text)

    def _refresh_filter_choices(self):
        infos = all_fighter_summaries()
        franchises = sorted({i["franchise"] for i in infos if i["franchise"]}, key=str.lower)
        stages = sorted({i["stage"] for i in infos if i["stage"]}, key=str.lower)

        lib_fr = ["All franchises", *franchises]
        lib_st = ["All stages", *stages]
        self.library_franchise_combo["values"] = lib_fr
        self.library_stage_combo["values"] = lib_st
        if self.library_franchise_filter_var.get() not in lib_fr:
            self.library_franchise_filter_var.set("All franchises")
        if self.library_stage_filter_var.get() not in lib_st:
            self.library_stage_filter_var.set("All stages")

        if hasattr(self, "roster_franchise_combo"):
            self.roster_franchise_combo["values"] = lib_fr
            self.roster_stage_combo["values"] = lib_st
            if self.roster_franchise_filter_var.get() not in lib_fr:
                self.roster_franchise_filter_var.set("All franchises")
            if self.roster_stage_filter_var.get() not in lib_st:
                self.roster_stage_filter_var.set("All stages")

        if hasattr(self, "random_franchise_combo"):
            rnd_fr = ["MULTIVERSE (spread franchises)", *franchises]
            rnd_st = ["Any stage", *stages]
            self.random_franchise_combo["values"] = rnd_fr
            self.random_stage_combo["values"] = rnd_st
            if self.random_franchise_var.get() not in rnd_fr:
                self.random_franchise_var.set("MULTIVERSE (spread franchises)")
            if self.random_stage_var.get() not in rnd_st:
                self.random_stage_var.set("Any stage")

    def refresh_library(self, select: Optional[str] = None):
        self._refresh_filter_choices()
        infos = [
            i for i in all_fighter_summaries()
            if matches_filters(
                i, self.library_search_var.get(),
                self.library_franchise_filter_var.get(),
                self.library_stage_filter_var.get(),
            )
        ]
        self.library.delete(0, "end")
        items = []
        for info in infos:
            stage = f" • {info['stage']}" if info["stage"] else ""
            self.library.insert("end", f"{info['name']}  [{info['franchise']}{stage}]")
            items.append(info["pack"])
        self.library.pack_names = items
        if select in items:
            i = items.index(select)
            self.library.selection_set(i)
            self.library.see(i)

    def on_library_select(self, _event=None):
        sel = self.library.curselection()
        if not sel:
            return
        pack = self.library.pack_names[sel[0]]
        stay_in_lab = hasattr(self, "transform_tab") and self.tabs.select() == str(self.transform_tab)
        self.load_fighter(ASSETS / pack, select_editor=not stay_in_lab)
        if stay_in_lab:
            # v0.13: library clicks become a fast way to choose a new line/root while
            # staying inside Transformation Lab instead of kicking the user back to Editor.
            self.transform_lab_root_var.set(pack)
            self.transform_lab_source_var.set(pack)
            if self.chain_vars:
                self.chain_vars[0].set(pack)
            self.refresh_transform_line_tree()
            self.refresh_transformation_tree()
            self.tabs.select(self.transform_tab)

    def new_fighter(self):
        self.current_folder = None
        self.name_var.set("")
        self.franchise_var.set("Custom")
        self.stage_var.set("")
        self.facing_var.set("right")
        self.flip_var.set(True)
        self.scale_var.set("4.0")
        self.hp_var.set("100")
        self.speed_var.set("1.0")
        self.attack_mult_var.set("1.0")
        self.defense_var.set("1.0")
        self.ai_profile_var.set("auto")
        self.retaliation_var.set("0.55")
        self.evasion_var.set("0.25")
        for v in self.anim_paths.values():
            v.set("")
        for v in self.anim_lock_vars.values():
            v.set(False)
        self.attack_lock_slots.clear()
        self.attack_data = []
        self.transformation_data = []
        self.refresh_attack_tree()
        self.refresh_transformation_tree()
        self.stop_preview()
        self.preview_label.configure(image="", text="Choose an animation\nto preview")
        self.tabs.select(self.editor_tab)
        self.name_var.set("New Fighter")
        self.set_status("New fighter ready. Replace the name and map your sprite files.")

    def import_mugen_gui(self):
        source = filedialog.askopenfilename(
            title="Import MUGEN character — choose .def or character archive",
            filetypes=[
                ("MUGEN character / archive", "*.def *.zip *.rar *.7z"),
                ("MUGEN DEF", "*.def"),
                ("Archives", "*.zip *.rar *.7z"),
                ("All files", "*.*"),
            ],
            parent=self.root,
        )
        if not source:
            return
        franchise = simpledialog.askstring(
            "MUGEN Import",
            "Franchise / pack label for this fighter:\n\nExamples: Dragon Ball, Marvel, DC, Street Fighter",
            initialvalue="MUGEN Import",
            parent=self.root,
        )
        if franchise is None:
            return
        self.set_status(f"Importing MUGEN character from {Path(source).name}… this can take a moment.")
        self.root.update_idletasks()
        try:
            folder, report = import_mugen_character(Path(source), ASSETS, franchise=franchise.strip() or "MUGEN Import")
            self.refresh_library(select=folder.name)
            self.refresh_roster()
            self.load_fighter(folder)
            self.set_status(
                f"Imported {report['name']}: {report['sprite_count']} sprites, "
                f"{report['action_count']} AIR actions, {report['exported_count']} rendered."
            )
            mapping = report.get("mapping", {})
            mapped = ", ".join(f"{k}=Action {v}" for k, v in mapping.items())
            messagebox.showinfo(
                "MUGEN import complete",
                f"{report['name']} is now a Rumble fighter!\n\n"
                f"Sprites decoded: {report['sprite_count']}\n"
                f"AIR actions found: {report['action_count']}\n"
                f"AIR actions rendered: {report['exported_count']}\n"
                f"Sprite format: {report.get('sff_kind', 'SFF')} {'.'.join(map(str, report.get('sff_version', ())))}\n\n"
                f"Auto mapping:\n{mapped[:700]}\n\n"
                "The fighter is open in Fighter Editor now. Preview the mapped states, check Native Facing/Scale, "
                "and edit attack types if you want ranged or beam behavior.\n\n"
                "See MUGEN_IMPORT_REPORT.txt inside the fighter folder for the full action list.",
                parent=self.root,
            )
        except MugenImportError as e:
            self.set_status("MUGEN import stopped. See the message for what this character needs.")
            messagebox.showerror("MUGEN import", str(e), parent=self.root)
        except Exception as e:
            self.set_status("MUGEN import failed.")
            messagebox.showerror("MUGEN import", f"Unexpected import error:\n{e}", parent=self.root)

    def _mugen_batch_options(self):
        franchise = simpledialog.askstring(
            "Batch MUGEN Import",
            "Franchise / pack label for this batch:\n\nExamples: Dragon Ball, Marvel, Mixed MUGEN",
            initialvalue="MUGEN Import", parent=self.root,
        )
        if franchise is None:
            return None
        full = messagebox.askyesno(
            "Batch MUGEN Import",
            "Use FULL import mode?\n\nYES = render/preserve up to 600 AIR actions per character. Best for editing later, but slower.\n\nNO = QUICK TEST mode. Render only core auto-mapped animations. Much faster for large batches.",
            parent=self.root,
        )
        return franchise.strip() or "MUGEN Import", full

    def _run_mugen_batch(self, sources: List[Path], franchise: str, export_all_actions: bool):
        sources = [Path(x) for x in sources if Path(x).exists()]
        if not sources:
            messagebox.showinfo("Batch MUGEN Import", "No importable MUGEN sources were selected/found.", parent=self.root)
            return
        imported = []
        failed = []
        mode = "FULL" if export_all_actions else "QUICK"
        for n, source in enumerate(sources, 1):
            self.set_status(f"Batch MUGEN {mode}: {n}/{len(sources)} • {source.name}")
            self.root.update_idletasks()
            try:
                folder, report = import_mugen_character(
                    source, ASSETS, franchise=franchise, export_all_actions=export_all_actions
                )
                imported.append((folder, report, source))
            except Exception as e:
                failed.append((source, str(e)))

        lines = [
            "RUMBLE STUDIO v0.13 — MUGEN BATCH IMPORT REPORT",
            "=================================================",
            f"Mode: {mode}",
            f"Requested sources: {len(sources)}",
            f"Imported: {len(imported)}",
            f"Failed: {len(failed)}",
            "", "IMPORTED", "--------",
        ]
        for folder, report, source in imported:
            lines.append(f"OK  {source.name} -> {report.get('name', folder.name)}  ({report.get('sprite_count', '?')} sprites / {report.get('exported_count', '?')} AIR renders)")
        lines += ["", "FAILED", "------"]
        for source, err in failed:
            lines.append(f"FAIL  {source}: {err}")
        report_path = ROOT / "MUGEN_BATCH_IMPORT_REPORT.txt"
        report_path.write_text("\n".join(lines), encoding="utf-8")

        self.refresh_library(select=imported[-1][0].name if imported else None)
        self.refresh_roster()
        if imported:
            self.load_fighter(imported[-1][0])
        self.set_status(f"Batch MUGEN complete: {len(imported)} imported, {len(failed)} failed. See MUGEN_BATCH_IMPORT_REPORT.txt")
        preview_fail = "\n".join(f"• {src.name}: {err[:150]}" for src, err in failed[:5])
        extra = f"\n\nFirst failures:\n{preview_fail}" if failed else ""
        messagebox.showinfo(
            "Batch MUGEN Import complete",
            f"Imported: {len(imported)}\nFailed: {len(failed)}\nMode: {mode}\n\nFull details: MUGEN_BATCH_IMPORT_REPORT.txt{extra}",
            parent=self.root,
        )

    def import_mugen_batch_files_gui(self):
        selected = filedialog.askopenfilenames(
            title="Batch MUGEN import — select multiple archives / DEF files",
            filetypes=[
                ("MUGEN characters / archives", "*.def *.zip *.rar *.7z"),
                ("Archives", "*.zip *.rar *.7z"),
                ("MUGEN DEF", "*.def"),
                ("All files", "*.*"),
            ], parent=self.root,
        )
        if not selected:
            return
        opts = self._mugen_batch_options()
        if opts is None:
            return
        franchise, full = opts
        self._run_mugen_batch([Path(x) for x in selected], franchise, full)

    def import_mugen_batch_folder_gui(self):
        folder = filedialog.askdirectory(
            title="Scan a MUGEN collection / chars folder", parent=self.root
        )
        if not folder:
            return
        sources = discover_mugen_sources(Path(folder))
        if not sources:
            messagebox.showinfo(
                "Scan MUGEN Folder",
                "I couldn't find .zip/.rar/.7z character archives or usable .def files under that folder.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "Scan MUGEN Folder",
            f"Found {len(sources)} character source(s). Import them all?\n\n" + "\n".join(f"• {x.name}" for x in sources[:10]) + (f"\n• +{len(sources)-10} more" if len(sources) > 10 else ""),
            parent=self.root,
        ):
            return
        opts = self._mugen_batch_options()
        if opts is None:
            return
        franchise, full = opts
        self._run_mugen_batch(sources, franchise, full)

    def import_dwc_gui(self):
        source = filedialog.askopenfilename(
            title="Import Digimon World Championship sprites — choose ANY one GIF or the full ZIP",
            filetypes=[
                ("DWC sprite / archive", "*.gif *.zip"),
                ("Animated GIF", "*.gif"),
                ("ZIP archive", "*.zip"),
                ("All files", "*.*"),
            ],
            parent=self.root,
        )
        if not source:
            return
        source_path = Path(source)
        stage_filter = None
        if source_path.suffix.lower() == ".zip":
            stage = simpledialog.askstring(
                "DWC Bulk Import",
                "Which evolution stage should I import from this ZIP?\n\n"
                "Type Rookie for the current World Championship pack.\n"
                "Leave blank to import every compatible 40-GIF set in the ZIP.",
                initialvalue="Rookie",
                parent=self.root,
            )
            if stage is None:
                return
            stage_filter = stage.strip() or None
        self.set_status(f"Scanning DWC sprites from {source_path.name}…")
        self.root.update_idletasks()
        try:
            report = import_dwc_source(source_path, ASSETS, stage_filter=stage_filter, overwrite=False)
            first = report["first_folder"]
            self.refresh_library(select=first.name)
            self.refresh_roster()
            self.load_fighter(first)
            imported = report["imported"]
            skipped = report["skipped"]
            total_states = sum(len(dset.states) for dset, _ in imported)
            self.set_status(f"Imported {len(imported)} DWC fighter(s), {total_states} sprite states total.")
            first_set = imported[0][0]
            if len(imported) == 1:
                summary = (
                    f"{first_set.display_name} is ready.\n\n"
                    f"States found: {len(first_set.states)}/40\n"
                    "Idle/walk/run/hit/attack/guard/KO/victory were auto-mapped.\n"
                    "All 0-39 GIFs were preserved in the fighter folder."
                )
            else:
                summary = (
                    f"Imported {len(imported)} fighters from the DWC archive.\n\n"
                    f"Sprite states copied: {total_states}\n"
                    f"Skipped: {len(skipped)}\n\n"
                    "Each fighter keeps all available 0-39 states and the states Rumble uses are auto-mapped."
                )
            messagebox.showinfo(
                "DWC import complete",
                summary + "\n\nTip: when importing one Digimon later, choose ANY ONE of its 0-39 GIFs. "
                "Rumble Studio automatically finds the rest beside it.",
                parent=self.root,
            )
        except DWCImportError as e:
            self.set_status("DWC import stopped. See the message for details.")
            messagebox.showerror("DWC import", str(e), parent=self.root)
        except Exception as e:
            self.set_status("DWC import failed.")
            messagebox.showerror("DWC import", f"Unexpected DWC import error:\n{e}", parent=self.root)

    def reset_current_fighter(self):
        """Discard unsaved edits for the current fighter and reload fighter.json."""
        if not self.current_folder or not (self.current_folder / "fighter.json").exists():
            if messagebox.askyesno("Reset fighter", "Reset this unsaved new fighter back to the blank defaults?", parent=self.root):
                self.new_fighter()
            return
        pack = self.current_folder.name
        name = self.name_var.get().strip() or pack
        if not messagebox.askyesno(
            "Reset to saved",
            f"Discard unsaved editor changes for {name} and reload the last saved fighter.json?\n\n"
            "Any unsaved Transformation Lab changes for this fighter will also be discarded.",
            parent=self.root,
        ):
            return
        self.transform_lab_rules.pop(pack, None)
        self.transform_lab_dirty.discard(pack)
        self._update_transform_dirty_label()
        self.load_fighter(self.current_folder)
        self.refresh_transform_line_tree()
        self.refresh_transformation_tree()
        self.set_status(f"Reset {name} to the last saved version.")

    def delete_current_fighter(self):
        """Delete the selected fighter pack and clean roster/transformation references."""
        if not self.current_folder or not (self.current_folder / "fighter.json").exists():
            messagebox.showinfo("Delete fighter", "Choose a saved fighter from the library first.", parent=self.root)
            return
        folder = self.current_folder
        pack = folder.name
        meta = read_json(folder / "fighter.json", {})
        name = str(meta.get("name", pack))
        incoming = []
        for other in list_fighter_packs():
            if other == pack:
                continue
            ometa = read_json(ASSETS / other / "fighter.json", {})
            for rule in ometa.get("transformations", []) if isinstance(ometa.get("transformations", []), list) else []:
                if isinstance(rule, dict) and str(rule.get("target_pack", "")) == pack:
                    incoming.append(other)
                    break
        detail = f"\n\nThis will also remove it from the current roster."
        if incoming:
            detail += f"\nIt is referenced by {len(incoming)} transformation source(s); those links will be removed too."
        if not messagebox.askyesno(
            "Delete fighter",
            f"Permanently delete {name}?\n\nFolder: {folder}{detail}\n\nThis cannot be undone from Rumble Studio.",
            parent=self.root,
        ):
            return
        # Remove incoming transformation links from saved fighters.
        for other in incoming:
            path = ASSETS / other / "fighter.json"
            ometa = read_json(path, {})
            rules = [
                r for r in ometa.get("transformations", [])
                if not (isinstance(r, dict) and str(r.get("target_pack", "")) == pack)
            ]
            ometa["transformations"] = rules
            path.write_text(json.dumps(ometa, indent=2), encoding="utf-8")
            if other in self.transform_lab_rules:
                self.transform_lab_rules[other] = [dict(r) for r in rules if isinstance(r, dict)]
                self.transform_lab_dirty.discard(other)
        # Remove from roster without requiring the roster to still contain 2 fighters.
        roster = load_roster()
        roster["fighters"] = [f for f in roster.get("fighters", []) if f.get("pack") != pack]
        ROSTER_FILE.write_text(json.dumps(roster, indent=2), encoding="utf-8")
        self.transform_lab_rules.pop(pack, None)
        self.transform_lab_dirty.discard(pack)
        self.roster_vars.pop(pack, None)
        if self.transform_lab_root_var.get() == pack:
            self.transform_lab_root_var.set("")
        if self.transform_lab_source_var.get() == pack:
            self.transform_lab_source_var.set("")
        for v in self.chain_vars:
            if v.get() == pack:
                v.set("")
        self.stop_preview()
        shutil.rmtree(folder, ignore_errors=False)
        self.current_folder = None
        self.refresh_library()
        self.refresh_roster()
        self.refresh_transform_lab_choices()
        self.new_fighter()
        self.set_status(f"Deleted {name} and cleaned its roster/transformation references.")

    def _locate_mugen_rescan_source(self, meta: dict) -> Optional[Path]:
        source = meta.get("source", {}) if isinstance(meta.get("source", {}), dict) else {}
        stored = str(source.get("source_path", "") or "").strip()
        if stored and Path(stored).exists():
            return Path(stored)
        picked = filedialog.askopenfilename(
            title="Locate the original MUGEN .def / archive for this fighter",
            filetypes=[
                ("MUGEN character / archive", "*.def *.zip *.rar *.7z"),
                ("Archives", "*.zip *.rar *.7z"),
                ("MUGEN DEF", "*.def"),
                ("All files", "*.*"),
            ],
            parent=self.root,
        )
        if picked:
            return Path(picked)
        # Canceling the file picker gives extracted-folder users a second path.
        folder = filedialog.askdirectory(title="Or choose an extracted MUGEN character folder", parent=self.root)
        return Path(folder) if folder else None

    def rescan_mugen_current(self):
        """Reread MUGEN files, preserving every mapping the user explicitly locked."""
        if not self.current_folder or not (self.current_folder / "fighter.json").exists():
            messagebox.showinfo("Rescan MUGEN", "Choose a saved MUGEN fighter first.", parent=self.root)
            return
        meta = read_json(self.current_folder / "fighter.json", {})
        source_meta = meta.get("source", {}) if isinstance(meta.get("source", {}), dict) else {}
        if str(source_meta.get("format", "")).upper() != "MUGEN":
            messagebox.showinfo("Rescan MUGEN", "This fighter was not imported from MUGEN, so there is no MUGEN source to rescan.", parent=self.root)
            return

        # Make the checkboxes authoritative even if the user has not pressed Save Fighter.
        self._persist_mugen_locks()
        meta = read_json(self.current_folder / "fighter.json", {})
        locks = self._current_mugen_locks()
        locked_anims = list(locks.get("animations", []))
        locked_attacks = list(locks.get("attacks", []))

        source = self._locate_mugen_rescan_source(meta)
        if source is None:
            return
        name = str(meta.get("name", self.current_folder.name))
        lock_summary = (
            f"Locked animation slots: {len(locked_anims)}\n"
            f"Locked attack slots: {len(locked_attacks)}\n"
            "Only UNLOCKED MUGEN mappings will be researched/replaced."
        )
        if not messagebox.askyesno(
            "Rescan unlocked MUGEN mappings",
            f"Rescan {name} from:\n{source}\n\n"
            "v0.13 rereads DEF + AIR + CMD + CNS/ST files and searches again for better mappings.\n\n"
            f"{lock_summary}\n\n"
            "Saved stats, stage/form, facing, AI, scale, transformations, and all locked mappings are preserved.\n\n"
            "Unsaved edits other than the lock checkboxes are not preserved. Continue?",
            parent=self.root,
        ):
            return
        self.set_status(f"Rescanning only unlocked MUGEN mappings for {name}…")
        self.root.update_idletasks()
        try:
            folder, report = import_mugen_character(
                source, ASSETS, franchise=str(meta.get("franchise", "MUGEN Import")),
                export_all_actions=True, existing_target=self.current_folder, preserve_existing=True,
                locked_animation_slots=set(locked_anims), locked_attack_slots=set(locked_attacks),
            )
            self.load_fighter(folder)
            self.refresh_library(select=folder.name)
            self.refresh_roster()
            candidates = report.get("deep_attack_candidates", [])
            picks = report.get("chosen_moves", [])
            refreshed = report.get("refreshed_attack_slots", [])
            preserved = report.get("preserved_attack_slots", [])
            pick_text = "\n".join(
                f"• {x.get('slot','?')}: {x.get('name','Move')} → Action {x.get('action')} ({x.get('type','melee')})"
                for x in picks if not x.get("locked")
            ) or "• No unlocked attack slots needed a new automatic pick."
            self.set_status(
                f"MUGEN rescan complete for {name}: preserved {len(locked_anims)} animation locks and {len(preserved)} attack locks."
            )
            messagebox.showinfo(
                "MUGEN rescan complete",
                f"{name} was rescanned in place.\n\n"
                f"Sprites decoded: {report.get('sprite_count', '?')}\n"
                f"AIR actions: {report.get('action_count', '?')}\n"
                f"Deep attack candidates: {len(candidates)}\n\n"
                f"Preserved locked animation slots: {len(report.get('preserved_animation_slots', []))}\n"
                f"Preserved locked attack slots: {', '.join(preserved) if preserved else 'None'}\n"
                f"Refreshed attack slots: {', '.join(refreshed) if refreshed else 'None'}\n\n"
                f"New picks for UNLOCKED slots:\n{pick_text}\n\n"
                "Anything marked Lock stays on the same mapping through future rescans until you unlock it.",
                parent=self.root,
            )
        except Exception as e:
            self.set_status("MUGEN rescan failed; the existing fighter was left in place where possible.")
            messagebox.showerror("Rescan MUGEN", str(e), parent=self.root)

    def duplicate_fighter(self):
        if not self.current_folder:
            messagebox.showinfo("Duplicate fighter", "Choose a fighter from the library first.")
            return
        self.current_folder = None
        self.name_var.set(self.name_var.get() + " Copy")
        self.set_status("Loaded as a new copy. Change the name, then Save Fighter.")

    def load_fighter(self, folder: Path, select_editor: bool = True):
        meta = read_json(folder / "fighter.json", {})
        self.current_folder = folder
        self.name_var.set(str(meta.get("name", folder.name)))
        self.franchise_var.set(str(meta.get("franchise", "Custom")))
        source = meta.get("source", {}) if isinstance(meta.get("source", {}), dict) else {}
        self.stage_var.set(str(meta.get("stage", "") or source.get("stage", "") or ""))
        self.facing_var.set(str(meta.get("native_facing", "right")))
        self.flip_var.set(bool(meta.get("allow_horizontal_flip", True)))
        self.scale_var.set(str(meta.get("scale", 4.0)))
        stats = meta.get("stats", {})
        self.hp_var.set(str(stats.get("hp", 100)))
        self.speed_var.set(str(stats.get("speed", 1.0)))
        self.attack_mult_var.set(str(stats.get("attack", 1.0)))
        self.defense_var.set(str(stats.get("defense", 1.0)))
        ai = meta.get("ai", {})
        self.ai_profile_var.set(str(ai.get("profile", "auto")))
        self.retaliation_var.set(str(ai.get("retaliation", 0.55)))
        self.evasion_var.set(str(ai.get("evasion", 0.25)))
        locks = meta.get("mugen_locks", {}) if isinstance(meta.get("mugen_locks", {}), dict) else {}
        locked_anims = {str(x) for x in locks.get("animations", [])}
        self.attack_lock_slots = {str(x) for x in locks.get("attacks", [])}
        for slot, _ in ANIMATION_SLOTS:
            filename = meta.get("animations", {}).get(slot, "")
            self.anim_paths[slot].set(str(folder / filename) if filename else "")
            self.anim_lock_vars[slot].set(slot in locked_anims)
        self.attack_data = [dict(a) for a in meta.get("attacks", [])]
        self.transformation_data = [dict(a) for a in meta.get("transformations", []) if isinstance(a, dict)]
        # Keep an already-staged Lab version authoritative; otherwise seed the Lab cache from disk.
        if folder.name not in self.transform_lab_rules:
            self.transform_lab_rules[folder.name] = [dict(a) for a in self.transformation_data]
        self.refresh_attack_tree()
        self.refresh_transform_lab_choices()
        if select_editor:
            self.tabs.select(self.editor_tab)
        self.set_status(f"Loaded {self.name_var.get()} for editing.")

    def browse_animation(self, slot: str):
        path = filedialog.askopenfilename(
            title=f"Choose {slot} sprite/animation",
            filetypes=[("Sprite images", "*.png *.gif *.webp *.jpg *.jpeg *.bmp *.apng"), ("All files", "*.*")],
            parent=self.root,
        )
        if not path:
            return
        if not image_is_usable(Path(path)):
            messagebox.showerror("Sprite file", "That file could not be opened as an image/animation.")
            return
        self.anim_paths[slot].set(path)
        self.preview_animation(slot)

    def stop_preview(self):
        if self.preview_job:
            try:
                self.root.after_cancel(self.preview_job)
            except Exception:
                pass
            self.preview_job = None
        self.preview_anim = None

    def preview_animation(self, slot: str):
        path = self.anim_paths[slot].get().strip()
        if not path:
            messagebox.showinfo("Preview", f"No file mapped to {slot} yet.")
            return
        try:
            im = Image.open(path)
            frames, durations = [], []
            for frame in ImageSequence.Iterator(im):
                fr = frame.convert("RGBA")
                # Pixel-art friendly preview, constrained to 180x180.
                scale = min(6.0, 180 / max(1, fr.width), 180 / max(1, fr.height))
                if scale > 1:
                    fr = fr.resize((max(1,int(fr.width*scale)), max(1,int(fr.height*scale))), Image.Resampling.NEAREST)
                frames.append(fr)
                durations.append(max(60, int(frame.info.get("duration", im.info.get("duration", 120)))))
            if not frames:
                frames = [im.convert("RGBA")]
                durations = [160]
            self.stop_preview()
            self.preview_anim = PreviewAnimation(frames, durations)
            self._preview_tick()
            self.set_status(f"Previewing {slot}: {Path(path).name}")
        except Exception as e:
            messagebox.showerror("Preview", f"Could not preview this file:\n{e}")

    def _preview_tick(self):
        if not self.preview_anim:
            return
        pa = self.preview_anim
        fr = pa.frames[pa.index]
        bg = Image.new("RGBA", (220, 210), (32, 38, 48, 255))
        x = (220 - fr.width)//2
        y = (210 - fr.height)//2
        bg.alpha_composite(fr, (x,y))
        photo = ImageTk.PhotoImage(bg)
        self.preview_label.configure(image=photo, text="")
        self.preview_label.image = photo
        delay = pa.durations[pa.index]
        pa.index = (pa.index + 1) % len(pa.frames)
        self.preview_job = self.root.after(delay, self._preview_tick)

    def add_attack(self):
        dlg = AttackDialog(self.root)
        self.root.wait_window(dlg)
        if dlg.result:
            self.attack_data.append(dlg.result)
            self.refresh_attack_tree()

    def edit_attack(self):
        sel = self.attack_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        dlg = AttackDialog(self.root, self.attack_data[idx])
        self.root.wait_window(dlg)
        if dlg.result:
            self.attack_data[idx] = dlg.result
            self.refresh_attack_tree()

    def remove_attack(self):
        sel = self.attack_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        self.attack_data.pop(idx)
        self.refresh_attack_tree()

    def refresh_attack_tree(self):
        for item in self.attack_tree.get_children():
            self.attack_tree.delete(item)
        for i, a in enumerate(self.attack_data):
            slot = str(a.get("animation", "attack_1"))
            vals = ("YES" if slot in self.attack_lock_slots else "", a.get("name","Attack"), a.get("type","melee"), slot, a.get("damage",12), a.get("range",54), a.get("cooldown",1.0))
            self.attack_tree.insert("", "end", iid=str(i), values=vals)

    # ------------------------------------------------------------------
    # MUGEN mapping locks (v0.13)
    # ------------------------------------------------------------------
    def _current_mugen_locks(self) -> dict:
        # Locking an attack also protects the animation slot it uses, so a
        # successful rescan cannot leave the attack pointing at new artwork.
        anims = {slot for slot, var in self.anim_lock_vars.items() if bool(var.get())}
        anims.update(self.attack_lock_slots)
        return {
            "animations": sorted(anims),
            "attacks": sorted(self.attack_lock_slots),
        }

    def _persist_mugen_locks(self):
        """Persist just the lock metadata immediately without saving unrelated edits."""
        if not self.current_folder or not (self.current_folder / "fighter.json").exists():
            return
        path = self.current_folder / "fighter.json"
        meta = read_json(path, {})
        meta["mugen_locks"] = self._current_mugen_locks()
        path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def _animation_lock_changed(self, slot: str):
        self._persist_mugen_locks()
        state = "locked" if self.anim_lock_vars[slot].get() else "unlocked"
        self.set_status(f"{slot} mapping {state}. MUGEN rescan will preserve locked mappings.")

    def toggle_selected_attack_lock(self):
        sel = self.attack_tree.selection()
        if not sel:
            messagebox.showinfo("Attack lock", "Choose an attack row first.", parent=self.root)
            return
        idx = int(sel[0])
        if idx < 0 or idx >= len(self.attack_data):
            return
        slot = str(self.attack_data[idx].get("animation", "attack_1"))
        if slot in self.attack_lock_slots:
            self.attack_lock_slots.discard(slot)
            state = "unlocked"
        else:
            self.attack_lock_slots.add(slot)
            # Keep the actual AIR/action mapping with the attack definition.
            if slot in self.anim_lock_vars:
                self.anim_lock_vars[slot].set(True)
            state = "locked"
        self._persist_mugen_locks()
        self.refresh_attack_tree()
        self.attack_tree.selection_set(str(idx))
        self.set_status(f"Attack slot {slot} {state}.")

    def lock_all_mapped(self):
        for slot, var in self.anim_lock_vars.items():
            if self.anim_paths[slot].get().strip():
                var.set(True)
        for a in self.attack_data:
            slot = str(a.get("animation", ""))
            if slot:
                self.attack_lock_slots.add(slot)
                if slot in self.anim_lock_vars:
                    self.anim_lock_vars[slot].set(True)
        self._persist_mugen_locks()
        self.refresh_attack_tree()
        self.set_status("Locked all currently mapped animations and attacks.")

    def unlock_all_mappings(self):
        for var in self.anim_lock_vars.values():
            var.set(False)
        self.attack_lock_slots.clear()
        self._persist_mugen_locks()
        self.refresh_attack_tree()
        self.set_status("All MUGEN mapping locks cleared.")

    # ------------------------------------------------------------------
    # Transformation Lab (v0.13)
    # ------------------------------------------------------------------
    def _lab_rules(self, pack: str) -> List[dict]:
        pack = str(pack or "").strip()
        if not pack:
            return []
        if pack not in self.transform_lab_rules:
            meta = read_json(ASSETS / pack / "fighter.json", {})
            self.transform_lab_rules[pack] = [dict(x) for x in meta.get("transformations", []) if isinstance(x, dict)]
        return self.transform_lab_rules[pack]

    def _set_lab_rules(self, pack: str, rules: List[dict]):
        if not pack:
            return
        self.transform_lab_rules[pack] = [dict(x) for x in rules if isinstance(x, dict)]
        self.transform_lab_dirty.add(pack)
        if self.current_folder and self.current_folder.name == pack:
            self.transformation_data = [dict(x) for x in self.transform_lab_rules[pack]]
        self._update_transform_dirty_label()

    def _update_transform_dirty_label(self):
        if self.transform_lab_dirty:
            names = []
            for pack in sorted(self.transform_lab_dirty, key=str.lower):
                info = fighter_summary(pack) if (ASSETS / pack / "fighter.json").exists() else {"name": pack}
                names.append(info.get("name", pack))
            shown = ", ".join(names[:5]) + (f" +{len(names)-5} more" if len(names) > 5 else "")
            self.transform_lab_dirty_var.set(f"Unsaved line changes: {shown}")
        else:
            self.transform_lab_dirty_var.set("No unsaved line changes.")

    def refresh_transform_lab_choices(self):
        if not hasattr(self, "transform_root_combo"):
            return
        packs = list_fighter_packs()
        for combo in (self.transform_root_combo, self.transform_source_combo):
            combo["values"] = packs
        for combo in getattr(self, "chain_combos", []):
            combo["values"] = ["", *packs]
        if not self.transform_lab_root_var.get() and packs:
            preferred = self.current_folder.name if self.current_folder and self.current_folder.name in packs else packs[0]
            self.transform_lab_root_var.set(preferred)
        if not self.transform_lab_source_var.get() and self.transform_lab_root_var.get():
            self.transform_lab_source_var.set(self.transform_lab_root_var.get())
        if self.chain_vars and self.transform_lab_root_var.get() and not self.chain_vars[0].get():
            self.chain_vars[0].set(self.transform_lab_root_var.get())
        self.refresh_transform_line_tree()
        self.refresh_transformation_tree()
        self._update_transform_dirty_label()

    def transform_use_current_as_root(self):
        if not self.current_folder:
            messagebox.showinfo("Transformation Lab", "Choose a fighter from the Fighter Library first.", parent=self.root)
            return
        pack = self.current_folder.name
        self.transform_lab_root_var.set(pack)
        self.transform_lab_source_var.set(pack)
        if self.chain_vars:
            self.chain_vars[0].set(pack)
        self.refresh_transform_line_tree()
        self.refresh_transformation_tree()

    def _transform_root_changed(self):
        root = self.transform_lab_root_var.get().strip()
        if root:
            self.transform_lab_source_var.set(root)
            if self.chain_vars:
                self.chain_vars[0].set(root)
        self.refresh_transform_line_tree()
        self.refresh_transformation_tree()

    def _transform_source_changed(self):
        self.refresh_transformation_tree()

    def _transform_line_selected(self, _event=None):
        sel = self.transform_line_tree.selection()
        if not sel:
            return
        pack = self.transform_line_pack_by_iid.get(sel[0], "")
        if pack and (ASSETS / pack / "fighter.json").exists():
            self.transform_lab_source_var.set(pack)
            self.refresh_transformation_tree()

    def refresh_transform_line_tree(self):
        if not hasattr(self, "transform_line_tree"):
            return
        for item in self.transform_line_tree.get_children():
            self.transform_line_tree.delete(item)
        self.transform_line_pack_by_iid = {}
        root = self.transform_lab_root_var.get().strip()
        if not root or not (ASSETS / root / "fighter.json").exists():
            return
        serial = [0]

        def add_node(pack: str, parent: str, edge_rule: Optional[dict], path: tuple[str, ...]):
            serial[0] += 1
            iid = f"line_{serial[0]}"
            exists = (ASSETS / pack / "fighter.json").exists()
            info = fighter_summary(pack) if exists else {"name": pack or "Missing fighter", "stage": "MISSING", "franchise": ""}
            chance = "ROOT" if edge_rule is None else f"{float(edge_rule.get('chance',0) or 0):g}%"
            heal = "—" if edge_rule is None else f"{float(edge_rule.get('heal_percent',0) or 0):g}%"
            label = info.get("name", pack)
            if pack in path:
                label += "  ↩ cycle"
            if not exists:
                label += "  ⚠ missing"
            self.transform_line_tree.insert(parent, "end", iid=iid, text=label, values=(info.get("stage", ""), chance, heal), open=True)
            self.transform_line_pack_by_iid[iid] = pack
            if not exists or pack in path:
                return
            new_path = path + (pack,)
            for rule in self._lab_rules(pack):
                target = str(rule.get("target_pack", "") or "").strip()
                if target:
                    add_node(target, iid, rule, new_path)

        add_node(root, "", None, tuple())

    def add_transformation(self):
        source = self.transform_lab_source_var.get().strip()
        if not source:
            messagebox.showinfo("Transformation Lab", "Choose the source form first.", parent=self.root)
            return
        dlg = TransformationDialog(self.root, current_pack=source)
        self.root.wait_window(dlg)
        if dlg.result:
            rules = [dict(x) for x in self._lab_rules(source)]
            rules.append(dlg.result)
            self._set_lab_rules(source, rules)
            self.refresh_transformation_tree()
            self.refresh_transform_line_tree()

    def edit_transformation(self):
        source = self.transform_lab_source_var.get().strip()
        sel = self.transform_tree.selection()
        if not source or not sel:
            return
        idx = int(sel[0])
        rules = [dict(x) for x in self._lab_rules(source)]
        if idx >= len(rules):
            return
        dlg = TransformationDialog(self.root, rules[idx], current_pack=source)
        self.root.wait_window(dlg)
        if dlg.result:
            rules[idx] = dlg.result
            self._set_lab_rules(source, rules)
            self.refresh_transformation_tree()
            self.refresh_transform_line_tree()

    def remove_transformation(self):
        source = self.transform_lab_source_var.get().strip()
        sel = self.transform_tree.selection()
        if not source or not sel:
            return
        rules = [dict(x) for x in self._lab_rules(source)]
        idx = int(sel[0])
        if idx < len(rules):
            rules.pop(idx)
            self._set_lab_rules(source, rules)
            self.refresh_transformation_tree()
            self.refresh_transform_line_tree()

    def clear_transformations(self):
        source = self.transform_lab_source_var.get().strip()
        rules = self._lab_rules(source) if source else []
        if rules and messagebox.askyesno(
            "Clear transformations", f"Remove every transformation branch from {fighter_summary(source).get('name', source)}?", parent=self.root
        ):
            self._set_lab_rules(source, [])
            self.refresh_transformation_tree()
            self.refresh_transform_line_tree()

    def follow_transform_target(self):
        sel = self.transform_tree.selection()
        source = self.transform_lab_source_var.get().strip()
        if not sel or not source:
            return
        rules = self._lab_rules(source)
        idx = int(sel[0])
        if idx >= len(rules):
            return
        target = str(rules[idx].get("target_pack", "") or "")
        if target and (ASSETS / target / "fighter.json").exists():
            self.transform_lab_source_var.set(target)
            self.refresh_transformation_tree()
            # Select the first visible tree node for this target when possible.
            for iid, pack in self.transform_line_pack_by_iid.items():
                if pack == target:
                    self.transform_line_tree.selection_set(iid)
                    self.transform_line_tree.see(iid)
                    break

    def refresh_transformation_tree(self):
        if not hasattr(self, "transform_tree"):
            return
        for item in self.transform_tree.get_children():
            self.transform_tree.delete(item)
        source = self.transform_lab_source_var.get().strip()
        if not source:
            self.transform_current_label.configure(text="Choose a source form in the Line Map.")
            self.transform_lab_summary_var.set("No source form selected.")
            return
        exists = (ASSETS / source / "fighter.json").exists()
        info = fighter_summary(source) if exists else {"name": source, "stage": "MISSING", "franchise": ""}
        stage = f" • {info.get('stage')}" if info.get("stage") else ""
        self.transform_current_label.configure(text=f"Editing: {info.get('name', source)}{stage} — what can this form become after a KO?")
        total = 0.0
        rules = self._lab_rules(source)
        for i, rule in enumerate(rules):
            target_pack = str(rule.get("target_pack", ""))
            target_info = fighter_summary(target_pack) if (ASSETS / target_pack / "fighter.json").exists() else {"name": target_pack or "Missing fighter", "stage": "MISSING"}
            chance = float(rule.get("chance", 0.0) or 0.0)
            total += chance
            vals = (
                target_info.get("name", target_pack), target_info.get("stage", ""), f"{chance:g}%",
                f"{float(rule.get('heal_percent',0) or 0):g}%", rule.get("label", "TRANSFORM!"),
            )
            self.transform_tree.insert("", "end", iid=str(i), values=vals)
        if not rules:
            summary = "No outgoing branches yet. This form currently cannot transform after a KO."
        elif total < 100:
            summary = f"{len(rules)} branch(es). {total:g}% total transform chance after each KO; {100-total:g}% chance to remain in this form."
        else:
            summary = f"{len(rules)} branch(es). Transformation is guaranteed after a KO; {total:g} total points are used as relative branch weights."
        self.transform_lab_summary_var.set(summary)

    def save_transform_lab(self):
        if not self.transform_lab_dirty:
            self.set_status("Transformation Lab: nothing new to save.")
            return
        saved = []
        for pack in sorted(self.transform_lab_dirty, key=str.lower):
            path = ASSETS / pack / "fighter.json"
            if not path.exists():
                continue
            meta = read_json(path, {})
            meta["transformations"] = [dict(x) for x in self._lab_rules(pack)]
            path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            saved.append(pack)
        if self.current_folder and self.current_folder.name in self.transform_lab_rules:
            self.transformation_data = [dict(x) for x in self.transform_lab_rules[self.current_folder.name]]
        self.transform_lab_dirty.clear()
        self._update_transform_dirty_label()
        self.refresh_transform_line_tree()
        self.set_status(f"Saved transformation rules for {len(saved)} fighter form(s).")
        messagebox.showinfo("Transformation Lab", f"Saved the line changes for {len(saved)} fighter form(s).", parent=self.root)

    def reload_transform_lab(self):
        if self.transform_lab_dirty and not messagebox.askyesno(
            "Reload saved rules", "Discard all unsaved Transformation Lab changes and reload from fighter files?", parent=self.root
        ):
            return
        self.transform_lab_rules.clear()
        self.transform_lab_dirty.clear()
        self._update_transform_dirty_label()
        self.refresh_transform_line_tree()
        self.refresh_transformation_tree()

    def apply_quick_transform_chain(self):
        chain = [v.get().strip() for v in self.chain_vars if v.get().strip()]
        if len(chain) < 2:
            messagebox.showinfo("Quick Chain", "Choose at least two forms in sequence.", parent=self.root)
            return
        if len(set(chain)) != len(chain):
            messagebox.showerror("Quick Chain", "The same fighter cannot appear twice in one quick chain.", parent=self.root)
            return
        try:
            chance = float(self.chain_chance_var.get())
            heal = float(self.chain_heal_var.get())
            if not (0 <= chance <= 100) or not (0 <= heal <= 100):
                raise ValueError
        except ValueError:
            messagebox.showerror("Quick Chain", "Chance and heal must each be between 0 and 100.", parent=self.root)
            return
        label = (self.chain_label_var.get().strip() or "TRANSFORM!")[:28]
        replace = self.chain_replace_var.get()
        for source, target in zip(chain, chain[1:]):
            if not (ASSETS / source / "fighter.json").exists() or not (ASSETS / target / "fighter.json").exists():
                continue
            rule = {"target_pack": target, "chance": round(chance,2), "heal_percent": round(heal,2), "label": label}
            if replace:
                rules = [rule]
            else:
                rules = [dict(x) for x in self._lab_rules(source)]
                replaced = False
                for i, old in enumerate(rules):
                    if str(old.get("target_pack", "")) == target:
                        rules[i] = rule
                        replaced = True
                        break
                if not replaced:
                    rules.append(rule)
            self._set_lab_rules(source, rules)
        self.transform_lab_root_var.set(chain[0])
        self.transform_lab_source_var.set(chain[0])
        self.refresh_transform_line_tree()
        self.refresh_transformation_tree()
        self.set_status(f"Quick chain staged: {' → '.join(fighter_summary(x).get('name',x) for x in chain)}. Click SAVE ALL LINE CHANGES when ready.")

    def _parse_editor_values(self):
        try:
            stats = {
                "hp": float(self.hp_var.get()),
                "speed": float(self.speed_var.get()),
                "attack": float(self.attack_mult_var.get()),
                "defense": float(self.defense_var.get()),
            }
            if stats["hp"] <= 0 or stats["speed"] <= 0 or stats["attack"] <= 0 or stats["defense"] <= 0:
                raise ValueError("HP, speed, attack and defense must all be greater than zero.")
            scale = float(self.scale_var.get())
            ai = {
                "profile": self.ai_profile_var.get(),
                "retaliation": float(self.retaliation_var.get()),
                "evasion": float(self.evasion_var.get()),
            }
            return scale, stats, ai
        except ValueError as e:
            raise ValueError(f"Check the numeric fighter settings. {e}")

    def save_fighter(self):
        try:
            scale, stats, ai = self._parse_editor_values()
            name = self.name_var.get().strip()
            if not name:
                raise ValueError("Give the fighter a name.")

            target = self.current_folder
            if target is None:
                proposed = ASSETS / safe_pack_name(name)
                if proposed.exists():
                    if not messagebox.askyesno("Existing fighter", f"A fighter folder named '{proposed.name}' already exists. Overwrite its fighter definition?", parent=self.root):
                        return
                    target = proposed
            elif target.name != safe_pack_name(name):
                # Editing the display name does not silently rename the folder. That keeps roster refs stable.
                pass

            # If this fighter has staged Transformation Lab changes, do not overwrite them
            # with an older editor snapshot when saving stats/animations.
            transformations_to_save = self.transformation_data
            if target is not None and target.name in self.transform_lab_rules:
                transformations_to_save = [dict(x) for x in self.transform_lab_rules[target.name]]

            folder = save_fighter_pack(
                display_name=name,
                franchise=self.franchise_var.get(),
                stage_form=self.stage_var.get(),
                native_facing=self.facing_var.get(),
                allow_flip=self.flip_var.get(),
                scale=scale,
                stats=stats,
                ai=ai,
                animation_sources={k:v.get() for k,v in self.anim_paths.items()},
                attacks=self.attack_data,
                transformations=transformations_to_save,
                mugen_locks=self._current_mugen_locks(),
                existing_folder=target,
            )
            self.current_folder = folder
            self.transform_lab_rules[folder.name] = [dict(x) for x in transformations_to_save]
            self.transform_lab_dirty.discard(folder.name)
            self._update_transform_dirty_label()
            self.refresh_transform_lab_choices()

            if self.add_roster_var.get():
                roster = load_roster()
                names = [f.get("pack") for f in roster.get("fighters", []) if f.get("pack")]
                if folder.name not in names:
                    names.append(folder.name)
                # Don't fail fighter saving just because this is the very first fighter.
                if len(names) >= 2:
                    write_roster(names)

            self.refresh_library(select=folder.name)
            self.refresh_roster()
            self.set_status(f"Saved {name}. Fighter pack: assets/fighters/{folder.name}")
            messagebox.showinfo("Fighter saved", f"{name} is ready for Rumble Studio.\n\nSaved to:\n{folder}", parent=self.root)
        except Exception as e:
            messagebox.showerror("Save fighter", str(e), parent=self.root)

    def _ensure_roster_vars(self):
        packs = list_fighter_packs()
        live = set(packs)
        # Remove deleted fighters from the in-memory selection map.
        for pack in list(self.roster_vars):
            if pack not in live:
                del self.roster_vars[pack]

        if not self.roster_initialized:
            selected = {x.get("pack") for x in load_roster().get("fighters", []) if x.get("pack")}
        else:
            selected = {p for p, v in self.roster_vars.items() if v.get()}

        for pack in packs:
            if pack not in self.roster_vars:
                self.roster_vars[pack] = tk.BooleanVar(value=pack in selected)
        self.roster_initialized = True

    def refresh_roster(self):
        self._refresh_filter_choices()
        self._ensure_roster_vars()
        for child in self.roster_inner.winfo_children():
            child.destroy()

        infos = [
            i for i in all_fighter_summaries()
            if matches_filters(
                i, self.roster_search_var.get(),
                self.roster_franchise_filter_var.get(),
                self.roster_stage_filter_var.get(),
            )
        ]
        for row, info in enumerate(infos):
            pack = info["pack"]
            stage = f" • {info['stage']}" if info["stage"] else ""
            text = f"{info['name']}   [{info['franchise']}{stage}]"
            ttk.Checkbutton(self.roster_inner, text=text, variable=self.roster_vars[pack]).grid(
                row=row, column=0, sticky="w", padx=6, pady=3
            )

        selected_n = len(self.selected_roster())
        shown = len(infos)
        total = len(self.roster_vars)
        if shown == 0:
            ttk.Label(self.roster_inner, text="No fighters match these filters.", foreground="#666").grid(
                row=0, column=0, sticky="w", padx=8, pady=8
            )
        self.set_status(f"Roster: {selected_n} selected • showing {shown} of {total} fighters.")

    def selected_roster(self) -> List[str]:
        return [p for p, v in self.roster_vars.items() if v.get()]

    def select_all_roster(self):
        self._ensure_roster_vars()
        for v in self.roster_vars.values():
            v.set(True)
        self.refresh_roster()

    def clear_roster(self):
        self._ensure_roster_vars()
        for v in self.roster_vars.values():
            v.set(False)
        self.refresh_roster()

    def reset_roster_filters(self):
        self.roster_search_var.set("")
        self.roster_franchise_filter_var.set("All franchises")
        self.roster_stage_filter_var.set("All stages")
        self.refresh_roster()

    def select_dwc_rookies(self):
        self._ensure_roster_vars()
        selected = 0
        for pack, var in self.roster_vars.items():
            info = fighter_summary(pack)
            source = info["meta"].get("source", {}) if isinstance(info["meta"].get("source", {}), dict) else {}
            if source.get("format") == "Digimon World Championship GIF Set" and info["stage"].lower() == "rookie":
                var.set(True)
                selected += 1
            else:
                var.set(False)
        self.refresh_roster()
        self.set_status(f"Selected {selected} DWC Rookie fighters.")

    def pick_random_roster(self):
        self._ensure_roster_vars()
        try:
            count = int(self.random_count_var.get())
        except ValueError:
            messagebox.showerror("Random fighter selector", "Pick count must be a whole number.", parent=self.root)
            return
        if count < 2:
            messagebox.showerror("Random fighter selector", "Pick at least 2 fighters for a rumble.", parent=self.root)
            return

        franchise = self.random_franchise_var.get()
        stage = self.random_stage_var.get()
        pool = [
            i for i in all_fighter_summaries()
            if matches_filters(i, "", franchise, stage)
        ]
        if len(pool) < 2:
            messagebox.showinfo(
                "Random fighter selector",
                "That pool does not contain at least two fighters yet. Try another franchise/stage combination.",
                parent=self.root,
            )
            return
        actual = min(count, len(pool))
        if franchise == "MULTIVERSE (spread franchises)":
            # Do not let a huge single franchise (for example dozens of Digimon)
            # drown out smaller universes. Take one random fighter per franchise
            # in shuffled rounds until we reach the requested size.
            by_franchise = {}
            for info in pool:
                by_franchise.setdefault(info["franchise"], []).append(info)
            for group in by_franchise.values():
                random.shuffle(group)
            franchise_names = list(by_franchise)
            random.shuffle(franchise_names)
            chosen = []
            while len(chosen) < actual:
                progressed = False
                random.shuffle(franchise_names)
                for fname in franchise_names:
                    group = by_franchise[fname]
                    if group and len(chosen) < actual:
                        chosen.append(group.pop())
                        progressed = True
                if not progressed:
                    break
        else:
            chosen = random.sample(pool, actual)
        chosen_packs = {i["pack"] for i in chosen}
        for pack, var in self.roster_vars.items():
            var.set(pack in chosen_packs)
        self.refresh_roster()
        names = ", ".join(i["name"] for i in chosen[:8])
        if len(chosen) > 8:
            names += f", +{len(chosen)-8} more"
        scope = franchise if franchise != "MULTIVERSE (spread franchises)" else "the multiverse (spread across franchises)"
        stage_text = "" if stage == "Any stage" else f" / {stage}"
        self.set_status(f"Randomly selected {actual} fighters from {scope}{stage_text}: {names}")
        if count > len(pool):
            messagebox.showinfo(
                "Random fighter selector",
                f"You asked for {count}, but this pool only has {len(pool)} fighters, so I selected all {len(pool)}.",
                parent=self.root,
            )

    def save_roster_gui(self, quiet=False) -> bool:
        try:
            selected = self.selected_roster()
            write_roster(selected)
            self.set_status(f"Battle roster saved with {len(selected)} fighters.")
            if not quiet:
                messagebox.showinfo("Roster saved", f"Ready to rumble with {len(selected)} fighters.", parent=self.root)
            return True
        except Exception as e:
            messagebox.showerror("Battle roster", str(e), parent=self.root)
            return False

    def _spawn(self, args: List[str]):
        kwargs = {"cwd": str(ROOT)}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        subprocess.Popen([sys.executable, str(ROOT / "rumble.py"), *args], **kwargs)

    def launch_preview(self):
        if not self.save_roster_gui(quiet=True):
            return
        self._spawn(["--mode", "preview"])
        self.set_status("Battle preview launched in a new window.")

    def launch_render(self):
        if not self.save_roster_gui(quiet=True):
            return
        RENDERS.mkdir(exist_ok=True)
        self._spawn(["--mode", "render"])
        self.set_status("Rendering started. The finished MP4 will appear in the renders folder.")
        messagebox.showinfo("Render started", "Rumble Studio is rendering the battle.\n\nThe finished MP4 will appear in the renders folder.", parent=self.root)

    def open_fighter_folder(self):
        folder = self.current_folder if self.current_folder else ASSETS
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as e:
            messagebox.showerror("Open folder", str(e), parent=self.root)


def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    RENDERS.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    StudioApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
