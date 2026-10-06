from __future__ import annotations

"""MUGEN -> Rumble Studio importer.

v0.14 expands the importer to understand both classic SFF v1 and MUGEN 1.x /
IKEMEN SFF v2 sprite archives. ZIP archives are handled natively. RAR archives
are unpacked by Windows' tar/libarchive when available, or by 7-Zip/WinRAR if
the user has either installed. ZIP archives are handled natively. RAR archives
are unpacked by Windows' tar/libarchive when available, or by 7-Zip/WinRAR if the
user has either installed.

The importer reads the character DEF, extracts SFF v1 sprites, parses AIR actions,
renders AIR actions into transparent GIFs, maps standard MUGEN action numbers to
Rumble animation slots, and generates a normal Rumble fighter.json.
"""

import configparser
import io
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from PIL import Image, ImageOps


class MugenImportError(RuntimeError):
    pass


@dataclass
class AirFrame:
    group: int
    image: int
    x: int = 0
    y: int = 0
    ticks: int = 1
    flip_h: bool = False
    flip_v: bool = False


@dataclass
class AirAction:
    number: int
    frames: List[AirFrame]
    comment: str = ""


@dataclass
class SffSprite:
    group: int
    image: int
    axis_x: int
    axis_y: int
    rgba: Image.Image
    linked_from: Optional[int] = None


@dataclass
class MugenCharacterInfo:
    def_file: Path
    name: str
    display_name: str
    author: str
    sff_file: Path
    air_file: Path
    snd_file: Optional[Path]
    cmd_file: Optional[Path]
    cns_file: Optional[Path]
    palette_file: Optional[Path]


def _decode_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1", errors="replace")


def _strip_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        return v[1:-1]
    return v


def parse_def(path: Path) -> Tuple[dict, Dict[str, str]]:
    """Tolerant MUGEN DEF parser returning info and files dictionaries."""
    section = ""
    sections: Dict[str, Dict[str, str]] = {}
    for raw in _decode_text(path).splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and "]" in line:
            section = line[1:line.index("]")].strip().lower()
            sections.setdefault(section, {})
            continue
        if "=" in line and section:
            k, v = line.split("=", 1)
            sections.setdefault(section, {})[k.strip().lower()] = _strip_quotes(v)
    return sections.get("info", {}), sections.get("files", {})


def _resolve_case_insensitive(base: Path, rel: str) -> Optional[Path]:
    if not rel:
        return None
    rel = _strip_quotes(rel).replace("\\", os.sep).replace("/", os.sep)
    p = (base / rel)
    if p.exists():
        return p
    # MUGEN character packs are often made on Windows and have case-mismatched refs.
    cur = base
    for part in Path(rel).parts:
        if part in (".", ""):
            continue
        if part == "..":
            cur = cur.parent
            continue
        if not cur.exists() or not cur.is_dir():
            return None
        match = next((x for x in cur.iterdir() if x.name.lower() == part.lower()), None)
        if match is None:
            return None
        cur = match
    return cur if cur.exists() else None


def inspect_character(def_file: Path) -> MugenCharacterInfo:
    info, files = parse_def(def_file)
    base = def_file.parent

    def f(*keys: str) -> Optional[Path]:
        for key in keys:
            if key in files and files[key]:
                hit = _resolve_case_insensitive(base, files[key])
                if hit:
                    return hit
        return None

    # Common DEF names are sprite/anim/sound/cmd/states. Be tolerant of variants.
    sff = f("sprite", "sff")
    air = f("anim", "air")
    snd = f("sound", "snd")
    cmd = f("cmd")
    cns = f("cns", "st", "st0")
    pal = None
    for key in ("pal1", "pal.defaults"):
        if key in files:
            pal = _resolve_case_insensitive(base, files[key].split(",")[0].strip())
            if pal:
                break
    if pal is None:
        # Old characters often have ACT files but the DEF key naming varies.
        pal = next(iter(sorted(base.glob("*.act"))), None)

    # If [Files] is sparse, fall back to unique file types beside the DEF.
    if sff is None:
        sffs = list(base.glob("*.sff")) + list(base.glob("*.SFF"))
        sff = sffs[0] if sffs else None
    if air is None:
        airs = list(base.glob("*.air")) + list(base.glob("*.AIR"))
        air = airs[0] if airs else None

    if sff is None or air is None:
        raise MugenImportError(
            f"Could not locate both the SFF sprite file and AIR animation file for {def_file.name}."
        )

    name = info.get("name") or def_file.stem
    display = info.get("displayname") or name
    author = info.get("author", "Unknown")
    return MugenCharacterInfo(def_file, name, display, author, sff, air, snd, cmd, cns, pal)


def list_def_files(root: Path) -> List[Path]:
    defs = [p for p in root.rglob("*.def") if p.is_file()]
    # Prefer character DEFs that point to a sprite + anim pair.
    scored = []
    for p in defs:
        try:
            _, files = parse_def(p)
            score = int("sprite" in files or "sff" in files) * 3 + int("anim" in files or "air" in files) * 3
            score += int("cmd" in files) + int("cns" in files)
        except Exception:
            score = 0
        scored.append((score, len(p.parts), p))
    return [p for _, _, p in sorted(scored, key=lambda x: (-x[0], x[1], x[2].name.lower()))]


def find_archive_tool() -> Optional[Tuple[str, List[str]]]:
    """Return an extractor command descriptor (kind, executable command prefix)."""
    candidates: List[Tuple[str, Path]] = []
    # 7-Zip common install locations / PATH
    for exe in (shutil.which("7z"), shutil.which("7zz"), shutil.which("7za")):
        if exe:
            candidates.append(("7z", Path(exe)))
    if os.name == "nt":
        for p in (
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "7-Zip" / "7z.exe",
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "7-Zip" / "7z.exe",
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "WinRAR" / "UnRAR.exe",
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "WinRAR" / "WinRAR.exe",
        ):
            if p.exists():
                candidates.append(("unrar" if "unrar" in p.name.lower() else "7z" if "7z" in p.name.lower() else "winrar", p))
        # Windows 11's bsdtar/libarchive may understand RAR on current builds.
        tar = shutil.which("tar")
        if tar:
            candidates.append(("tar", Path(tar)))
    else:
        for name in ("unrar", "unar", "bsdtar", "tar"):
            exe = shutil.which(name)
            if exe:
                candidates.append((name, Path(exe)))
    if not candidates:
        return None
    kind, exe = candidates[0]
    return kind, [str(exe)]


