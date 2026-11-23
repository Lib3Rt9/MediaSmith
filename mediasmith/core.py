"""
Image processing core.

No printing and no argparse in here, so it can be used as a library:

    from mediasmith.core import Options, Context, Job, PathReserver, process_file
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from PIL import Image, ImageFile, ImageOps

Image.MAX_IMAGE_PIXELS = None  # don't reject very large images

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass

try:
    import pillow_avif  # noqa: F401  (registers AVIF on Pillow versions without native AVIF)
except ImportError:
    pass

LANCZOS = getattr(Image, "Resampling", Image).LANCZOS

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

JPEG_EXTS = {".jpg", ".jpeg", ".jfif", ".jpe"}
OTHER_EXTS = {".png", ".bmp", ".tif", ".tiff", ".webp", ".gif", ".heic", ".heif", ".avif"}
INPUT_EXTS = JPEG_EXTS | OTHER_EXTS
RENAME_EXTS = {".jfif", ".jpe"}  # plain JPEG data with an unusual extension -> saved as .jpg

# Result statuses
COPIED = "copied"                # same-format file copied untouched
CONVERTED = "converted"          # re-encoded (maybe resized)
KEPT_ORIGINAL = "kept_original"  # re-encode would not be smaller, original copied
ALREADY_DONE = "already_done"    # skipped by --resume
SKIPPED = "skipped"              # not an image we handle
ERROR = "error"                  # something went wrong, see .error

SIZE_STATUSES = (COPIED, CONVERTED, KEPT_ORIGINAL, ALREADY_DONE)

RESIZE_MODES = ("max_edge", "width", "height", "scale")
TEMPLATE_FIELDS = ("name", "n", "date", "parent", "width", "height", "orig_ext", "format")


@dataclass(frozen=True)
class FormatInfo:
    key: str
    ext: str
    pil_name: str
    lossy: bool          # has a quality setting
    keeps_alpha: bool    # False -> transparency is flattened onto white
    same_exts: frozenset  # input extensions that count as "already this format"


FORMATS: Dict[str, FormatInfo] = {
    "jpg": FormatInfo("jpg", ".jpg", "JPEG", True, False, frozenset(JPEG_EXTS)),
    "png": FormatInfo("png", ".png", "PNG", False, True, frozenset({".png"})),
    "webp": FormatInfo("webp", ".webp", "WEBP", True, True, frozenset({".webp"})),
    "avif": FormatInfo("avif", ".avif", "AVIF", True, True, frozenset({".avif"})),
}


def avif_supported() -> bool:
    Image.init()
    return "AVIF" in Image.SAVE


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmg]?)(?:i?b)?\s*$", re.I)


def parse_size(text) -> int:
    """'500KB' -> 512000, '2MB' -> 2097152, '300000' -> 300000 (binary units)."""
    m = _SIZE_RE.match(str(text))
    if not m:
        raise ValueError(f"Cannot read size {text!r} (examples: 500KB, 2MB, 1.5MB, 300000)")
    mult = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2).lower()]
    return int(float(m.group(1)) * mult)


def _sample_fields() -> dict:
    return dict(name="photo", n=1, date="20260101", parent="folder",
                width=100, height=100, orig_ext="png", format="jpg")


def check_template(template: str) -> None:
    try:
        template.format(**_sample_fields())
    except (KeyError, IndexError, ValueError, AttributeError, TypeError) as e:
        raise ValueError(f"Bad --rename template {template!r}: {e!r}. "
                         f"Available fields: {', '.join('{' + f + '}' for f in TEMPLATE_FIELDS)}")


@dataclass
class Options:
    fmt: str = "jpg"
    resize_mode: str = "max_edge"      # max_edge | width | height | scale
    resize_value: float = 2560
    upscale: bool = False
    quality: int = 95
    min_quality: int = 30              # floor used by target_bytes
    lossless: bool = False             # WebP only
    target_bytes: Optional[int] = None
    strip_metadata: bool = False       # drops EXIF (orientation is applied first)
    strip_icc: bool = False
    copy_same: bool = False
    rename: Optional[str] = None
    dry_run: bool = False

    def validate(self) -> None:
        if self.fmt not in FORMATS:
            raise ValueError(f"Unknown format {self.fmt!r} (choose from {', '.join(FORMATS)})")
        if self.resize_mode not in RESIZE_MODES:
            raise ValueError(f"Unknown resize mode {self.resize_mode!r}")
        if self.resize_value <= 0:
            raise ValueError("Resize value must be greater than 0.")
        if self.resize_mode == "scale" and self.resize_value > 100 and not self.upscale:
            raise ValueError("--scale above 100 enlarges images; add --upscale to allow that.")
        if not 1 <= self.quality <= 100:
            raise ValueError("--quality must be between 1 and 100.")
        if not 1 <= self.min_quality <= 100:
            raise ValueError("--min-quality must be between 1 and 100.")
        if self.lossless and self.fmt != "webp":
            raise ValueError("--lossless only applies to --format webp "
                             "(use --format png for lossless output).")
        if self.target_bytes is not None:
            if self.target_bytes <= 0:
                raise ValueError("--target-size must be greater than 0.")
            if not FORMATS[self.fmt].lossy or self.lossless:
                raise ValueError("--target-size needs a lossy format "
                                 "(jpg, webp without --lossless, or avif).")
        if self.rename:
            check_template(self.rename)
        if self.fmt == "avif" and not avif_supported():
            raise ValueError("AVIF output is not available: use a recent Pillow "
                             "or run `pip install pillow-avif-plugin`.")

    def fingerprint(self) -> str:
        """Short hash of every setting that changes the output (used by --resume)."""
        d = asdict(self)
        d.pop("dry_run", None)
        return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Records and helpers
# --------------------------------------------------------------------------- #

@dataclass
class FileResult:
    """Everything that happened to one input file."""
    status: str
    src: Path
    rel: Path                                   # path relative to the input folder
    order: int = 0                              # position in the sorted input list
    dst: Optional[Path] = None
    bytes_in: int = 0                           # filled for successful files only
    bytes_out: int = 0
    old_size: Optional[Tuple[int, int]] = None  # pixels
    new_size: Optional[Tuple[int, int]] = None
    candidate_bytes: int = 0                    # size the rejected re-encode would have had
    quality: Optional[int] = None               # quality actually used (lossy formats)
    target_met: Optional[bool] = None           # only set when --target-size is used
    error: Optional[str] = None


@dataclass
class Job:
    order: int
    src: Path
    rel: Path
    number: int  # 1-based counter over image files (used by {n} in --rename)


class PathReserver:
    """Hands out output paths that don't clash, even across worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._taken = set()

    def reserve(self, path: Path) -> Path:
        with self._lock:
            candidate, i = path, 1
            while candidate.exists() or os.path.normcase(str(candidate)) in self._taken:
                candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
                i += 1
            self._taken.add(os.path.normcase(str(candidate)))
            return candidate


