from __future__ import annotations

import argparse
import json
import math
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageSequence

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets" / "fighters"
ROSTER_FILE = ROOT / "roster.json"
ENTRANCE_PROFILE_FILE = ROOT / "entrance_profiles.json"

W, H = 540, 960
FPS = 30
ARENA_TOP = 150
ARENA_BOTTOM = 900
DEFAULT_SCALE = 4.0
ATTACK_ANIMS = ("attack_1", "attack_2", "attack_3", "attack_4")
KO_VISIBLE_SECONDS = 0.72
KO_FADE_START = 0.38
TRANSFORM_LOCK_SECONDS = 0.72

# Universal AI presets. Fighter packs can select one and override individual values.
AI_PROFILES = {
    "auto": {"distance_weight": 1.0, "opportunism": 0.35, "retaliation": 0.55, "crowd_avoid": 0.65, "stickiness": 0.70, "evasion": 0.25, "kite": 0.35, "chaos": 0.30},
    "aggressive": {"distance_weight": 1.15, "opportunism": 0.35, "retaliation": 0.70, "crowd_avoid": 0.25, "stickiness": 0.78, "evasion": 0.16, "kite": 0.12, "chaos": 0.20},
    "brawler": {"distance_weight": 1.25, "opportunism": 0.25, "retaliation": 0.82, "crowd_avoid": 0.20, "stickiness": 0.85, "evasion": 0.12, "kite": 0.05, "chaos": 0.15},
    "ranged": {"distance_weight": 0.82, "opportunism": 0.42, "retaliation": 0.48, "crowd_avoid": 0.88, "stickiness": 0.62, "evasion": 0.52, "kite": 0.92, "chaos": 0.22},
    "defensive": {"distance_weight": 0.92, "opportunism": 0.28, "retaliation": 0.82, "crowd_avoid": 0.78, "stickiness": 0.74, "evasion": 0.48, "kite": 0.60, "chaos": 0.18},
    "opportunist": {"distance_weight": 0.88, "opportunism": 0.95, "retaliation": 0.42, "crowd_avoid": 0.72, "stickiness": 0.48, "evasion": 0.34, "kite": 0.45, "chaos": 0.28},
    "skirmisher": {"distance_weight": 0.92, "opportunism": 0.48, "retaliation": 0.50, "crowd_avoid": 0.86, "stickiness": 0.52, "evasion": 0.66, "kite": 0.72, "chaos": 0.34},
    "chaotic": {"distance_weight": 0.72, "opportunism": 0.35, "retaliation": 0.30, "crowd_avoid": 0.20, "stickiness": 0.20, "evasion": 0.30, "kite": 0.35, "chaos": 1.00},
}