def extract_archive(path: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()
    if ext == ".zip":
        with zipfile.ZipFile(path) as zf:
            # Avoid absolute/path traversal entries.
            root = out_dir.resolve()
            for info in zf.infolist():
                dest = (out_dir / info.filename).resolve()
                if root not in dest.parents and dest != root:
                    raise MugenImportError("Archive contains an unsafe path and was not extracted.")
            zf.extractall(out_dir)
        return out_dir
    if ext not in (".rar", ".7z"):
        raise MugenImportError(f"Unsupported archive type: {ext}")

    tool = find_archive_tool()
    if not tool:
        raise MugenImportError(
            "Rumble Studio found a RAR/7Z MUGEN archive, but this computer does not have a compatible extractor.\n\n"
            "Install 7-Zip (recommended), then click Import MUGEN again. You can also extract the archive yourself "
            "and choose the character's .def file directly."
        )
    kind, prefix = tool
    if kind == "7z":
        cmd = prefix + ["x", "-y", f"-o{out_dir}", str(path)]
    elif kind == "unrar":
        cmd = prefix + ["x", "-o+", str(path), str(out_dir) + os.sep]
    elif kind == "winrar":
        cmd = prefix + ["x", "-ibck", "-y", str(path), str(out_dir) + os.sep]
    elif kind in ("tar", "bsdtar"):
        cmd = prefix + ["-xf", str(path), "-C", str(out_dir)]
    elif kind == "unar":
        cmd = prefix + ["-f", "-o", str(out_dir), str(path)]
    else:
        raise MugenImportError("No supported archive extractor was found.")
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise MugenImportError(
            "The MUGEN archive could not be extracted automatically.\n\n"
            f"Extractor: {' '.join(prefix)}\n"
            f"Message: {(proc.stderr or proc.stdout)[-1000:]}\n\n"
            "You can extract it manually and import the .def file instead."
        )
    return out_dir


def prepare_source(path: Path, work_dir: Path) -> Tuple[Path, Optional[Path]]:
    """Return (def file, extracted root if an archive was unpacked)."""
    path = path.resolve()
    if path.is_dir():
        defs = list_def_files(path)
        if not defs:
            raise MugenImportError("No MUGEN .def file was found in that folder.")
        return defs[0], None
    if path.suffix.lower() == ".def":
        return path, None
    if path.suffix.lower() in (".zip", ".rar", ".7z"):
        extracted = work_dir / "archive"
        extract_archive(path, extracted)
        defs = list_def_files(extracted)
        if not defs:
            raise MugenImportError("The archive extracted, but no MUGEN character .def file was found.")
        return defs[0], extracted
    raise MugenImportError("Choose a MUGEN .def file, character folder, .zip, .rar, or .7z archive.")


def read_sff_version(path: Path) -> Tuple[int, int, int, int]:
    """Return SFF version as (major, minor1, minor2, minor3).

    Elecbyte stores the four version bytes on disk low-to-high (verlo3, verlo2,
    verlo1, verhi) for both SFF v1 and v2. v0.6 treated classic v1.0.1.0 files
    as 0.1.0.1, which is exactly the compatibility bug fixed in v0.7.
    """
    with path.open("rb") as f:
        head = f.read(16)
    if len(head) < 16 or head[:12] != b"ElecbyteSpr\x00":
        raise MugenImportError(f"{path.name} is not recognized as a MUGEN SFF file.")
    verlo3, verlo2, verlo1, verhi = head[12:16]
    return (verhi, verlo1, verlo2, verlo3)


def _read_act_palette(path: Optional[Path]) -> Optional[List[Tuple[int, int, int, int]]]:
    """Read a MUGEN ACT palette.

    Classic external ACT palettes store 256 RGB triplets in reverse logical
    order. Palette index 0 is transparent in-game.
    """
    if not path or not path.exists():
        return None
    data = path.read_bytes()
    if len(data) < 768:
        return None
    data = data[:768]
    pal: List[Tuple[int, int, int, int]] = [(0, 0, 0, 0)] * 256
    for i in range(256):
        r, g, b = data[i * 3:i * 3 + 3]
        dst = 255 - i
        pal[dst] = (r, g, b, 0 if dst == 0 else 255)
    return pal


def _decode_v1_palette(block: bytes) -> List[Tuple[int, int, int, int]]:
    if len(block) < 768:
        return [(0, 0, 0, 0)] * 256
    block = block[:768]
    pal: List[Tuple[int, int, int, int]] = []
    for i in range(256):
        r, g, b = block[i * 3:i * 3 + 3]
        pal.append((r, g, b, 0 if i == 0 else 255))
    return pal


def _decode_sff1_pcx_indices(data: bytes) -> Tuple[int, int, bytes]:
    """Decode the 8-bit, one-plane PCX stream embedded in SFF v1.

    We intentionally do not ask Pillow to decode the whole PCX. Many real
    MUGEN SFF v1 sprites omit a normal PCX palette trailer because the
    palette is stored separately by SFF. Pillow can report "buffer overrun"
    on those otherwise-valid sprites. Decoding the indexed RLE directly is
    both simpler and faithful to the format.
    """
    if len(data) < 128:
        raise MugenImportError(f"SFF v1 PCX data is too short ({len(data)} bytes).")
    h = data[:128]
    if h[0] != 0x0A:
        raise MugenImportError(f"SFF v1 sprite is not PCX data (manufacturer byte {h[0]:02x}).")
    if h[2] != 1 or h[3] != 8:
        raise MugenImportError(f"Unsupported SFF v1 PCX encoding={h[2]} bpp={h[3]}; expected RLE/8-bit.")
    planes = h[65]
    if planes != 1:
        raise MugenImportError(f"Unsupported SFF v1 PCX plane count {planes}; expected 1.")
    xmin, ymin, xmax, ymax = struct.unpack_from('<HHHH', h, 4)
    if xmax < xmin or ymax < ymin:
        raise MugenImportError("Invalid SFF v1 PCX dimensions.")
    width = xmax - xmin + 1
    height = ymax - ymin + 1
    bytes_per_line = struct.unpack_from('<H', h, 66)[0]
    if bytes_per_line < width or bytes_per_line <= 0:
        raise MugenImportError(f"Invalid SFF v1 PCX bytes-per-line value {bytes_per_line} for width {width}.")
    if width * height > 4096 * 4096:
        raise MugenImportError(f"Implausibly large SFF v1 sprite {width}x{height}.")

    cursor = 128
    out = bytearray(width * height)
    out_pos = 0
    for y in range(height):
        line = bytearray()
        while len(line) < bytes_per_line:
            if cursor >= len(data):
                raise MugenImportError(
                    f"Truncated SFF v1 PCX RLE at row {y}; decoded {len(line)}/{bytes_per_line} bytes."
                )
            b = data[cursor]
            cursor += 1
            if b & 0xC0 == 0xC0:
                count = b & 0x3F
                if cursor >= len(data):
                    raise MugenImportError(f"Truncated SFF v1 PCX run at row {y}.")
                value = data[cursor]
                cursor += 1
                remaining = bytes_per_line - len(line)
                # A few old encoders overrun a padded scanline by a byte or two.
                # Clamp to the declared scanline instead of rejecting the whole char.
                count = min(count, remaining)
                line.extend([value] * count)
            else:
                line.append(b)
        out[out_pos:out_pos + width] = line[:width]
        out_pos += width
    return width, height, bytes(out)


def _apply_indexed_palette(indices: bytes, width: int, height: int,
                           palette: List[Tuple[int, int, int, int]]) -> Image.Image:
    if len(palette) < 256:
        palette = list(palette) + [(0, 0, 0, 0)] * (256 - len(palette))
    rgba = bytearray(width * height * 4)
    for i, idx in enumerate(indices[:width * height]):
        r, g, b, a = palette[idx]
        j = i * 4
        rgba[j:j+4] = bytes((r, g, b, a))
    return Image.frombytes('RGBA', (width, height), bytes(rgba))


def read_sff1(path: Path, external_palette: Optional[Path] = None) -> Dict[Tuple[int, int], SffSprite]:
    """Decode a classic SFF v1 archive.

    Important v1 details handled here:
    * sprite Length counts bytes *after* the fixed 32-byte subheader;
    * a non-shared sprite's 768-byte palette block lives inside Length;
    * zero-length sprites inherit the previous sprite's resolved pixels;
    * true backward LinkedIndex references are followed for data-owning sprites;
    * shared palettes inherit from the previous resolved palette;
    * an external ACT palette, when supplied by the DEF, overrides embedded palettes.
    """
    data = path.read_bytes()
    if len(data) < 512 or data[:12] != b"ElecbyteSpr\x00":
        raise MugenImportError("Invalid SFF v1 file header.")
    version = read_sff_version(path)
    if version[0] != 1:
        raise MugenImportError(
            f"This MUGEN character uses SFF v{version[0]}.{version[1]}.{version[2]}.{version[3]}, not SFF v1."
        )

    image_total = struct.unpack_from('<I', data, 20)[0]
    first_sub = struct.unpack_from('<I', data, 24)[0]
    if image_total > 200000 or first_sub < 32 or first_sub >= len(data):
        raise MugenImportError("SFF v1 header contains invalid sprite table values.")

    entries = []
    offset = first_sub
    for i in range(image_total):
        if not offset or offset + 32 > len(data):
            break
        next_off, length = struct.unpack_from('<II', data, offset)
        axis_x, axis_y = struct.unpack_from('<hh', data, offset + 8)
        group, image, link_index = struct.unpack_from('<HHH', data, offset + 12)
        pal_same = data[offset + 18] != 0
        entries.append({
            'offset': offset + 32,
            'length': int(length),
            'axis_x': int(axis_x),
            'axis_y': int(axis_y),
            'group': int(group),
            'image': int(image),
            'link': int(link_index),
            'shared_palette': pal_same,
            'next': int(next_off),
        })
        if i + 1 >= image_total:
            break
        if next_off <= offset or next_off > len(data):
            break
        offset = next_off

    if not entries:
        raise MugenImportError("No sprite entries were found in the SFF v1 file.")

    external = _read_act_palette(external_palette)
    pixel_cache: Dict[int, Tuple[int, int, bytes]] = {}
    palette_cache: Dict[int, List[Tuple[int, int, int, int]]] = {}

    def resolve_pixels(i: int, chain=None) -> Tuple[int, int, bytes]:
        if i in pixel_cache:
            return pixel_cache[i]
        if not (0 <= i < len(entries)):
            raise MugenImportError(f"SFF v1 linked sprite index {i} is out of range.")
        chain = set() if chain is None else set(chain)
        if i in chain:
            raise MugenImportError(f"SFF v1 linked sprite cycle at index {i}.")
        chain.add(i)
        e = entries[i]
        if e['length'] == 0:
            if i == 0:
                result = (1, 1, b'\x00')
            else:
                result = resolve_pixels(i - 1, chain)
            pixel_cache[i] = result
            return result
        linked = e['link']
        if linked >= i:
            linked = 0
        if linked != 0:
            result = resolve_pixels(linked, chain)
            pixel_cache[i] = result
            return result

        pixel_len = e['length'] - (0 if e['shared_palette'] else 768)
        if pixel_len < 128:
            # Some old SFFs mark the first/shared palette oddly. Try full Length as pixels.
            pixel_len = e['length']
        start = e['offset']
        end = min(len(data), start + pixel_len)
        raw = data[start:end]
        try:
            result = _decode_sff1_pcx_indices(raw)
        except MugenImportError:
            # Real files occasionally overstate a final shared sprite by one palette block.
            if e['shared_palette'] and pixel_len > 768:
                result = _decode_sff1_pcx_indices(data[start:min(len(data), start + pixel_len - 768)])
            else:
                raise
        pixel_cache[i] = result
        return result

    def resolve_palette(i: int, chain=None) -> List[Tuple[int, int, int, int]]:
        if external is not None:
            return external
        if i in palette_cache:
            return palette_cache[i]
        chain = set() if chain is None else set(chain)
        if i in chain:
            return [(0, 0, 0, 0)] * 256
        chain.add(i)
        e = entries[i]
        if e['length'] == 0 and i > 0:
            pal = resolve_palette(i - 1, chain)
        elif e['shared_palette'] and i > 0:
            pal = resolve_palette(i - 1, chain)
        elif e['length'] >= 768:
            pal_start = e['offset'] + e['length'] - 768
            pal_end = pal_start + 768
            if 0 <= pal_start and pal_end <= len(data):
                pal = _decode_v1_palette(data[pal_start:pal_end])
            elif i > 0:
                pal = resolve_palette(i - 1, chain)
            else:
                pal = [(0, 0, 0, 0)] * 256
        elif i > 0:
            pal = resolve_palette(i - 1, chain)
        else:
            pal = [(0, 0, 0, 0)] * 256
        palette_cache[i] = pal
        return pal

    by_key: Dict[Tuple[int, int], SffSprite] = {}
    usable = 0
    errors: List[str] = []
    for i, e in enumerate(entries):
        try:
            width, height, indices = resolve_pixels(i)
            pal = resolve_palette(i)
            rgba = _apply_indexed_palette(indices, width, height, pal)
            if width > 1 or height > 1:
                usable += 1
        except Exception as exc:
            errors.append(f"#{i} {e['group']},{e['image']}: {exc}")
            rgba = Image.new('RGBA', (1, 1), (0, 0, 0, 0))
        # Keep first sprite for duplicate keys, matching normal engine behavior.
        by_key.setdefault(
            (e['group'], e['image']),
            SffSprite(e['group'], e['image'], e['axis_x'], e['axis_y'], rgba, None),
        )

    if usable == 0:
        detail = errors[0] if errors else "unknown PCX decode failure"
        raise MugenImportError(f"No usable sprites could be decoded from the SFF v1 file. First error: {detail}")
    return by_key

def _sff2_apply_palette(indices: bytes, width: int, height: int, palette: List[Tuple[int, int, int, int]]) -> Image.Image:
    """Turn indexed SFF v2 pixels into an RGBA Pillow image."""
    needed = max(0, width * height)
    if len(indices) < needed:
        indices = indices + bytes(needed - len(indices))
    elif len(indices) > needed:
        indices = indices[:needed]
    if not palette:
        palette = [(0, 0, 0, 0)] + [(255, 0, 255, 255)] * 255
    # SFF palettes can legally contain fewer than 256 entries.
    pal = list(palette)
    if len(pal) < 256:
        pal.extend([(0, 0, 0, 0)] * (256 - len(pal)))
    pix = Image.frombytes("P", (max(1, width), max(1, height)), indices or b"\x00")
    rgb = []
    for r, g, b, a in pal[:256]:
        rgb.extend((r, g, b))
    pix.putpalette(rgb)
    rgba = pix.convert("RGBA")
    alpha_bytes = bytes(pal[v][3] if v < len(pal) else 0 for v in (indices or b"\x00"))
    alpha = Image.frombytes("L", rgba.size, alpha_bytes)
    rgba.putalpha(alpha)
    return rgba


def _sff2_rle8_decode(src: bytes, pixel_count: int) -> bytes:
    out = bytearray()
    i = 0
    while len(out) < pixel_count:
        if i >= len(src):
            raise MugenImportError("Truncated SFF v2 RLE8 sprite data.")
        d = src[i]; i += 1
        count = 1
        if d & 0xC0 == 0x40:
            count = d & 0x3F
            if i >= len(src):
                raise MugenImportError("Truncated SFF v2 RLE8 run.")
            d = src[i]; i += 1
        if count <= 0:
            continue
        take = min(count, pixel_count - len(out))
        out.extend(bytes([d]) * take)
    return bytes(out)


def _sff2_rle5_decode(src: bytes, pixel_count: int) -> bytes:
    out = bytearray()
    i = 0
    while len(out) < pixel_count:
        if i + 1 >= len(src):
            raise MugenImportError("Truncated SFF v2 RLE5 sprite data.")
        rl = int(src[i]); i += 1
        dl_byte = src[i]
        dl = int(dl_byte & 0x7F)
        color = 0
        if dl_byte & 0x80:
            i += 1
            if i >= len(src):
                raise MugenImportError("Truncated SFF v2 RLE5 color run.")
            color = src[i]
        i += 1
        while True:
            if len(out) < pixel_count:
                out.append(color)
            rl -= 1
            if rl < 0:
                dl -= 1
                if dl < 0:
                    break
                if i >= len(src):
                    raise MugenImportError("Truncated SFF v2 RLE5 packet.")
                color = src[i] & 0x1F
                rl = int(src[i] >> 5)
                i += 1
    return bytes(out)


def _sff2_lz5_decode(src: bytes, pixel_count: int) -> bytes:
    if not src and pixel_count:
        raise MugenImportError("Empty SFF v2 LZ5 sprite data.")
    out = bytearray()
    i = 0
    ctrl = src[i] if src else 0
    i += 1
    ctrl_shift = 0
    recycle = 0
    recycle_bits = 0
    while len(out) < pixel_count:
        if i >= len(src):
            raise MugenImportError("Truncated SFF v2 LZ5 sprite data.")
        d = int(src[i]); i += 1
        if ctrl & (1 << ctrl_shift):
            # LZ copy packet.
            if d & 0x3F == 0:
                if i + 1 >= len(src):
                    raise MugenImportError("Truncated SFF v2 LZ5 long copy packet.")
                distance = ((d << 2) | int(src[i])) + 1
                i += 1
                n = int(src[i]) + 2
                i += 1
            else:
                recycle |= ((d & 0xC0) >> recycle_bits)
                recycle_bits += 2
                n = int(d & 0x3F)
                if recycle_bits < 8:
                    if i >= len(src):
                        raise MugenImportError("Truncated SFF v2 LZ5 short copy packet.")
                    distance = int(src[i]) + 1
                    i += 1
                else:
                    distance = int(recycle) + 1
                    recycle = 0
                    recycle_bits = 0
            # Elecbyte/Ikemen's loop emits n+1 bytes for copy packets.
            count = n + 1
            if distance <= 0 or distance > len(out):
                raise MugenImportError("Invalid SFF v2 LZ5 back-reference.")
            for _ in range(count):
                if len(out) >= pixel_count:
                    break
                out.append(out[-distance])
        else:
            # Literal color run.
            if d & 0xE0 == 0:
                if i >= len(src):
                    raise MugenImportError("Truncated SFF v2 LZ5 literal packet.")
                count = int(src[i]) + 8
                i += 1
                color = d
            else:
                count = d >> 5
                color = d & 0x1F
            for _ in range(count):
                if len(out) >= pixel_count:
                    break
                out.append(color)
        ctrl_shift += 1
        if ctrl_shift >= 8:
            if len(out) >= pixel_count:
                break
            if i >= len(src):
                raise MugenImportError("Truncated SFF v2 LZ5 control byte.")
            ctrl = src[i]
            i += 1
            ctrl_shift = 0
    return bytes(out)


def read_sff2(path: Path) -> Dict[Tuple[int, int], SffSprite]:
    """Decode an Elecbyte SFF v2 archive into Rumble-ready RGBA sprites.

    Supports raw indexed/true-color sprites, RLE8, RLE5, LZ5, PNG8, PNG24,
    PNG32, linked sprites, linked palettes, local/translated data blocks.
    """
    data = path.read_bytes()
    if len(data) < 64 or data[:12] != b"ElecbyteSpr\x00":
        raise MugenImportError("Invalid SFF file header.")
    version = read_sff_version(path)
    if version[0] != 2:
        raise MugenImportError(
            f"This MUGEN character uses SFF v{version[0]}.{version[1]}.{version[2]}.{version[3]}, not SFF v2."
        )
    try:
        first_sprite, sprite_count, first_palette, palette_count, lofs, _reserved, tofs = struct.unpack_from(
            "<IIIIIII", data, 36
        )
    except struct.error as e:
        raise MugenImportError(f"Truncated SFF v2 header: {e}")
    if sprite_count > 200000 or palette_count > 20000:
        raise MugenImportError("SFF v2 header contains unreasonable sprite/palette counts.")

    palettes: List[List[Tuple[int, int, int, int]]] = []
    for i in range(palette_count):
        pos = first_palette + i * 16
        if pos + 16 > len(data):
            raise MugenImportError("Truncated SFF v2 palette table.")
        group, item, numcols, link, rel_ofs, size = struct.unpack_from("<HHHHII", data, pos)
        if size == 0:
            if 0 <= link < len(palettes):
                palettes.append(list(palettes[link]))
            else:
                palettes.append([])
            continue
        absolute = lofs + rel_ofs
        if absolute < 0 or absolute + size > len(data):
            raise MugenImportError("SFF v2 palette data points outside the file.")
        raw = data[absolute:absolute + size]
        count = min(int(numcols) if numcols else len(raw)//4, len(raw)//4, 256)
        pal: List[Tuple[int, int, int, int]] = []
        for j in range(count):
            r, g, b, a = raw[j*4:j*4+4]
            # SFF 2.0 files commonly leave alpha undefined; follow Elecbyte convention.
            if version[2] == 0:
                a = 0 if j == 0 else 255
            pal.append((r, g, b, a))
        palettes.append(pal)

    by_index: List[SffSprite] = []
    by_key: Dict[Tuple[int, int], SffSprite] = {}
    for i in range(sprite_count):
        pos = first_sprite + i * 28
        if pos + 28 > len(data):
            raise MugenImportError("Truncated SFF v2 sprite table.")
        group, image, width, height, axis_x, axis_y, link, fmt, depth, rel_ofs, size, palidx, flags = struct.unpack_from(
            "<HHHHhhHBBIIHH", data, pos
        )
        sprite: Optional[SffSprite] = None
        if size == 0:
            if 0 <= link < len(by_index):
                src = by_index[link]
                sprite = SffSprite(group, image, axis_x, axis_y, src.rgba.copy(), link)
        else:
            absolute = (tofs if (flags & 1) else lofs) + rel_ofs
            if absolute < 0 or absolute + size > len(data):
                # Some fan-made SFFs have a few broken helper sprites. Keep import going with a placeholder.
                absolute = -1
            if absolute >= 0:
                blob = data[absolute:absolute + size]
                pixel_count = max(1, int(width) * int(height))
                try:
                    if fmt == 0:
                        if depth == 8:
                            indices = blob[:pixel_count]
                            pal = palettes[palidx] if 0 <= palidx < len(palettes) else []
                            rgba = _sff2_apply_palette(indices, width, height, pal)
                        elif depth == 24:
                            need = int(width) * int(height) * 3
                            if len(blob) < need:
                                raise MugenImportError("Truncated raw 24-bit SFF v2 sprite.")
                            rgba = Image.frombytes("RGB", (width, height), blob[:need]).convert("RGBA")
                        elif depth == 32:
                            need = int(width) * int(height) * 4
                            if len(blob) < need:
                                raise MugenImportError("Truncated raw 32-bit SFF v2 sprite.")
                            rgba = Image.frombytes("RGBA", (width, height), blob[:need])
                        else:
                            raise MugenImportError(f"Unsupported raw SFF v2 color depth {depth}.")
                    elif fmt in (2, 3, 4):
                        if len(blob) < 4:
                            raise MugenImportError("Truncated compressed SFF v2 sprite.")
                        src = blob[4:]
                        if fmt == 2:
                            indices = _sff2_rle8_decode(src, pixel_count)
                        elif fmt == 3:
                            indices = _sff2_rle5_decode(src, pixel_count)
                        else:
                            indices = _sff2_lz5_decode(src, pixel_count)
                        pal = palettes[palidx] if 0 <= palidx < len(palettes) else []
                        rgba = _sff2_apply_palette(indices, width, height, pal)
                    elif fmt in (10, 11, 12):
                        if len(blob) < 4:
                            raise MugenImportError("Truncated PNG SFF v2 sprite.")
                        im = Image.open(io.BytesIO(blob[4:]))
                        im.load()
                        if fmt == 10 and im.mode == "P":
                            indices = im.tobytes()
                            pal = palettes[palidx] if 0 <= palidx < len(palettes) else []
                            rgba = _sff2_apply_palette(indices, im.width, im.height, pal)
                        else:
                            rgba = im.convert("RGBA")
                    else:
                        raise MugenImportError(f"Unsupported SFF v2 sprite compression format {fmt}.")
                    sprite = SffSprite(group, image, axis_x, axis_y, rgba, None)
                except (MugenImportError, OSError, ValueError, IndexError):
                    # Do not let one malformed helper/effect sprite make the whole character unusable.
                    sprite = None
        if sprite is None:
            sprite = SffSprite(group, image, axis_x, axis_y, Image.new("RGBA", (1, 1), (0, 0, 0, 0)), None)
        by_index.append(sprite)
        if (group, image) not in by_key:
            by_key[(group, image)] = sprite

    visible = [s for s in by_key.values() if s.rgba.width > 1 or s.rgba.height > 1]
    if not visible:
        raise MugenImportError("No usable sprites could be decoded from the SFF v2 file.")
    return by_key

def parse_air(path: Path) -> Dict[int, AirAction]:
    actions: Dict[int, AirAction] = {}
    current: Optional[AirAction] = None
    pending_comment = ""
    begin_re = re.compile(r"\[\s*Begin\s+Action\s+(-?\d+)\s*\]", re.I)
    for raw in _decode_text(path).splitlines():
        stripped = raw.strip()
        if stripped.startswith(";"):
            comment = stripped[1:].strip()
            if comment:
                pending_comment = comment
            continue
        # Preserve inline comment for action identification, then remove it for parsing.
        line, _, inline = raw.partition(";")
        line = line.strip()
        if not line:
            continue
        m = begin_re.match(line)
        if m:
            num = int(m.group(1))
            current = AirAction(num, [], pending_comment)
            actions[num] = current
            pending_comment = ""
            continue
        if current is None:
            continue
        low = line.lower()
        if low.startswith("clsn") or low.startswith("loopstart") or low.startswith("interpolate"):
            continue
        # AIR element: group,image,x,y,ticks[,flip[,blend...]]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            group = int(parts[0]); image = int(parts[1]); x = int(float(parts[2] or 0)); y = int(float(parts[3] or 0)); ticks = int(float(parts[4] or 1))
        except ValueError:
            continue
        opt = ",".join(parts[5:]).upper() if len(parts) > 5 else ""
        # H/V can be in the first option slot; blend flags may also be present.
        flags = {x.strip() for x in re.split(r"[ ,]+", opt) if x.strip()}
        current.frames.append(AirFrame(group, image, x, y, ticks, "H" in flags or "HV" in flags, "V" in flags or "HV" in flags))
    return actions


def _render_action(action: AirAction, sprites: Dict[Tuple[int, int], SffSprite], out_path: Path, max_dim: int = 900) -> bool:
    prepared = []
    min_x = min_y = 10**9
    max_x = max_y = -10**9
    for fr in action.frames:
        spr = sprites.get((fr.group, fr.image))
        if spr is None:
            continue
        img = spr.rgba
        axis_x, axis_y = spr.axis_x, spr.axis_y
        if fr.flip_h:
            img = ImageOps.mirror(img)
            axis_x = img.width - axis_x
        if fr.flip_v:
            img = ImageOps.flip(img)
            axis_y = img.height - axis_y
        # AIR x/y offsets are applied relative to the character axis.
        left = fr.x - axis_x
        top = fr.y - axis_y
        right = left + img.width
        bottom = top + img.height
        min_x, min_y = min(min_x, left), min(min_y, top)
        max_x, max_y = max(max_x, right), max(max_y, bottom)
        prepared.append((fr, img, left, top))
    if not prepared:
        return False

    width = max(1, max_x - min_x)
    height = max(1, max_y - min_y)
    # Prevent pathological helper/projectile actions from creating giant canvases.
    if width > max_dim or height > max_dim:
        scale = min(max_dim / width, max_dim / height)
    else:
        scale = 1.0
    frames: List[Image.Image] = []
    durations: List[int] = []
    for fr, img, left, top in prepared:
        canvas = Image.new("RGBA", (width, height), (0,0,0,0))
        canvas.alpha_composite(img, (left - min_x, top - min_y))
        if scale < 1.0:
            canvas = canvas.resize((max(1, int(width*scale)), max(1, int(height*scale))), Image.Resampling.NEAREST)
        frames.append(canvas)
        ticks = fr.ticks
        if ticks < 0:
            ms = 250
        else:
            ms = max(17, int(round(max(1, ticks) * 1000 / 60)))
        durations.append(ms)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if len(frames) == 1:
        frames[0].save(out_path, format="GIF", save_all=True, duration=durations[0], loop=0, disposal=2, transparency=0)
    else:
        frames[0].save(out_path, format="GIF", save_all=True, append_images=frames[1:], duration=durations, loop=0, disposal=2, optimize=False)
    return True


def _first_present(actions: Dict[int, AirAction], nums: Iterable[int]) -> Optional[int]:
    for n in nums:
        if n in actions and actions[n].frames:
            return n
    return None


def auto_map_actions(actions: Dict[int, AirAction]) -> Tuple[Dict[str, int], List[int]]:
    mapping: Dict[str, int] = {}
    slot_candidates = {
        "idle": [0],
        "walk": [20, 21],
        "run": [100, 20],
        "jump": [41, 42, 43, 44, 45, 46, 40],
        "guard": [130, 120, 150, 131, 132],
        "hit": [5000, 5001, 5002, 5010, 5011, 5012, 5020, 5021, 5022, 5030],
        "ko": [5150, 5140, 5110, 5100, 5070],
        "victory": [180, 181, 182, 183, 184, 185, 186, 187, 188, 189],
        "cheer": [195, 180, 181],
        "happy": [180, 181, 195],
        "victory_jump": [180, 181, 42, 41],
    }
    for slot, nums in slot_candidates.items():
        n = _first_present(actions, nums)
        if n is not None:
            mapping[slot] = n

    # Recommended MUGEN numbering: 200-999 normal attacks, 1000-2999 special, 3000-4999 hyper.
    attack_candidates = [n for n, a in sorted(actions.items()) if 200 <= n <= 4999 and a.frames]
    # Prefer common normals first, then specials/hypers for visual variety.
    ordered: List[int] = []
    for pool in (
        [n for n in attack_candidates if 200 <= n < 1000],
        [n for n in attack_candidates if 1000 <= n < 3000],
        [n for n in attack_candidates if 3000 <= n < 5000],
    ):
        if pool:
            # Spread selections across the pool rather than taking four adjacent sub-actions.
            for n in pool:
                if n not in ordered:
                    ordered.append(n)
                    if len(ordered) >= 12:
                        break
        if len(ordered) >= 12:
            break
    chosen = []
    # Prefer canonical strong/medium/light spacing where present.
    for n in (200, 210, 220, 230, 240, 250, 1000, 1100, 1200, 3000):
        if n in actions and n not in chosen:
            chosen.append(n)
    for n in ordered:
        if n not in chosen:
            chosen.append(n)
        if len(chosen) >= 4:
            break
    chosen = chosen[:4]
    for i, n in enumerate(chosen, 1):
        mapping[f"attack_{i}"] = n
    return mapping, attack_candidates




MOVE_KEYWORDS = {
    "beam": ("beam", "kamehameha", "laser", "ray", "masenko", "galick", "wave", "cannon"),
    "projectile": ("blast", "shot", "fireball", "projectile", "missile", "bullet", "arrow", "ki ", "energy ball", "orb"),
    "melee": ("punch", "kick", "slash", "uppercut", "combo", "rush", "tackle", "throw", "strike", "smash", "claw", "sword"),
}

def _clean_move_label(text: str, fallback: str) -> str:
    t = (text or "").strip().strip(";:-_ ")
    t = re.sub(r"^state\s*-?1\s*,?", "", t, flags=re.I).strip(" ,-_")
    t = re.sub(r"[_]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t or t.lower() in {"state", "changestate", "attack", "move"}:
        t = fallback
    return t[:48]

def _infer_rumble_attack_type(label: str, action_no: int) -> str:
    low = f" {label.lower()} "
    for kind in ("beam", "projectile", "melee"):
        if any(k in low for k in MOVE_KEYWORDS[kind]):
            return kind
    # Specials/hypers are more likely to be ranged, but defaulting to melee is safer
    # than turning every flashy animation into a projectile.
    return "melee"

def _collect_state_files(info: MugenCharacterInfo) -> List[Path]:
    """Collect CNS/ST state definition files referenced by the character DEF."""
    _, files = parse_def(info.def_file)
    out: List[Path] = []
    for key, rel in files.items():
        lk = key.lower()
        if lk == "cns" or lk.startswith("st"):
            hit = _resolve_case_insensitive(info.def_file.parent, rel)
            if hit and hit.is_file() and hit.suffix.lower() in {".cns", ".st"}:
                out.append(hit)
    if info.cns_file and info.cns_file not in out:
        out.append(info.cns_file)
    # Some authors omit state files from DEF or use unconventional keys.
    if not out:
        out.extend(sorted(info.def_file.parent.glob("*.cns")))
        out.extend(sorted(info.def_file.parent.glob("*.st")))
    seen = set(); uniq=[]
    for x in out:
        k=str(x.resolve()).lower()
        if k not in seen:
            seen.add(k); uniq.append(x)
    return uniq

def _parse_statedef_animation_map(info: MugenCharacterInfo) -> Tuple[Dict[int, int], Dict[int, str]]:
    """Return StateDef -> AIR animation number plus a useful state label when available."""
    state_to_anim: Dict[int, int] = {}
    labels: Dict[int, str] = {}
    statedef_re = re.compile(r"\[\s*StateDef\s+(-?\d+)\s*\]", re.I)
    for path in _collect_state_files(info):
        current = None
        pending_comment = ""
        try:
            lines = _decode_text(path).splitlines()
        except Exception:
            continue
        for raw in lines:
            stripped = raw.strip()
            if stripped.startswith(";"):
                c = stripped[1:].strip()
                if c:
                    pending_comment = c
                continue
            line = raw.split(";", 1)[0].strip()
            if not line:
                continue
            m = statedef_re.match(line)
            if m:
                current = int(m.group(1))
                if pending_comment:
                    labels.setdefault(current, pending_comment)
                pending_comment = ""
                continue
            if current is None or "=" not in line:
                continue
            k, v = [x.strip() for x in line.split("=", 1)]
            if k.lower() == "anim":
                mnum = re.search(r"-?\d+", v)
                if mnum:
                    state_to_anim[current] = int(mnum.group())
    return state_to_anim, labels

def scan_mugen_attack_candidates(info: MugenCharacterInfo, actions: Dict[int, AirAction]) -> List[dict]:
    """Deep-scan CMD/CNS/AIR to find move animations and human-ish labels.

    This does not execute MUGEN logic. It only follows obvious ChangeState links from
    command blocks into StateDefs and then maps those states to AIR animation numbers.
    """
    state_to_anim, state_labels = _parse_statedef_animation_map(info)
    found: Dict[int, dict] = {}

    def add(action_no: int, name: str, score: int, source: str, state_no: Optional[int] = None):
        if action_no not in actions or not actions[action_no].frames:
            return
        comment = actions[action_no].comment or ""
        fallback = comment or (f"Action {action_no}")
        label = _clean_move_label(name or comment, fallback)
        item = {
            "action": action_no, "name": label, "score": score, "source": source,
            "state": state_no, "type": _infer_rumble_attack_type(label, action_no),
        }
        old = found.get(action_no)
        if old is None or score > old["score"]:
            found[action_no] = item

    # AIR comments often already contain useful move names.
    for n, a in actions.items():
        if not (200 <= n <= 4999) or not a.frames:
            continue
        base_score = 25 + (15 if 1000 <= n < 3000 else 20 if 3000 <= n < 5000 else 0)
        comment = (a.comment or "").strip()
        if comment:
            keyword_bonus = 18 if any(k in comment.lower() for vals in MOVE_KEYWORDS.values() for k in vals) else 5
            add(n, comment, base_score + keyword_bonus, "AIR comment")
        else:
            add(n, f"Action {n}", base_score, "AIR range")

    # CMD command/state blocks are much better evidence that something is a real move.
    if info.cmd_file and info.cmd_file.exists():
        text = _decode_text(info.cmd_file)
        section_re = re.compile(r"^\s*\[([^\]]+)\]\s*$")
        blocks=[]; header=None; body=[]
        for raw in text.splitlines()+["[__END__]"]:
            m=section_re.match(raw)
            if m:
                if header is not None:
                    blocks.append((header, body))
                header=m.group(1).strip(); body=[]
            else:
                body.append(raw)
        for header, lines in blocks:
            if not header.lower().startswith("state -1"):
                continue
            label = header.split(",", 1)[1].strip() if "," in header else ""
            clean_lines=[]
            for raw in lines:
                line=raw.split(";",1)[0].strip()
                if line:
                    clean_lines.append(line)
            blob="\n".join(clean_lines)
            if not re.search(r"(?im)^\s*type\s*=\s*changestate\b", blob):
                continue
            mv=re.search(r"(?im)^\s*value\s*=\s*(-?\d+)", blob)
            if not mv:
                continue
            state_no=int(mv.group(1))
            commands=re.findall(r'(?im)command\s*=\s*["\']?([^"\'\r\n]+)', blob)
            cmd_name=commands[0].strip() if commands else ""
            if not label:
                label=state_labels.get(state_no, "") or cmd_name
            anim_no=state_to_anim.get(state_no, state_no)
            # A named command-linked move is strong evidence. Specials/hypers score higher.
            tier = 30 if 1000 <= state_no < 3000 else 45 if 3000 <= state_no < 5000 else 15
            add(anim_no, label or cmd_name or f"State {state_no}", 100+tier, "CMD → StateDef", state_no)

    ranked=sorted(found.values(), key=lambda x: (-x["score"], x["action"]))
    return ranked

def choose_mugen_attack_slots(actions: Dict[int, AirAction], candidates: List[dict]) -> List[dict]:
    """Pick four varied attacks: normal, specials, hyper when available."""
    if not candidates:
        return []
    pools = [
        [x for x in candidates if 200 <= x["action"] < 1000],
        [x for x in candidates if 1000 <= x["action"] < 3000],
        [x for x in candidates if 3000 <= x["action"] < 5000],
    ]
    chosen=[]
    # One normal, up to two specials, one hyper is a useful generic mix.
    wishes=[(0,1),(1,2),(2,1)]
    for pi,count in wishes:
        for item in pools[pi][:count]:
            if item["action"] not in {x["action"] for x in chosen}:
                chosen.append(item)
    for item in candidates:
        if len(chosen)>=4: break
        if item["action"] not in {x["action"] for x in chosen}:
            chosen.append(item)
    return chosen[:4]


def import_mugen_character(
    source: Path, fighters_dir: Path, franchise: str = "MUGEN Import",
    export_all_actions: bool = True, existing_target: Optional[Path] = None,
    preserve_existing: bool = False, locked_animation_slots: Optional[set[str]] = None,
    locked_attack_slots: Optional[set[str]] = None,
) -> Tuple[Path, dict]:
    """Import a MUGEN source into a Rumble fighter folder.

    Returns (fighter folder, import report).
    """
    fighters_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rumble_mugen_") as td:
        work = Path(td)
        def_file, extracted_root = prepare_source(source, work)
        info = inspect_character(def_file)
        version = read_sff_version(info.sff_file)
        if version[0] == 1:
            sprites = read_sff1(info.sff_file, info.palette_file)
            sff_kind = "SFF v1"
        elif version[0] == 2:
            sprites = read_sff2(info.sff_file)
            sff_kind = "SFF v2"
        else:
            raise MugenImportError(
                f"Detected unsupported SFF v{version[0]}.{version[1]}.{version[2]}.{version[3]}. "
                "Rumble Studio v0.14 supports SFF v1 and SFF v2."
            )
        actions = parse_air(info.air_file)
        if not actions:
            raise MugenImportError("The AIR file was found, but no animation actions could be parsed.")

        # Load the existing fighter before choosing new mappings so v0.14 can protect
        # exact AIR/action choices the user explicitly locked.
        old_meta = {}
        if existing_target is not None:
            target = Path(existing_target)
            if (target / "fighter.json").exists():
                try:
                    old_meta = json.loads((target / "fighter.json").read_text(encoding="utf-8"))
                except Exception:
                    old_meta = {}
        else:
            target = None

        saved_locks = old_meta.get("mugen_locks", {}) if isinstance(old_meta.get("mugen_locks", {}), dict) else {}
        lock_anims = set(locked_animation_slots or saved_locks.get("animations", []) or [])
        lock_attacks = set(locked_attack_slots or saved_locks.get("attacks", []) or [])
        # A locked attack implicitly locks the artwork/action slot it uses.
        lock_anims.update(lock_attacks)

        def old_action_for_slot(slot: str) -> Optional[int]:
            rel = str(old_meta.get("animations", {}).get(slot, "") or "")
            mm = re.search(r"action_(-?\d+)\.gif$", rel.replace("\\", "/"), re.I)
            return int(mm.group(1)) if mm else None

        old_attacks_by_slot = {
            str(a.get("animation", "")): dict(a)
            for a in old_meta.get("attacks", []) if isinstance(a, dict) and a.get("animation")
        }

        mapping, legacy_attack_candidates = auto_map_actions(actions)
        deep_candidates = scan_mugen_attack_candidates(info, actions)

        # Lock non-attack animation mappings to the same AIR action when possible.
        for slot in lock_anims:
            old_action = old_action_for_slot(slot)
            if old_action is not None and old_action in actions:
                mapping[slot] = old_action

        # Replace generic attack guesses only for UNLOCKED attack slots. Locked slots
        # retain their old definition and mapping; the scanner researches the rest.
        for key in [k for k in mapping if k.startswith("attack_")]:
            if key not in lock_attacks:
                del mapping[key]
        reserved_actions = set()
        chosen_moves = []
        preserved_attack_slots = []
        refreshed_attack_slots = []
        for slot in ("attack_1", "attack_2", "attack_3", "attack_4"):
            if slot in lock_attacks:
                old_attack = old_attacks_by_slot.get(slot, {})
                old_action = old_attack.get("mugen_action")
                if old_action is None:
                    old_action = old_action_for_slot(slot)
                if old_action is not None:
                    try:
                        old_action = int(old_action)
                    except Exception:
                        old_action = None
                if old_action is not None:
                    reserved_actions.add(old_action)
                    if old_action in actions:
                        mapping[slot] = old_action
                preserved_attack_slots.append(slot)
                chosen_moves.append({
                    "slot": slot, "action": old_action,
                    "name": old_attack.get("name", slot),
                    "type": old_attack.get("type", "melee"),
                    "source": "LOCKED", "locked": True,
                })

        available_candidates = [x for x in deep_candidates if int(x.get("action", -999999)) not in reserved_actions]
        pick_iter = iter(choose_mugen_attack_slots(actions, available_candidates))
        used_new = set()
        for slot in ("attack_1", "attack_2", "attack_3", "attack_4"):
            if slot in lock_attacks:
                continue
            move = None
            while move is None:
                try:
                    cand = next(pick_iter)
                except StopIteration:
                    # Fall back to any remaining deep candidate not already used/reserved.
                    cand = next((x for x in available_candidates if int(x["action"]) not in used_new), None)
                    if cand is None:
                        break
                action_no = int(cand["action"])
                if action_no in reserved_actions or action_no in used_new:
                    continue
                move = dict(cand)
                used_new.add(action_no)
            if move is not None:
                mapping[slot] = int(move["action"])
                move["slot"] = slot
                move["locked"] = False
                chosen_moves.append(move)
                refreshed_attack_slots.append(slot)

        # Windows-safe name without importing studio.py (avoid circular imports).
        clean = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "", info.display_name).strip().rstrip(".") or info.name or "MUGEN Fighter"
        clean = re.sub(r"\s+", " ", clean)[:80]
        if existing_target is None:
            target = fighters_dir / clean
            base_target = target
            n = 2
            while target.exists() and (target / "fighter.json").exists():
                # Never silently overwrite an existing Rumble fighter on a normal import.
                target = fighters_dir / f"{base_target.name} MUGEN {n}"
                n += 1
        target.mkdir(parents=True, exist_ok=True)
        action_dir = target / "mugen_actions"
        # On a locked rescan keep the existing action library in place. Newly rendered
        # actions overwrite matching files, while a locked action that vanished from the
        # source can still keep its last known-good GIF/path instead of being destroyed.
        if existing_target is not None and action_dir.exists() and not preserve_existing:
            shutil.rmtree(action_dir, ignore_errors=True)
        action_dir.mkdir(exist_ok=True)

        exported: Dict[int, str] = {}
        needed_actions = {int(x) for x in mapping.values() if isinstance(x, int) and int(x) in actions}
        if export_all_actions:
            to_export = sorted(actions)
        else:
            to_export = sorted(needed_actions)
        # Safety cap: enough for very large chars without accidentally generating thousands of helpers.
        to_export = to_export[:600]
        for number in to_export:
            a = actions[number]
            out = action_dir / f"action_{number}.gif"
            try:
                if _render_action(a, sprites, out):
                    exported[number] = str(out.relative_to(target)).replace("\\", "/")
            except Exception:
                continue

        anims: Dict[str, str] = {}
        for slot, number in mapping.items():
            if number in exported:
                anims[slot] = exported[number]
        if not anims:
            raise MugenImportError("Sprites were decoded, but none of the mapped AIR actions could be rendered.")
        if "idle" not in anims:
            # Pick first rendered action as a safe universal fallback.
            first = next(iter(exported.values()))
            anims["idle"] = first

        preserved_animation_slots = []
        if preserve_existing and old_meta:
            old_anims = old_meta.get("animations", {}) if isinstance(old_meta.get("animations", {}), dict) else {}
            for slot in sorted(lock_anims):
                old_rel = str(old_anims.get(slot, "") or "")
                if old_rel and (target / old_rel).exists():
                    anims[slot] = old_rel
                    preserved_animation_slots.append(slot)

        attacks = []
        move_by_action = {int(x["action"]): x for x in deep_candidates}
        for i in range(1, 5):
            slot = f"attack_{i}"
            if slot not in anims:
                continue
            action_no = mapping.get(slot)
            move = move_by_action.get(int(action_no or -999), {})
            comment = actions.get(action_no).comment if action_no in actions else ""
            label = move.get("name") or (comment.strip(" ;-_") if comment else f"MUGEN Attack {i}")
            if len(label) > 48:
                label = f"MUGEN Attack {i}"
            kind = move.get("type", "melee")
            tier = 0 if not action_no or action_no < 1000 else 1 if action_no < 3000 else 2
            if kind == "beam":
                damage, rng, cooldown, knockback = (18 + tier*5, 430, 1.45 + tier*0.15, 14 + tier*3)
            elif kind == "projectile":
                damage, rng, cooldown, knockback = (16 + tier*4, 340, 1.25 + tier*0.14, 13 + tier*2)
            else:
                damage, rng, cooldown, knockback = (12 + tier*4 + (i-1), 68 if tier else 62, 0.95 + tier*0.18 + (i-1)*0.07, 12 + tier*3)
            attacks.append({
                "name": label or f"MUGEN Attack {i}",
                "type": kind,
                "animation": slot,
                "damage": round(damage, 2),
                "range": round(rng, 2),
                "cooldown": round(cooldown, 2),
                "knockback": round(knockback, 2),
                "effect_color": "#ffd250",
                "weight": 1.0,
                "mugen_action": action_no,
                "mugen_state": move.get("state"),
                "mugen_detection": move.get("source", "AIR"),
            })
        if preserve_existing and old_meta and lock_attacks:
            by_slot = {str(a.get("animation", "")): dict(a) for a in attacks if isinstance(a, dict)}
            for slot in lock_attacks:
                if slot in old_attacks_by_slot:
                    by_slot[slot] = dict(old_attacks_by_slot[slot])
            attack_order = {f"attack_{i}": i for i in range(1, 5)}
            attacks = sorted(by_slot.values(), key=lambda a: (attack_order.get(str(a.get("animation", "")), 99), str(a.get("name", ""))))

        if not attacks:
            attacks = [{
                "name": "Basic Strike", "type": "melee", "animation": "idle", "damage": 12,
                "range": 58, "cooldown": 1.0, "knockback": 10, "effect_color": "#ffd250", "weight": 1.0
            }]

        fighter_json = {
            "name": info.display_name,
            "franchise": franchise or "MUGEN Import",
            "stage": "",
            "native_facing": "right",
            "allow_horizontal_flip": True,
            "scale": 2.4,
            "stats": {"hp": 100, "speed": 1.0, "attack": 1.0, "defense": 1.0},
            "animations": anims,
            "attacks": attacks,
            "ai": {"profile": "auto", "retaliation": 0.55, "evasion": 0.25},
            "mugen_locks": {"animations": sorted(lock_anims), "attacks": sorted(lock_attacks)},
            "source": {
                "format": "MUGEN",
                "def_name": info.def_file.name,
                "mugen_name": info.name,
                "author": info.author,
                "sff_version": ".".join(map(str, version)),
                "source_path": str(Path(source).resolve()),
                "import_mode": "full" if export_all_actions else "quick",
            },
        }
        if preserve_existing and old_meta:
            # Rescanning should repair MUGEN mappings without wiping the user's balancing,
            # stage/form labels, facing, scale, AI, or transformation work.
            for key in ("name", "franchise", "stage", "native_facing", "allow_horizontal_flip", "scale", "stats", "ai", "transformations"):
                if key in old_meta:
                    fighter_json[key] = old_meta[key]
        (target / "fighter.json").write_text(json.dumps(fighter_json, indent=2), encoding="utf-8")

        report_lines = [
            "RUMBLE STUDIO MUGEN IMPORT REPORT",
            "=================================",
            f"Display name: {info.display_name}",
            f"MUGEN name: {info.name}",
            f"Author: {info.author}",
            f"DEF: {info.def_file.name}",
            f"SFF: {info.sff_file.name}",
            f"AIR: {info.air_file.name}",
            f"SFF version: {'.'.join(map(str, version))} ({sff_kind})",
            f"Decoded sprites: {len(sprites)}",
            f"AIR actions found: {len(actions)}",
            f"AIR actions exported: {len(exported)}",
            "",
            "AUTO-MAPPED RUMBLE STATES",
            "-------------------------",
        ]
        for slot, number in sorted(mapping.items()):
            marker = "OK" if number in exported else "FAILED TO RENDER"
            comment = actions[number].comment if number in actions else ""
            report_lines.append(f"{slot:16s} <- Action {number:5d}  {marker}  {comment}")
        report_lines += [
            "",
            "MAPPING LOCKS",
            "-------------",
            f"Locked animation slots: {', '.join(sorted(lock_anims)) or 'None'}",
            f"Locked attack slots: {', '.join(sorted(lock_attacks)) or 'None'}",
            f"Preserved animation slots: {', '.join(preserved_animation_slots) or 'None'}",
            f"Refreshed attack slots: {', '.join(refreshed_attack_slots) or 'None'}",
            "",
            "DEEP ATTACK SCAN",
            "----------------",
            *[f"Action {x['action']:5d}  score={x['score']:3d}  type={x['type']:10s}  source={x['source']:16s}  name={x['name']}" for x in deep_candidates[:120]],
            "",
            "LEGACY AIR-RANGE CANDIDATES",
            "---------------------------",
            ", ".join(str(x) for x in legacy_attack_candidates[:120]) or "None found in 200-4999 range.",
            "",
            "NOTES",
            "-----",
            "- v0.14 deep-scans CMD/CNS/AIR for move-linked animations. Locked mappings are preserved; only unlocked mappings are researched/replaced.",
            "- All successfully rendered AIR actions are in mugen_actions/. You can remap them manually in Fighter Editor.",
            "- MUGEN projectiles/helpers and CNS/CMD logic are not executed by Rumble Studio; Rumble uses its own battle engine.",
            "- Native facing defaults to RIGHT for typical MUGEN characters. Change it in Fighter Editor if the art faces left.",
        ]
        (target / "MUGEN_IMPORT_REPORT.txt").write_text("\n".join(report_lines), encoding="utf-8")

        # v0.14: persist a machine-readable index for MUGEN Move Lab.
        move_index = []
        for action_no, rel in sorted(exported.items()):
            cand = move_by_action.get(int(action_no), {})
            comment = actions[action_no].comment if action_no in actions else ""
            clean_comment = comment.strip(" ;-_") if comment else ""
            name = cand.get("name") or clean_comment or f"Action {action_no}"
            if len(name) > 80:
                name = f"Action {action_no}"
            mapped_slots = sorted(
                slot for slot, number in mapping.items()
                if isinstance(number, int) and int(number) == int(action_no)
            )
            move_index.append({
                "action": int(action_no),
                "name": name,
                "type": cand.get("type") or _infer_rumble_attack_type(name, int(action_no)),
                "score": int(cand.get("score", 0) or 0),
                "state": cand.get("state"),
                "source": cand.get("source", "AIR action"),
                "comment": comment,
                "gif": rel,
                "mapped_slots": mapped_slots,
            })
        (target / "MUGEN_MOVE_INDEX.json").write_text(
            json.dumps({
                "version": 1,
                "fighter": info.display_name,
                "author": info.author,
                "sff_kind": sff_kind,
                "moves": move_index,
            }, indent=2),
            encoding="utf-8",
        )

        report = {
            "name": info.display_name,
            "author": info.author,
            "sprite_count": len(sprites),
            "action_count": len(actions),
            "exported_count": len(exported),
            "mapping": mapping,
            "deep_attack_candidates": deep_candidates,
            "chosen_moves": chosen_moves,
            "preserved_animation_slots": preserved_animation_slots,
            "preserved_attack_slots": preserved_attack_slots,
            "refreshed_attack_slots": refreshed_attack_slots,
            "locked_animation_slots": sorted(lock_anims),
            "locked_attack_slots": sorted(lock_attacks),
            "target": str(target),
            "sff_version": version,
            "sff_kind": sff_kind,
            "move_index_count": len(move_index),
        }
        return target, report


def inspect_rar_listing_with_rarfile(path: Path) -> List[str]:
    """Optional diagnostic used by tests; listing does not require an external extractor."""
    try:
        import rarfile  # type: ignore
        return [x.filename for x in rarfile.RarFile(path).infolist()]
    except Exception:
        return []


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Import a MUGEN character into Rumble Studio")
    ap.add_argument("source", type=Path)
    ap.add_argument("--fighters", type=Path, default=Path("assets/fighters"))
    ap.add_argument("--franchise", default="MUGEN Import")
    args = ap.parse_args()
    folder, report = import_mugen_character(args.source, args.fighters, args.franchise)
    print(json.dumps(report, indent=2, default=str))
    print("Imported to", folder)