@dataclass
class Context:
    dst_dir: Path
    opts: Options
    reserver: PathReserver
    today: str = ""

    def __post_init__(self) -> None:
        if not self.today:
            self.today = date.today().strftime("%Y%m%d")


def to_rgb(img: Image.Image) -> Image.Image:
    """Convert any Pillow mode to RGB suitable for JPEG (transparency -> white)."""
    if img.mode == "RGB":
        return img

    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background

    if img.mode.startswith("I") or img.mode == "F":  # 16/32-bit -> 8-bit first
        img = img.point(lambda p: p * (1 / 256)).convert("L")

    return img.convert("RGB")


def prepare(img: Image.Image, info: FormatInfo) -> Image.Image:
    """Bring the image into a mode the target format can store."""
    if not info.keeps_alpha:
        return to_rgb(img)
    allowed = ("RGB", "RGBA", "L", "LA") if info.key == "png" else ("RGB", "RGBA")
    if img.mode in allowed:
        return img
    if img.mode.startswith("I") or img.mode == "F":
        img = img.point(lambda p: p * (1 / 256)).convert("L")
        if img.mode in allowed:
            return img
    has_alpha = "A" in img.mode or "transparency" in img.info
    return img.convert("RGBA" if has_alpha else "RGB")


def compute_size(width: int, height: int, mode: str, value: float,
                 upscale: bool) -> Tuple[int, int]:
    """New (w, h). Aspect ratio is kept; enlarging only happens when upscale is True."""
    if mode == "max_edge":
        factor = value / max(width, height)
    elif mode == "width":
        factor = value / width
    elif mode == "height":
        factor = value / height
    elif mode == "scale":
        factor = value / 100.0
    else:
        raise ValueError(f"unknown resize mode: {mode}")
    if factor == 1 or (factor > 1 and not upscale):
        return width, height
    return max(1, round(width * factor)), max(1, round(height * factor))