def load_font(size: int):
    candidates = [
        Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for p in candidates:
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except Exception:
                pass
    return ImageFont.load_default()


FONT_TITLE = load_font(26)
FONT_NAME = load_font(14)
FONT_SMALL = load_font(12)
FONT_TINY = load_font(8)
FONT_ATTACK = load_font(13)
FONT_WIN = load_font(34)


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def parse_color(value, fallback=(255, 210, 80)) -> Tuple[int, int, int]:
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            return tuple(int(clamp(float(c), 0, 255)) for c in value[:3])
        except Exception:
            return fallback
    if isinstance(value, str):
        s = value.strip().lstrip("#")
        if len(s) == 6:
            try:
                return tuple(int(s[i:i+2], 16) for i in (0, 2, 4))
            except ValueError:
                pass
    return fallback


def load_entrance_profiles() -> dict:
    builtins = {
        "default": "Flash",
        "rules": [
            {"franchise": "Digimon", "stage": "", "style": "Digital Beam"},
            {"franchise": "Pokemon", "stage": "", "style": "Flash"},
            {"franchise": "Dragon Ball", "stage": "", "style": "Aura"},
            {"franchise": "Kingdom Hearts", "stage": "", "style": "Portal"},
        ],
    }
    try:
        data = json.loads(ENTRANCE_PROFILE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("rules", []), list):
            data.setdefault("default", "Flash")
            return data
    except Exception:
        pass
    return builtins


def resolve_entrance_style(pack: "FighterPack", profiles: dict, global_style: str) -> str:
    if global_style and global_style != "Auto / Profile":
        return global_style
    stage = str(pack.meta.get("stage", "") or pack.meta.get("source", {}).get("stage", "") or "").strip().lower()
    franchise = str(pack.franchise or "").strip().lower()
    rules = [x for x in profiles.get("rules", []) if isinstance(x, dict)]
    best = None
    best_score = -1
    for rule in rules:
        rf = str(rule.get("franchise", "") or "").strip().lower()
        rs = str(rule.get("stage", "") or "").strip().lower()
        if rf and rf != franchise:
            continue
        if rs and rs != stage:
            continue
        score = (2 if rf else 0) + (1 if rs else 0)
        if score > best_score:
            best = str(rule.get("style", "Flash") or "Flash")
            best_score = score
    return best or str(profiles.get("default", "Flash") or "Flash")


class SpriteAnim:
    """Loads either an animated GIF or a still image."""

    def __init__(self, path: Path):
        im = Image.open(path)
        self.frames = []
        self.durations = []
        for frame in ImageSequence.Iterator(im):
            rgba = frame.convert("RGBA")
            self.frames.append(rgba)
            ms = frame.info.get("duration", im.info.get("duration", 120))
            self.durations.append(max(0.06, ms / 1000.0))
        if not self.frames:
            self.frames = [im.convert("RGBA")]
            self.durations = [0.2]
        self.total = sum(self.durations)

    def frame_at(self, t: float) -> Image.Image:
        if len(self.frames) == 1:
            return self.frames[0]
        t = t % self.total
        acc = 0.0
        for frame, duration in zip(self.frames, self.durations):
            acc += duration
            if t <= acc:
                return frame
        return self.frames[-1]


@dataclass(frozen=True)
class AttackDef:
    name: str
    kind: str = "melee"  # melee / projectile / beam
    animation: str = "attack_1"
    damage: float = 12.0
    range: float = 52.0
    cooldown: float = 1.0
    knockback: float = 10.0
    projectile_speed: float = 280.0
    hit_radius: float = 20.0
    effect_color: Tuple[int, int, int] = (255, 210, 80)
    weight: float = 1.0
    min_range: float = 0.0

    @classmethod
    def from_json(cls, data: dict, index: int = 0) -> "AttackDef":
        kind = str(data.get("type", data.get("kind", "melee"))).lower()
        if kind not in ("melee", "projectile", "beam"):
            kind = "melee"
        return cls(
            name=str(data.get("name", f"Attack {index + 1}")),
            kind=kind,
            animation=str(data.get("animation", f"attack_{min(index + 1, 4)}")),
            damage=float(data.get("damage", 12)),
            range=float(data.get("range", 52 if kind == "melee" else 300)),
            cooldown=float(data.get("cooldown", 1.0)),
            knockback=float(data.get("knockback", 10)),
            projectile_speed=float(data.get("projectile_speed", 280)),
            hit_radius=float(data.get("hit_radius", 20)),
            effect_color=parse_color(data.get("effect_color")),
            weight=max(0.01, float(data.get("weight", 1.0))),
            min_range=max(0.0, float(data.get("min_range", 0.0))),
        )


class FighterPack:
    """Franchise-agnostic fighter definition loaded from assets/fighters/<pack>/fighter.json."""

    def __init__(self, folder: Path):
        self.folder = folder
        meta_path = folder / "fighter.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"Missing fighter definition: {meta_path}")
        self.meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.name = self.meta.get("name", folder.name)
        self.franchise = self.meta.get("franchise", "Custom")
        self.native_facing = str(self.meta.get("native_facing", "right")).lower()
        if self.native_facing not in ("left", "right"):
            self.native_facing = "right"
        self.allow_flip = bool(self.meta.get("allow_horizontal_flip", True))
        self.scale = float(self.meta.get("scale", DEFAULT_SCALE))
        self.stats = self.meta.get("stats", {})
        self.anims: Dict[str, SpriteAnim] = {}
        for key, filename in self.meta.get("animations", {}).items():
            p = folder / filename
            if p.exists():
                self.anims[key] = SpriteAnim(p)
        if not self.anims:
            raise RuntimeError(f"No usable sprite files found for {self.name}")

        raw_attacks = self.meta.get("attacks", [])
        self.attacks: List[AttackDef] = [AttackDef.from_json(a, i) for i, a in enumerate(raw_attacks)]
        if not self.attacks:
            self.attacks = [AttackDef(name="Basic Strike", kind="melee", animation="attack_1")]

        raw_ai = dict(self.meta.get("ai", {}))
        profile = str(raw_ai.pop("profile", "auto")).lower()
        if profile == "auto":
            ranged_weight = sum(a.weight for a in self.attacks if a.kind in ("projectile", "beam"))
            melee_weight = sum(a.weight for a in self.attacks if a.kind == "melee")
            if ranged_weight > melee_weight * 1.35:
                profile = "ranged"
            elif melee_weight > ranged_weight * 1.75:
                profile = "brawler"
            else:
                profile = "auto"
        self.ai_profile = profile if profile in AI_PROFILES else "auto"
        self.ai = dict(AI_PROFILES[self.ai_profile])
        for key, value in raw_ai.items():
            if key in self.ai:
                try:
                    self.ai[key] = clamp(float(value), 0.0, 1.5)
                except (TypeError, ValueError):
                    pass

        # v0.13: branching transformations triggered by eliminations.
        self.transformations: List[dict] = []
        for raw in self.meta.get("transformations", []):
            if not isinstance(raw, dict):
                continue
            target_pack = str(raw.get("target_pack", raw.get("target", ""))).strip()
            if not target_pack:
                continue
            try:
                chance = clamp(float(raw.get("chance", 100.0)), 0.0, 1000.0)
                heal_percent = clamp(float(raw.get("heal_percent", 0.0)), 0.0, 100.0)
            except (TypeError, ValueError):
                continue
            self.transformations.append({
                "target_pack": target_pack,
                "chance": chance,
                "heal_percent": heal_percent,
                "label": str(raw.get("label", "TRANSFORM!"))[:28] or "TRANSFORM!",
            })

    def anim(self, requested: str) -> SpriteAnim:
        # Missing states are deliberately allowed so a custom fighter can start with only one image.
        fallbacks = {
            "entrance": ("entrance", "idle", "cheer", "jump"),
            "walk": ("walk", "run", "idle"),
            "run": ("run", "walk", "idle"),
            "hit": ("hit", "idle", "walk"),
            "guard": ("guard", "idle"),
            "ko": ("ko", "hit", "idle"),
            "victory": ("victory", "cheer", "happy", "victory_jump", "jump", "idle"),
            "cheer": ("cheer", "happy", "victory_jump", "jump", "idle"),
            "happy": ("happy", "cheer", "victory_jump", "idle"),
            "victory_jump": ("victory_jump", "jump", "cheer", "happy", "idle"),
            "attack_1": ("attack_1", "attack_2", "attack_3", "attack_4", "idle"),
            "attack_2": ("attack_2", "attack_1", "attack_3", "attack_4", "idle"),
            "attack_3": ("attack_3", "attack_1", "attack_2", "attack_4", "idle"),
            "attack_4": ("attack_4", "attack_1", "attack_2", "attack_3", "idle"),
            "idle": ("idle", "walk", "run"),
        }
        for key in fallbacks.get(requested, (requested, "idle")):
            if key in self.anims:
                return self.anims[key]
        return next(iter(self.anims.values()))

    def victory_anim_name(self) -> str:
        for key in ("victory", "cheer", "happy", "victory_jump", "jump", "idle"):
            if key in self.anims:
                return key
        return next(iter(self.anims.keys()))


@dataclass
class Fighter:
    pack: FighterPack
    x: float
    y: float
    hp: float
    max_hp: float
    speed: float
    attack: float
    defense: float
    cooldown: float = 0.0
    anim_name: str = "idle"
    state_t: float = 0.0
    anim_t: float = 0.0
    target: int = -1
    alive: bool = True
    facing: int = -1  # -1 left, +1 right
    kills: int = 0
    ko_t: float = 0.0
    attack_label: str = ""
    attack_label_t: float = 0.0
    recent_attacker: int = -1
    recent_hit_t: float = 0.0
    decision_t: float = 0.0
    dodge_t: float = 0.0
    dodge_cooldown: float = 0.0
    dodge_vx: float = 0.0
    dodge_vy: float = 0.0
    strafe_sign: int = 1
    transform_lock_t: float = 0.0
    transform_count: int = 0
    spawn_index: int = 0
    spawn_time: float = 0.0
    entrance_style: str = "Flash"

    @property
    def name(self) -> str:
        return self.pack.name


@dataclass
class Projectile:
    owner: int
    attack: AttackDef
    x: float
    y: float
    vx: float
    vy: float
    life: float
    max_life: float
    prev_x: float = 0.0
    prev_y: float = 0.0


@dataclass
class BeamEffect:
    owner: int
    target: int
    attack: AttackDef
    x1: float
    y1: float
    x2: float
    y2: float
    age: float = 0.0
    life: float = 0.25


@dataclass
class ImpactEffect:
    x: float
    y: float
    color: Tuple[int, int, int]
    age: float = 0.0
    life: float = 0.30
    size: float = 16.0


@dataclass
class DamageEvent:
    x: float
    y: float
    value: int
    color: Tuple[int, int, int]
    age: float = 0.0
    life: float = 0.70
    critical: bool = False


@dataclass
class EliminationEvent:
    x: float
    y: float
    name: str
    age: float = 0.0
    life: float = 1.15
    final: bool = False


@dataclass
class TransformationEvent:
    fighter: int
    old_name: str
    new_name: str
    label: str
    color: Tuple[int, int, int] = (255, 222, 90)
    age: float = 0.0
    life: float = 1.10


class Rumble:
    def __init__(self, seed: int, roster: dict):
        self.rng = random.Random(seed)
        self.seed = seed
        self.t = 0.0
        self.fighters: List[Fighter] = []
        self.projectiles: List[Projectile] = []
        self.beams: List[BeamEffect] = []
        self.impacts: List[ImpactEffect] = []
        self.damage_events: List[DamageEvent] = []
        self.elimination_events: List[EliminationEvent] = []
        self.transformation_events: List[TransformationEvent] = []
        self.final_hit_t: float = 0.0

        studio = roster.get("studio", {})
        self.title = studio.get("title", "RUMBLE STUDIO")
        self.subtitle = studio.get("subtitle", "Battle Opening")

        opening = roster.get("opening", {}) if isinstance(roster.get("opening", {}), dict) else {}
        self.opening_enabled = bool(opening.get("enabled", True))
        self.opening_effect = str(opening.get("entrance_effect", "Auto / Profile"))
        self.entrance_delay = clamp(float(opening.get("entrance_delay", 0.18)), 0.05, 1.0)
        self.show_who_will_win = bool(opening.get("who_will_win", True))
        self.choose_hold = clamp(float(opening.get("choose_hold", 2.5)), 0.0, 10.0)
        self.show_countdown = bool(opening.get("countdown", True))
        self.formation = str(opening.get("formation", "Auto"))
        self.entrance_profiles = load_entrance_profiles()
        self.intro_blank = 0.28 if self.opening_enabled else 0.0
        self.entrance_effect_life = 0.72

        entries = list(roster["fighters"])
        positions = self._formation_positions(len(entries))
        for idx, entry in enumerate(entries):
            pack = FighterPack(ASSETS / entry["pack"])
            stats = dict(pack.stats)
            stats.update(entry.get("stats", {}))
            hp = float(stats.get("hp", 100))
            x, y = positions[idx]
            native = -1 if pack.native_facing == "left" else 1
            spawn_time = self.intro_blank + idx * self.entrance_delay if self.opening_enabled else 0.0
            self.fighters.append(
                Fighter(
                    pack=pack,
                    x=x,
                    y=y,
                    hp=hp,
                    max_hp=hp,
                    speed=48 * float(stats.get("speed", 1.0)),
                    attack=float(stats.get("attack", 1.0)),
                    defense=float(stats.get("defense", 1.0)),
                    facing=native,
                    decision_t=self.rng.uniform(0.15, 0.75),
                    strafe_sign=-1 if self.rng.random() < 0.5 else 1,
                    spawn_index=idx,
                    spawn_time=spawn_time,
                    entrance_style=resolve_entrance_style(pack, self.entrance_profiles, self.opening_effect),
                )
            )

        if self.opening_enabled and self.fighters:
            self.last_spawn_t = self.fighters[-1].spawn_time
            self.choose_start_t = self.last_spawn_t + self.entrance_effect_life
            self.choose_end_t = self.choose_start_t + (self.choose_hold if self.show_who_will_win else 0.0)
            self.countdown_start_t = self.choose_end_t
            self.countdown_duration = 2.10 if self.show_countdown else 0.0
            self.rumble_start_t = self.countdown_start_t + self.countdown_duration
            self.battle_start_t = self.rumble_start_t + 0.72
        else:
            self.last_spawn_t = self.choose_start_t = self.choose_end_t = 0.0
            self.countdown_start_t = self.countdown_duration = self.rumble_start_t = self.battle_start_t = 0.0

        self.winner: Optional[int] = None
        self.finished_t: Optional[float] = None

    def _formation_positions(self, n: int) -> List[Tuple[float, float]]:
        if n <= 0:
            return []
        style = self.formation
        if style == "Auto":
            style = "Circle" if n <= 12 else "Two Rings"
        if style == "Random":
            return [
                (self.rng.uniform(60, W - 60), self.rng.uniform(ARENA_TOP + 85, ARENA_BOTTOM - 85))
                for _ in range(n)
            ]

        cx = W / 2
        cy = (ARENA_TOP + ARENA_BOTTOM) / 2 + 18

        def ring(count: int, rx: float, ry: float, offset: float = 0.0):
            pts = []
            for j in range(count):
                angle = -math.pi / 2 + offset + (2 * math.pi * j / max(1, count))
                pts.append((cx + math.cos(angle) * rx, cy + math.sin(angle) * ry))
            return pts

        if style == "Circle" or n <= 12:
            return ring(n, 176 if n > 2 else 135, 255 if n > 2 else 210)

        outer_n = max(8, int(math.ceil(n * 0.60)))
        outer_n = min(outer_n, n)
        inner_n = n - outer_n
        pts = ring(outer_n, 188, 270)
        if inner_n:
            pts += ring(inner_n, 110, 158, math.pi / max(4, inner_n))
        return pts

    def living(self) -> List[int]:
        return [i for i, f in enumerate(self.fighters) if f.alive]

    def choose_target(self, i: int):
        f = self.fighters[i]
        choices = [j for j, g in enumerate(self.fighters) if j != i and g.alive]
        if not choices:
            f.target = -1
            return

        ai = f.pack.ai
        current = f.target if f.target in choices else -1
        attackers_per_target = {j: 0 for j in choices}
        for k, h in enumerate(self.fighters):
            if k != i and h.alive and h.target in attackers_per_target:
                attackers_per_target[h.target] += 1

        scored = []
        for j in choices:
            g = self.fighters[j]
            dist = math.hypot(g.x - f.x, g.y - f.y)
            hp_ratio = g.hp / max(1.0, g.max_hp)
            score = dist * ai["distance_weight"]
            # Opportunists prefer wounded targets; crowd avoidance spreads the fight out.
            score += hp_ratio * 155.0 * ai["opportunism"]
            score += attackers_per_target[j] * 62.0 * ai["crowd_avoid"]
            if j == current:
                score -= 90.0 * ai["stickiness"]
            if f.recent_hit_t > 0 and j == f.recent_attacker:
                score -= 185.0 * ai["retaliation"]
            score += self.rng.uniform(0.0, 72.0) * ai["chaos"]
            scored.append((score, j))
        scored.sort(key=lambda item: item[0])
        f.target = scored[0][1]

    @staticmethod
    def face_horizontal(f: Fighter, horizontal: float, deadzone: float = 0.04):
        """Turn only when horizontal motion/targeting is meaningful."""
        if horizontal > deadzone:
            f.facing = 1
        elif horizontal < -deadzone:
            f.facing = -1

    def available_attacks(self, f: Fighter, dist: float) -> List[AttackDef]:
        return [a for a in f.pack.attacks if a.min_range <= dist <= a.range]

    def select_attack(self, f: Fighter, dist: float) -> Optional[AttackDef]:
        choices = self.available_attacks(f, dist)
        if not choices:
            return None
        weights = [a.weight for a in choices]
        return self.rng.choices(choices, weights=weights, k=1)[0]

    def movement_goal_range(self, f: Fighter) -> float:
        ranged = [a.range for a in f.pack.attacks if a.kind in ("projectile", "beam")]
        if ranged:
            longest = max(ranged)
            kite = f.pack.ai["kite"]
            return clamp(longest * (0.52 + 0.24 * kite), 105.0, 270.0)
        return max(a.range for a in f.pack.attacks) * 0.82

    def minimum_safe_range(self, f: Fighter) -> float:
        ranged = [a for a in f.pack.attacks if a.kind in ("projectile", "beam")]
        if not ranged or f.pack.ai["kite"] < 0.25:
            return 0.0
        preferred = self.movement_goal_range(f)
        explicit = max((a.min_range for a in ranged), default=0.0)
        return max(72.0, explicit * 0.85, preferred * (0.34 + 0.18 * f.pack.ai["kite"]))

    def try_projectile_dodge(self, i: int) -> bool:
        f = self.fighters[i]
        if f.dodge_cooldown > 0 or f.state_t > 0 or f.pack.ai["evasion"] <= 0.05:
            return False
        nearest = None
        nearest_dist = 9999.0
        for p in self.projectiles:
            if p.owner == i:
                continue
            dist = math.hypot(f.x - p.x, (f.y - 22) - p.y)
            if dist < 105.0 and dist < nearest_dist:
                # Only react to projectiles moving generally toward the fighter.
                toward = (f.x - p.x) * p.vx + ((f.y - 22) - p.y) * p.vy
                if toward > 0:
                    nearest, nearest_dist = p, dist
        if nearest is None:
            return False
        chance = 0.28 + 0.52 * f.pack.ai["evasion"]
        if self.rng.random() > chance:
            f.dodge_cooldown = 0.45
            return False
        speed = max(1.0, math.hypot(nearest.vx, nearest.vy))
        px, py = nearest.vx / speed, nearest.vy / speed
        sx, sy = -py * f.strafe_sign, px * f.strafe_sign
        # Flip side if the first option aims out of the arena.
        tx, ty = f.x + sx * 70, f.y + sy * 70
        if tx < 42 or tx > W - 42 or ty < ARENA_TOP + 42 or ty > ARENA_BOTTOM - 42:
            sx, sy = -sx, -sy
            f.strafe_sign *= -1
        f.dodge_vx, f.dodge_vy = sx, sy
        f.dodge_t = 0.22
        f.dodge_cooldown = self.rng.uniform(1.05, 1.70)
        return True

    def separate_fighters(self):
        """Small collision/separation pass so fighters don't occupy the exact same spot."""
        min_dist = 36.0
        for i in range(len(self.fighters)):
            a = self.fighters[i]
            if not a.alive:
                continue
            for j in range(i + 1, len(self.fighters)):
                b = self.fighters[j]
                if not b.alive:
                    continue
                dx, dy = b.x - a.x, b.y - a.y
                dist = math.hypot(dx, dy)
                if dist < 0.001:
                    angle = self.rng.random() * math.tau
                    dx, dy, dist = math.cos(angle), math.sin(angle), 1.0
                if dist < min_dist:
                    push = (min_dist - dist) * 0.46
                    nx, ny = dx / dist, dy / dist
                    a.x -= nx * push
                    a.y -= ny * push
                    b.x += nx * push
                    b.y += ny * push
        for f in self.fighters:
            if f.alive:
                f.x = clamp(f.x, 35, W - 35)
                f.y = clamp(f.y, ARENA_TOP + 35, ARENA_BOTTOM - 35)

    def start_attack(self, owner_idx: int, target_idx: int, attack: AttackDef):
        f = self.fighters[owner_idx]
        g = self.fighters[target_idx]
        dx, dy = g.x - f.x, g.y - f.y
        dist = max(0.001, math.hypot(dx, dy))
        self.face_horizontal(f, dx)

        f.anim_name = attack.animation if attack.animation else "attack_1"
        f.state_t = 0.30 if attack.kind != "beam" else 0.38
        f.anim_t = 0.0
        f.cooldown = attack.cooldown * self.rng.uniform(0.92, 1.08)
        f.attack_label = attack.name
        f.attack_label_t = 0.65

        if attack.kind == "melee":
            self.apply_damage(owner_idx, target_idx, attack, g.x, g.y - 22)
        elif attack.kind == "projectile":
            nx, ny = dx / dist, dy / dist
            spawn_x = f.x + nx * 25
            spawn_y = f.y - 22 + ny * 5
            speed = max(80.0, attack.projectile_speed)
            life = attack.range / speed + 0.45
            self.projectiles.append(
                Projectile(
                    owner=owner_idx,
                    attack=attack,
                    x=spawn_x,
                    y=spawn_y,
                    vx=nx * speed,
                    vy=ny * speed,
                    life=life,
                    max_life=life,
                    prev_x=spawn_x,
                    prev_y=spawn_y,
                )
            )
        elif attack.kind == "beam":
            # Beam attacks resolve instantly but remain visible for a few frames.
            self.beams.append(
                BeamEffect(
                    owner=owner_idx,
                    target=target_idx,
                    attack=attack,
                    x1=f.x + (22 * f.facing),
                    y1=f.y - 25,
                    x2=g.x,
                    y2=g.y - 22,
                )
            )
            self.apply_damage(owner_idx, target_idx, attack, g.x, g.y - 22)

    def apply_damage(self, owner_idx: int, target_idx: int, attack: AttackDef, hit_x: float, hit_y: float):
        if target_idx < 0 or target_idx >= len(self.fighters):
            return
        owner = self.fighters[owner_idx]
        target = self.fighters[target_idx]
        if not target.alive:
            return

        raw = attack.damage * owner.attack * self.rng.uniform(0.88, 1.12)
        dmg = max(1.0, raw / max(0.55, target.defense))
        critical = self.rng.random() < 0.07
        if critical:
            dmg *= 1.55
        target.hp = max(0.0, target.hp - dmg)
        target.anim_name = "hit"
        target.state_t = 0.22
        target.anim_t = 0.0
        target.recent_attacker = owner_idx
        target.recent_hit_t = 2.4
        # Strong retaliation personalities can immediately notice the attacker.
        if target.alive and self.rng.random() < 0.34 * target.pack.ai["retaliation"]:
            target.target = owner_idx
            target.decision_t = self.rng.uniform(0.35, 0.75)

        dx, dy = target.x - owner.x, target.y - owner.y
        dist = max(0.001, math.hypot(dx, dy))
        kb = attack.knockback
        target.x += dx / dist * kb
        target.y += dy / dist * kb

        color = attack.effect_color
        self.impacts.append(ImpactEffect(hit_x, hit_y, color, size=18 if attack.kind != "beam" else 22))
        self.damage_events.append(
            DamageEvent(hit_x, hit_y, int(round(dmg)), color, critical=critical)
        )

        if target.hp <= 0:
            target.alive = False
            target.anim_name = "ko"
            target.state_t = 999.0
            target.anim_t = 0.0
            target.ko_t = 0.0
            owner.kills += 1
            remaining = [j for j, h in enumerate(self.fighters) if h.alive]
            final = len(remaining) == 1
            self.elimination_events.append(EliminationEvent(target.x, target.y - 28, target.name, final=final))
            if final:
                self.final_hit_t = 0.85
            for h in self.fighters:
                if h.target == target_idx:
                    h.target = -1
            # One elimination can trigger at most one configured branch.
            self.try_transform_after_elimination(owner_idx)

    def try_transform_after_elimination(self, fighter_idx: int) -> bool:
        """Attempt one evolution/power-up branch after this fighter scores a KO."""
        if fighter_idx < 0 or fighter_idx >= len(self.fighters):
            return False
        f = self.fighters[fighter_idx]
        if not f.alive:
            return False
        eligible = []
        for rule in getattr(f.pack, "transformations", []):
            target_pack = str(rule.get("target_pack", "")).strip()
            if not target_pack or target_pack == f.pack.folder.name:
                continue
            target_path = ASSETS / target_pack
            if not (target_path / "fighter.json").exists():
                continue
            chance = max(0.0, float(rule.get("chance", 0.0)))
            if chance > 0:
                eligible.append((rule, chance))
        if not eligible:
            return False

        total = sum(chance for _, chance in eligible)
        if total < 100.0 and self.rng.random() * 100.0 >= total:
            return False
        roll = self.rng.random() * total
        chosen = eligible[-1][0]
        cursor = 0.0
        for rule, chance in eligible:
            cursor += chance
            if roll <= cursor:
                chosen = rule
                break

        try:
            new_pack = FighterPack(ASSETS / str(chosen["target_pack"]))
        except Exception:
            return False

        old_name = f.name
        hp_ratio = clamp(f.hp / max(1.0, f.max_hp), 0.0, 1.0)
        stats = dict(new_pack.stats)
        new_max_hp = max(1.0, float(stats.get("hp", 100)))
        heal_bonus = clamp(float(chosen.get("heal_percent", 0.0)), 0.0, 100.0) / 100.0

        f.pack = new_pack
        f.max_hp = new_max_hp
        f.hp = min(new_max_hp, new_max_hp * hp_ratio + new_max_hp * heal_bonus)
        f.speed = 48 * float(stats.get("speed", 1.0))
        f.attack = float(stats.get("attack", 1.0))
        f.defense = float(stats.get("defense", 1.0))
        f.cooldown = max(f.cooldown, 0.35)
        f.anim_name = "cheer" if "cheer" in new_pack.anims else ("happy" if "happy" in new_pack.anims else "idle")
        f.anim_t = 0.0
        f.state_t = TRANSFORM_LOCK_SECONDS
        f.transform_lock_t = TRANSFORM_LOCK_SECONDS
        f.transform_count += 1
        f.decision_t = 0.0
        self.transformation_events.append(TransformationEvent(
            fighter=fighter_idx, old_name=old_name, new_name=f.name,
            label=str(chosen.get("label", "TRANSFORM!")) or "TRANSFORM!",
        ))
        return True

    def update_projectiles(self, dt: float):
        alive_projectiles: List[Projectile] = []
        for p in self.projectiles:
            p.life -= dt
            if p.life <= 0:
                continue
            p.prev_x, p.prev_y = p.x, p.y
            p.x += p.vx * dt
            p.y += p.vy * dt
            if p.x < 10 or p.x > W - 10 or p.y < ARENA_TOP + 10 or p.y > ARENA_BOTTOM - 10:
                continue

            hit_idx = None
            best_d = 1e9
            for j, g in enumerate(self.fighters):
                if j == p.owner or not g.alive:
                    continue
                d = math.hypot(g.x - p.x, (g.y - 22) - p.y)
                if d <= p.attack.hit_radius and d < best_d:
                    hit_idx, best_d = j, d
            if hit_idx is not None:
                self.apply_damage(p.owner, hit_idx, p.attack, p.x, p.y)
                continue
            alive_projectiles.append(p)
        self.projectiles = alive_projectiles

    def update_effects(self, dt: float):
        for b in self.beams:
            b.age += dt
        self.beams = [b for b in self.beams if b.age < b.life]
        for e in self.impacts:
            e.age += dt
        self.impacts = [e for e in self.impacts if e.age < e.life]
        for e in self.damage_events:
            e.age += dt
        self.damage_events = [e for e in self.damage_events if e.age < e.life]
        for e in self.elimination_events:
            e.age += dt
        self.elimination_events = [e for e in self.elimination_events if e.age < e.life]
        for e in self.transformation_events:
            e.age += dt
        self.transformation_events = [e for e in self.transformation_events if e.age < e.life]
        self.final_hit_t = max(0.0, self.final_hit_t - dt)

    def update(self, dt: float):
        self.t += dt

        if self.opening_enabled and self.t < self.battle_start_t:
            # Entrance/choose/countdown phases are presentation-only: everyone is frozen
            # in the fair starting formation until RUMBLE! releases the AI.
            for f in self.fighters:
                if self.t < f.spawn_time:
                    continue
                f.anim_t += dt
                age = self.t - f.spawn_time
                f.anim_name = "entrance" if age < self.entrance_effect_life else "idle"
            return

        self.update_projectiles(dt)
        self.update_effects(dt)
        # KO sprites get a brief readable defeat beat, then leave the battlefield.
        # We keep the Fighter object for the HUD/results, but stop drawing the corpse.
        for f in self.fighters:
            if not f.alive:
                f.ko_t += dt

        living = self.living()
        if len(living) <= 1:
            if self.winner is None and living:
                self.winner = living[0]
                self.finished_t = self.t
                winner = self.fighters[self.winner]
                winner.anim_name = winner.pack.victory_anim_name()
                winner.anim_t = 0.0
                winner.state_t = 999.0
            # Keep GIFs animating through the victory hold instead of freezing the last frame.
            for f in self.fighters:
                f.anim_t += dt
            return

        for i, f in enumerate(self.fighters):
            f.anim_t += dt
            f.cooldown = max(0.0, f.cooldown - dt)
            f.state_t = max(0.0, f.state_t - dt)
            f.attack_label_t = max(0.0, f.attack_label_t - dt)
            f.recent_hit_t = max(0.0, f.recent_hit_t - dt)
            f.decision_t = max(0.0, f.decision_t - dt)
            f.dodge_t = max(0.0, f.dodge_t - dt)
            f.dodge_cooldown = max(0.0, f.dodge_cooldown - dt)
            f.transform_lock_t = max(0.0, f.transform_lock_t - dt)
            if not f.alive:
                continue

            if f.recent_hit_t <= 0:
                f.recent_attacker = -1

            invalid_target = f.target < 0 or f.target >= len(self.fighters) or not self.fighters[f.target].alive
            if invalid_target or f.decision_t <= 0:
                self.choose_target(i)
                f.decision_t = self.rng.uniform(0.55, 1.25)
            if f.target < 0:
                f.anim_name = "idle"
                continue

            # Don't interrupt attack/hit poses, but once free, high-evasion fighters can dodge shots.
            if f.state_t > 0:
                continue
            self.try_projectile_dodge(i)
            if f.dodge_t > 0:
                f.x += f.dodge_vx * f.speed * 1.85 * dt
                f.y += f.dodge_vy * f.speed * 1.85 * dt
                self.face_horizontal(f, f.dodge_vx)
                f.anim_name = "run"
                f.x = clamp(f.x, 35, W - 35)
                f.y = clamp(f.y, ARENA_TOP + 35, ARENA_BOTTOM - 35)
                continue

            g = self.fighters[f.target]
            dx, dy = g.x - f.x, g.y - f.y
            dist = max(0.001, math.hypot(dx, dy))

            attack = self.select_attack(f, dist) if f.cooldown <= 0 else None
            if attack is not None:
                self.start_attack(i, f.target, attack)
                continue

            goal = self.movement_goal_range(f)
            safe = self.minimum_safe_range(f)
            available = self.available_attacks(f, dist)

            # Ranged/skirmisher fighters actively create space instead of letting melee fighters sit on top of them.
            if safe > 0 and dist < safe:
                nx, ny = -dx / dist, -dy / dist
                sx, sy = -dy / dist * f.strafe_sign, dx / dist * f.strafe_sign
                blend = 0.72 + 0.18 * f.pack.ai["kite"]
                vx = nx * blend + sx * (1.0 - blend)
                vy = ny * blend + sy * (1.0 - blend)
                norm = max(0.001, math.hypot(vx, vy))
                vx, vy = vx / norm, vy / norm
                f.x += vx * f.speed * (1.05 + 0.25 * f.pack.ai["kite"]) * dt
                f.y += vy * f.speed * (1.05 + 0.25 * f.pack.ai["kite"]) * dt
                # Face the direction of travel: no accidental moonwalking while retreating.
                self.face_horizontal(f, vx)
                f.anim_name = "run"
            elif dist > goal or not available:
                wob = math.sin(self.t * 1.9 + i * 1.73) * (0.06 + 0.08 * f.pack.ai["chaos"])
                nx, ny = dx / dist, dy / dist
                vx = nx * math.cos(wob) - ny * math.sin(wob)
                vy = nx * math.sin(wob) + ny * math.cos(wob)
                f.x += vx * f.speed * dt
                f.y += vy * f.speed * dt
                self.face_horizontal(f, vx)
                f.anim_name = "run" if dist > 145 else "walk"
            else:
                # At fighting distance, reposition with a visible side-step rather than drifting backward.
                self.face_horizontal(f, dx)
                f.anim_name = "idle"
                if f.cooldown > 0 and dist > 90 and self.rng.random() < (0.18 + 0.18 * f.pack.ai["evasion"]):
                    sx, sy = (-dy / dist) * f.strafe_sign, (dx / dist) * f.strafe_sign
                    f.x += sx * f.speed * 0.24 * dt
                    f.y += sy * f.speed * 0.24 * dt
                    self.face_horizontal(f, sx)
                    f.anim_name = "walk"

            f.x = clamp(f.x, 35, W - 35)
            f.y = clamp(f.y, ARENA_TOP + 35, ARENA_BOTTOM - 35)

        self.separate_fighters()

    def draw_arena(self, img: Image.Image):
        d = ImageDraw.Draw(img)
        d.rounded_rectangle(
            (18, ARENA_TOP, W - 18, ARENA_BOTTOM),
            20,
            fill=(61, 98, 67),
            outline=(118, 160, 116),
            width=3,
        )
        for x in range(45, W - 20, 55):
            d.line((x, ARENA_TOP + 5, x, ARENA_BOTTOM - 5), fill=(70, 112, 75), width=1)
        for y in range(ARENA_TOP + 35, ARENA_BOTTOM, 55):
            d.line((22, y, W - 22, y), fill=(70, 112, 75), width=1)

        d.text((W // 2, 20), self.title, font=FONT_TITLE, anchor="ma", fill=(245, 245, 245))
        d.text(
            (W // 2, 55),
            f"Seed {self.seed}  •  {self.subtitle}",
            font=FONT_SMALL,
            anchor="ma",
            fill=(190, 205, 215),
        )

        # Roster HUD: normal roomy layout for small fights, compact 8-column grid
        # for giant rumbles (e.g. all 38 bundled DWC Rookies) so the HUD never
        # spills down over the arena.
        if len(self.fighters) <= 12:
            cols, y0, row_h, name_font, name_chars, bar_h = 4, 82, 30, FONT_SMALL, 10, 7
            colw = W // cols
            xpad, bar_gap = 10, 15
        else:
            cols, y0, row_h, name_font, name_chars, bar_h = 8, 73, 15, FONT_TINY, 7, 3
            colw = W // cols
            xpad, bar_gap = 4, 9
        for i, f in enumerate(self.fighters):
            col, row = i % cols, i // cols
            x, y = xpad + col * colw, y0 + row * row_h
            label = f.name[:name_chars]
            d.text((x, y), label, font=name_font, fill=(240, 240, 240) if f.alive else (120, 120, 120))
            bx, by, bw = x, y + bar_gap, colw - xpad * 2
            d.rectangle((bx, by, bx + bw, by + bar_h), fill=(45, 45, 50))
            ratio = f.hp / f.max_hp
            fill = (55, 200, 90) if ratio > .5 else ((235, 180, 50) if ratio > .25 else (220, 65, 65))
            if ratio > 0:
                d.rectangle((bx, by, bx + bw * ratio, by + bar_h), fill=fill)
            if not f.alive:
                strike_w = min(bw, 45 if len(self.fighters) > 12 else 70)
                d.line((x, y + max(3, bar_gap // 2), x + strike_w, y + max(3, bar_gap // 2)), fill=(210, 70, 70), width=1 if len(self.fighters) > 12 else 2)

    def draw_projectiles(self, img: Image.Image):
        d = ImageDraw.Draw(img)
        for p in self.projectiles:
            c = p.attack.effect_color
            # Trail + glow + core. Geometric effects keep v0.4 franchise-agnostic.
            tx = p.x - p.vx * 0.05
            ty = p.y - p.vy * 0.05
            d.line((tx, ty, p.x, p.y), fill=tuple(max(0, int(v * 0.65)) for v in c), width=6)
            d.ellipse((p.x - 9, p.y - 9, p.x + 9, p.y + 9), fill=tuple(min(255, int(v * 0.65 + 90)) for v in c))
            d.ellipse((p.x - 4, p.y - 4, p.x + 4, p.y + 4), fill=(255, 255, 245))

    def draw_beams(self, img: Image.Image):
        d = ImageDraw.Draw(img)
        for b in self.beams:
            fade = max(0.0, 1.0 - b.age / b.life)
            c = tuple(int(v * (0.55 + 0.45 * fade)) for v in b.attack.effect_color)
            width = max(2, int(12 * fade))
            d.line((b.x1, b.y1, b.x2, b.y2), fill=c, width=width)
            d.line((b.x1, b.y1, b.x2, b.y2), fill=(250, 250, 250), width=max(1, width // 3))

    def draw_fighter(self, img: Image.Image, f: Fighter):
        if self.opening_enabled and self.t < f.spawn_time:
            return
        if not f.alive and f.ko_t >= KO_VISIBLE_SECONDS:
            return
        anim = f.pack.anim(f.anim_name)
        fr = anim.frame_at(f.anim_t)
        native = -1 if f.pack.native_facing == "left" else 1
        if f.pack.allow_flip and f.facing != native:
            fr = fr.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if len(self.fighters) > 24:
            crowd_scale = 0.68
        elif len(self.fighters) > 16:
            crowd_scale = 0.82
        else:
            crowd_scale = 1.0
        scale = f.pack.scale * crowd_scale
        fr = fr.resize(
            (max(1, int(fr.width * scale)), max(1, int(fr.height * scale))),
            Image.Resampling.NEAREST,
        )
        if not f.alive and f.ko_t > KO_FADE_START:
            remain = max(0.0, (KO_VISIBLE_SECONDS - f.ko_t) / max(0.001, KO_VISIBLE_SECONDS - KO_FADE_START))
            alpha = fr.getchannel("A").point(lambda a: int(a * remain))
            fr.putalpha(alpha)
        px = int(f.x - fr.width / 2)
        py = int(f.y - fr.height + 12)
        img.paste(fr, (px, py), fr)
        d = ImageDraw.Draw(img)
        if not f.alive:
            return

        crowded = len(self.fighters) > 16
        label_font = FONT_TINY if crowded else FONT_ATTACK
        name_font = FONT_TINY if crowded else FONT_NAME
        if f.attack_label_t > 0 and f.alive:
            alpha = min(1.0, f.attack_label_t / 0.25)
            c = tuple(int(245 * alpha) for _ in range(3))
            d.text((int(f.x), py - (22 if crowded else 34)), f.attack_label[:14] if crowded else f.attack_label, font=label_font, anchor="ma", fill=c)

        name_y = py - (12 if crowded else 19)
        d.text((int(f.x), name_y), f.name[:10] if crowded else f.name, font=name_font, anchor="ma", fill=(255, 255, 255))
        bw = 46 if crowded else 64
        bar_h = 4 if crowded else 6
        bx, by = int(f.x - bw / 2), py - (5 if crowded else 7)
        d.rounded_rectangle((bx, by, bx + bw, by + bar_h), 2, fill=(35, 35, 40))
        ratio = f.hp / f.max_hp
        color = (60, 205, 90) if ratio > .5 else ((240, 180, 40) if ratio > .25 else (225, 60, 60))
        if ratio > 0:
            d.rounded_rectangle((bx, by, bx + bw * ratio, by + bar_h), 2, fill=color)

    def draw_impacts_and_damage(self, img: Image.Image):
        d = ImageDraw.Draw(img)
        for e in self.impacts:
            p = e.age / e.life
            r = e.size * (0.55 + p * 1.1)
            c = tuple(int(v * (1.0 - 0.45 * p)) for v in e.color)
            d.ellipse((e.x - r, e.y - r, e.x + r, e.y + r), outline=c, width=max(1, int(4 * (1 - p) + 1)))
            if p < 0.55:
                d.line((e.x - r, e.y, e.x + r, e.y), fill=c, width=2)
                d.line((e.x, e.y - r, e.x, e.y + r), fill=c, width=2)

        for e in self.damage_events:
            p = e.age / e.life
            yy = e.y - 38 - p * 42
            prefix = "CRIT " if e.critical else ""
            text = f"{prefix}-{e.value}"
            c = (255, 245, 140) if e.critical else e.color
            d.text((int(e.x), int(yy)), text, font=FONT_NAME, anchor="mm", fill=c)

        for e in self.elimination_events:
            p = e.age / e.life
            yy = e.y - 36 - p * 26
            text = "FINAL KO!" if e.final else "KO!"
            c = (255, 220, 70) if e.final else (255, 100, 95)
            d.text((int(e.x), int(yy)), text, font=FONT_TITLE if e.final else FONT_NAME, anchor="mm", fill=c)

        for e in self.transformation_events:
            if e.fighter < 0 or e.fighter >= len(self.fighters):
                continue
            f = self.fighters[e.fighter]
            p = clamp(e.age / max(0.001, e.life), 0.0, 1.0)
            radius = 36 + 45 * p
            glow = tuple(int(c * (1.0 - 0.45 * p)) for c in e.color)
            d.ellipse((f.x - radius, f.y - 62 - radius * 0.35, f.x + radius, f.y - 62 + radius * 0.35), outline=glow, width=max(1, int(5 * (1-p) + 1)))
            title_y = f.y - 112 - p * 14
            d.text((int(f.x), int(title_y)), e.label.upper(), font=FONT_TITLE if len(self.fighters) <= 16 else FONT_NAME, anchor="mm", fill=e.color)
            if len(self.fighters) <= 16:
                d.text((int(f.x), int(title_y + 25)), f"{e.old_name} → {e.new_name}", font=FONT_SMALL, anchor="mm", fill=(245, 245, 245))

        if self.final_hit_t > 0:
            strength = min(1.0, self.final_hit_t / 0.28)
            c = (255, int(220 * strength + 25), 70)
            d.text((W // 2, ARENA_TOP + 30), "FINAL HIT!", font=FONT_TITLE, anchor="ma", fill=c)

    def draw_entrance_effects(self, img: Image.Image):
        if not self.opening_enabled or self.t >= self.choose_start_t:
            return
        d = ImageDraw.Draw(img)
        for f in self.fighters:
            age = self.t - f.spawn_time
            if age < 0 or age > self.entrance_effect_life:
                continue
            p = clamp(age / self.entrance_effect_life, 0.0, 1.0)
            x, y = int(f.x), int(f.y)
            style = f.entrance_style
            if style == "None":
                continue
            if style == "Digital Beam":
                fade = 1.0 - p
                c = (90, 220, 255)
                width = max(2, int(18 * fade))
                d.line((x, ARENA_TOP + 12, x, y + 10), fill=c, width=width)
                d.line((x, ARENA_TOP + 12, x, y + 10), fill=(235, 255, 255), width=max(1, width // 4))
                for k in range(7):
                    yy = y - 120 + ((k * 37 + f.spawn_index * 19 + int(age * 180)) % 145)
                    xx = x + ((k * 23 + f.spawn_index * 11) % 34) - 17
                    r = 2 + (k % 3)
                    d.rectangle((xx-r, yy-r, xx+r, yy+r), fill=c)
            elif style == "Aura":
                c = (255, 226, 80)
                r = int(28 + 42 * p)
                d.ellipse((x-r, y-70-r//2, x+r, y-70+r//2), outline=c, width=max(1, int(5*(1-p)+1)))
                d.line((x-18, y-110, x-4, y-150), fill=c, width=3)
                d.line((x+16, y-105, x+30, y-145), fill=c, width=3)
            elif style == "Portal":
                c = (130, 105, 255)
                r = int(18 + 58 * p)
                d.ellipse((x-r, y-68-r, x+r, y-68+r), outline=c, width=max(2, int(7*(1-p)+2)))
                d.ellipse((x-r//2, y-68-r//2, x+r//2, y-68+r//2), outline=(220, 205, 255), width=2)
            elif style == "Teleport":
                c = (210, 235, 255)
                for k in range(6):
                    dx = (k - 3) * 8
                    d.line((x+dx, y-145+int(70*p), x+dx, y-25), fill=c, width=2)
            else:  # Flash
                c = (245, 250, 255)
                r = int(12 + 55 * p)
                d.ellipse((x-r, y-70-r, x+r, y-70+r), outline=c, width=max(1, int(6*(1-p)+1)))
                d.line((x-r-8, y-70, x+r+8, y-70), fill=c, width=2)
                d.line((x, y-70-r-8, x, y-70+r+8), fill=c, width=2)

    def draw_intro_overlay(self, img: Image.Image):
        if not self.opening_enabled or self.t >= self.battle_start_t:
            return
        d = ImageDraw.Draw(img)
        if self.show_who_will_win and self.choose_start_t <= self.t < self.choose_end_t:
            d.rounded_rectangle((72, 392, W-72, 520), 22, fill=(10, 14, 22), outline=(245, 210, 70), width=3)
            d.text((W//2, 425), "WHO WILL WIN?", font=FONT_WIN, anchor="ma", fill=(255, 235, 110))
            d.text((W//2, 478), "CHOOSE YOUR FIGHTER", font=FONT_NAME, anchor="ma", fill=(245, 245, 245))
            return
        if self.show_countdown and self.countdown_start_t <= self.t < self.rumble_start_t:
            elapsed = self.t - self.countdown_start_t
            step = int(elapsed / 0.70)
            number = max(1, 3 - step)
            d.text((W//2, 465), str(number), font=load_font(86), anchor="mm", fill=(255, 238, 105), stroke_width=4, stroke_fill=(20, 24, 32))
            return
        if self.rumble_start_t <= self.t < self.battle_start_t:
            d.text((W//2, 465), "RUMBLE!", font=load_font(58), anchor="mm", fill=(255, 225, 75), stroke_width=4, stroke_fill=(20, 24, 32))

    def draw_victory_finish(self, img: Image.Image):
        if self.winner is None or self.finished_t is None:
            return
        elapsed = max(0.0, self.t - self.finished_t)
        f = self.fighters[self.winner]
        d = ImageDraw.Draw(img)

        # Let the last KO breathe for a fraction of a second before the result card arrives.
        if elapsed < 0.45:
            return

        # Dim the finished battlefield so the champion presentation is unmistakable.
        veil = Image.new("RGBA", img.size, (0, 0, 0, 0))
        vd = ImageDraw.Draw(veil)
        vd.rounded_rectangle((18, ARENA_TOP, W - 18, ARENA_BOTTOM), 20, fill=(5, 8, 12, 112))
        img.paste(veil, (0, 0), veil)
        d = ImageDraw.Draw(img)

        card_top, card_bottom = 545, 708
        d.rounded_rectangle((42, card_top, W - 42, card_bottom), 24, fill=(14, 18, 25), outline=(250, 211, 64), width=4)

        # Give the champion a clean showcase area above the card, covering old labels/bodies behind it.
        d.ellipse((W // 2 - 96, card_top - 166, W // 2 + 96, card_top + 12), fill=(17, 22, 30), outline=(250, 211, 64), width=3)

        # Animated winner on a little gold podium ABOVE the card so the champion is never hidden by UI.
        anim_name = f.pack.victory_anim_name()
        anim = f.pack.anim(anim_name)
        fr = anim.frame_at(elapsed - 0.45)
        native = -1 if f.pack.native_facing == "left" else 1
        # Face inward/right for presentation if flipping is allowed.
        if f.pack.allow_flip and native < 0:
            fr = fr.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        presentation_scale = f.pack.scale * 1.32
        fr = fr.resize((max(1, int(fr.width * presentation_scale)), max(1, int(fr.height * presentation_scale))), Image.Resampling.NEAREST)
        bob = int(math.sin((elapsed - 0.45) * 7.0) * 5.0)
        baseline = card_top + 7 + bob
        px = int(W / 2 - fr.width / 2)
        py = int(baseline - fr.height)
        # Shadow/podium makes the presentation read clearly even on busy arenas.
        d.ellipse((W // 2 - 55, card_top - 10, W // 2 + 55, card_top + 10), fill=(90, 72, 24), outline=(250, 211, 64), width=3)
        img.paste(fr, (px, py), fr)

        # Simple deterministic celebratory sparkles in the otherwise empty side space.
        for n in range(8):
            phase = (elapsed * 1.8 + n * 0.73 + self.seed * 0.001) % 6.28
            side = -1 if n % 2 == 0 else 1
            x = W // 2 + side * (105 + (n % 4) * 25) + int(math.sin(phase) * 12)
            y = card_top + 35 + (n % 4) * 27 + int(math.cos(phase * 1.3) * 8)
            r = 3 + (n % 3)
            d.line((x - r * 2, y, x + r * 2, y), fill=(250, 211, 64), width=2)
            d.line((x, y - r * 2, x, y + r * 2), fill=(250, 211, 64), width=2)

        d.text((W // 2, 570), "WINNER", font=FONT_TITLE, anchor="ma", fill=(250, 211, 64))
        d.text((W // 2, 618), f.name.upper(), font=FONT_WIN, anchor="ma", fill=(255, 255, 255))
        d.text((W // 2, 670), f"{f.kills} eliminations  •  {int(round(f.hp))} HP left", font=FONT_NAME, anchor="ma", fill=(210, 220, 230))

    def draw(self) -> Image.Image:
        img = Image.new("RGB", (W, H), (24, 30, 38))
        self.draw_arena(img)
        self.draw_entrance_effects(img)

        # Effects that should sit behind sprites.
        self.draw_beams(img)
        self.draw_projectiles(img)

        for i in sorted(range(len(self.fighters)), key=lambda j: self.fighters[j].y):
            self.draw_fighter(img, self.fighters[i])

        self.draw_impacts_and_damage(img)
        self.draw_intro_overlay(img)
        self.draw_victory_finish(img)
        return img


def load_roster():
    return json.loads(ROSTER_FILE.read_text(encoding="utf-8"))


def run_render(seed: int, output: Path, max_seconds: float = 55.0):
    sim = Rumble(seed, load_roster())
    output.parent.mkdir(parents=True, exist_ok=True)
    writer = imageio_ffmpeg.write_frames(
        str(output),
        (W, H),
        fps=FPS,
        codec="libx264",
        pix_fmt_in="rgb24",
        pix_fmt_out="yuv420p",
        quality=7,
        macro_block_size=2,
    )
    writer.send(None)
    dt = 1 / FPS
    frames = 0
    writer.send(np.asarray(sim.draw(), dtype=np.uint8))
    frames += 1
    while sim.t < max_seconds:
        sim.update(dt)
        writer.send(np.asarray(sim.draw(), dtype=np.uint8))
        frames += 1
        if sim.winner is not None and sim.finished_t is not None and sim.t - sim.finished_t >= 4.2:
            break
    writer.close()
    winner = sim.fighters[sim.winner].name if sim.winner is not None else "No winner (time limit)"
    print(f"Created: {output}")
    print(f"Winner: {winner}")
    print(f"Length: {frames / FPS:.1f}s")


def run_preview(seed: int):
    import tkinter as tk
    from PIL import ImageTk

    sim = Rumble(seed, load_roster())
    root = tk.Tk()
    root.title(f"Rumble Studio v0.15 Preview - seed {seed}")
    label = tk.Label(root)
    label.pack()
    last = time.perf_counter()

    def tick():
        nonlocal last
        now = time.perf_counter()
        dt = min(0.05, now - last)
        last = now
        sim.update(dt)
        frame = sim.draw().resize((405, 720), Image.Resampling.NEAREST)
        photo = ImageTk.PhotoImage(frame)
        label.configure(image=photo)
        label.image = photo
        root.after(16, tick)

    tick()
    root.mainloop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Rumble Studio v0.15")
    ap.add_argument("--mode", choices=["preview", "render"], default="preview")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--output", default="")
    ap.add_argument("--max-seconds", type=float, default=55)
    a = ap.parse_args()
    seed = a.seed if a.seed is not None else random.randint(100000, 999999)
    if a.mode == "preview":
        run_preview(seed)
    else:
        out = Path(a.output) if a.output else ROOT / "renders" / f"rumble_{seed}.mp4"
        run_render(seed, out, a.max_seconds)