def safe_filename(text: str, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip().strip(".")
    return cleaned or fallback


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #

_buffer_lock = threading.Lock()


def _ensure_jpeg_buffer(pixels: int) -> None:
    """Pillow sizes the progressive-JPEG output buffer at roughly 1-2 bytes per pixel, which is
    too small for grainy images at quality < 95 ("Suspension not allowed here" / "broken data
    stream"). Pillow uses max(ImageFile.MAXBLOCK, its own guess), so raise MAXBLOCK. The buffer
    is only allocated, not filled, so a larger value costs little memory. Only ever increased."""
    needed = 4 * pixels + 65536
    with _buffer_lock:
        if ImageFile.MAXBLOCK < needed:
            ImageFile.MAXBLOCK = needed


def encode(img: Image.Image, info: FormatInfo, opts: Options, quality: int,
           exif: Optional[bytes], icc: Optional[bytes]) -> bytes:
    kw: dict = {}
    if info.key == "jpg":
        kw.update(quality=quality, subsampling=0, optimize=True, progressive=True)  # 4:4:4
    elif info.key == "png":
        kw.update(optimize=True)
    elif info.key == "webp":
        if opts.lossless:
            kw.update(lossless=True, quality=100, method=6)
        else:
            kw.update(quality=quality, method=4)
    elif info.key == "avif":
        kw.update(quality=quality)
    if icc:
        kw["icc_profile"] = icc
    if exif:
        kw["exif"] = exif
    # Save through a real (anonymous) temp file, not BytesIO: Pillow's progressive/optimized
    # JPEG encoder can fail with "Suspension not allowed here" on grainy images when writing
    # into an in-memory buffer. A file object takes the safe encode-to-file path.
    if info.key == "jpg":
        _ensure_jpeg_buffer(img.width * img.height)
    with tempfile.TemporaryFile() as fh:
        img.save(fh, format=info.pil_name, **kw)
        fh.seek(0)
        return fh.read()


def fit_to_size(img, info, opts, exif, icc) -> Tuple[bytes, int, bool]:
    """Highest quality (between min_quality and quality) that fits opts.target_bytes."""
    target = opts.target_bytes
    hi = opts.quality
    lo = min(opts.min_quality, opts.quality)

    data = encode(img, info, opts, hi, exif, icc)
    if len(data) <= target:
        return data, hi, True
    best = encode(img, info, opts, lo, exif, icc)
    if len(best) > target:
        return best, lo, False          # can't reach the target; best effort
    best_q = lo
    while hi - lo > 1:
        mid = (lo + hi) // 2
        d = encode(img, info, opts, mid, exif, icc)
        if len(d) <= target:
            best, best_q, lo = d, mid, mid
        else:
            hi = mid
    return best, best_q, True


def render(src: Path, opts: Options, info: FormatInfo):
    """Open, resize and encode in memory. Returns (data, old_size, new_size, quality, met)."""
    with Image.open(src) as img:
        icc = img.info.get("icc_profile")
        exif = img.info.get("exif")
        if img.mode in ("CMYK", "YCbCr", "LAB", "HSV"):
            icc = None  # profile no longer matches once we convert to RGB

        work = img
        if opts.strip_metadata:
            work = ImageOps.exif_transpose(img)  # apply rotation before dropping the tag
            exif = None
        old_size = work.size

        work = prepare(work, info)
        new_size = compute_size(work.width, work.height, opts.resize_mode,
                                opts.resize_value, opts.upscale)
        if new_size != work.size:
            work = work.resize(new_size, LANCZOS)
        if opts.strip_icc:
            icc = None

        quality: Optional[int] = None
        met: Optional[bool] = None
        if info.lossy and not opts.lossless:
            quality = opts.quality
            if opts.target_bytes:
                data, quality, met = fit_to_size(work, info, opts, exif, icc)
            else:
                data = encode(work, info, opts, quality, exif, icc)
        else:
            data = encode(work, info, opts, opts.quality, exif, icc)
        return data, old_size, new_size, quality, met


# --------------------------------------------------------------------------- #
# Per-file processing
# --------------------------------------------------------------------------- #

def make_stem(job: Job, ctx: Context, size: Optional[Tuple[int, int]]) -> str:
    opts = ctx.opts
    if not opts.rename:
        return job.src.stem
    w, h = size if size else (0, 0)
    fields = dict(name=job.src.stem, n=job.number, date=ctx.today,
                  parent=job.rel.parent.name, width=w, height=h,
                  orig_ext=job.src.suffix.lower().lstrip("."), format=opts.fmt)
    return safe_filename(opts.rename.format(**fields), job.src.stem)


def _copy_original(job: Job, ctx: Context, res: FileResult, out_dir: Path,
                   status: str, src_bytes: int) -> FileResult:
    ext = job.src.suffix.lower()
    out_ext = ".jpg" if ext in RENAME_EXTS else job.src.suffix
    stem = make_stem(job, ctx, res.old_size)
    dst = ctx.reserver.reserve(out_dir / (stem + out_ext))
    if not ctx.opts.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(job.src, dst)
    res.status = status
    res.dst = dst
    res.bytes_in = res.bytes_out = src_bytes
    return res


def process_file(job: Job, ctx: Context) -> FileResult:
    """Handle one file and ALWAYS return a FileResult (never raises on bad files)."""
    opts = ctx.opts
    src = job.src
    res = FileResult(status=SKIPPED, src=src, rel=job.rel, order=job.order)
    ext = src.suffix.lower()
    if ext not in INPUT_EXTS:
        return res

    tmp: Optional[Path] = None
    try:
        info = FORMATS[opts.fmt]
        src_bytes = src.stat().st_size
        out_dir = ctx.dst_dir / job.rel.parent

        with Image.open(src) as im:
            res.old_size = im.size
        planned = compute_size(res.old_size[0], res.old_size[1], opts.resize_mode,
                               opts.resize_value, opts.upscale)

        # Same format, nothing to change -> byte-for-byte copy (zero loss)
        strips = opts.strip_metadata or opts.strip_icc
        passthrough = (
            ext in info.same_exts and not strips
            and (opts.copy_same
                 or (planned == res.old_size
                     and (opts.target_bytes is None or src_bytes <= opts.target_bytes)))
        )
        if passthrough:
            res.new_size = res.old_size
            return _copy_original(job, ctx, res, out_dir, COPIED, src_bytes)

        data, res.old_size, res.new_size, res.quality, res.target_met = render(src, opts, info)

        # Not smaller than the original -> keep the original instead
        if len(data) >= src_bytes:
            res.candidate_bytes = len(data)
            res.new_size = res.old_size
            res.quality = res.target_met = None
            return _copy_original(job, ctx, res, out_dir, KEPT_ORIGINAL, src_bytes)

        dst = ctx.reserver.reserve(out_dir / (make_stem(job, ctx, res.new_size) + info.ext))
        if not opts.dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(dst)
            tmp = None
        res.status = CONVERTED
        res.dst = dst
        res.bytes_in = src_bytes
        res.bytes_out = len(data)
        return res

    except Exception as e:  # noqa: BLE001 - every failure must be recorded, not crash the run
        res.status = ERROR
        res.dst = None
        res.bytes_in = res.bytes_out = 0
        res.error = f"{type(e).__name__}: {e}"
        return res
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def run_jobs(jobs: List[Job], ctx: Context, workers: int,
             on_result: Callable[[FileResult], None]) -> bool:
    """Process jobs (in threads when workers > 1). Returns True if interrupted by Ctrl-C."""
    if workers <= 1:
        try:
            for job in jobs:
                on_result(process_file(job, ctx))
        except KeyboardInterrupt:
            return True
        return False

    delivered = set()
    interrupted = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(process_file, job, ctx) for job in jobs]
        try:
            for fut in as_completed(futures):
                delivered.add(fut)
                on_result(fut.result())
        except KeyboardInterrupt:
            interrupted = True
            for fut in futures:
                fut.cancel()
        if interrupted:
            # let files that were already running finish, and still record them
            for fut in futures:
                if fut not in delivered and not fut.cancelled():
                    try:
                        on_result(fut.result())
                    except Exception:  # noqa: BLE001
                        pass
    return interrupted
