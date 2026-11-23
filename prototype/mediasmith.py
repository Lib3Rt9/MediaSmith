#!/usr/bin/env python3
"""
Mediasmith - shrink a folder of images for archiving.

What it does
------------
- Reads every image in the input folder (jpg, jpeg, jfif, jpe, png, webp, tiff, bmp, gif, heic).
- Resizes so the LONG edge is at most --max-edge pixels (default 2560), keeping the
  aspect ratio. Images already smaller are NEVER upscaled.
- Saves as JPG: quality 95, 4:4:4 chroma (full color detail), optimized encoding,
  EXIF + ICC color profile preserved. Same file name, new extension (.jpg).
- Existing JPGs:
    * already within the size limit -> copied byte-for-byte (zero loss);
                                       .jfif/.jpe files are renamed to .jpg
    * larger than the limit         -> resized and re-saved (use --copy-jpg to copy
                                       ALL existing JPGs untouched instead)
- If the converted file would NOT be smaller than the original, the original is
  copied unchanged instead (no bigger files, no needless quality loss).
- Output goes to a separate folder; the originals are never modified.
- Every file produces one result record. Failures are listed at the end of the run
  and written, with the reason for each, to a log file (mediasmith.log, saved in the same
  folder as this script; use --log FILE to choose another place).

Requirements:  pip install pillow
Optional (HEIC/HEIF input):  pip install pillow-heif

Usage:
    python mediasmith.py INPUT_FOLDER OUTPUT_FOLDER
    python mediasmith.py INPUT_FOLDER OUTPUT_FOLDER --max-edge 1920 --recursive
    python mediasmith.py INPUT_FOLDER OUTPUT_FOLDER --copy-jpg
    python mediasmith.py INPUT_FOLDER OUTPUT_FOLDER --log errors.log

Exit codes: 0 = all fine, 1 = bad arguments, 2 = some files failed, 130 = interrupted.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image

Image.MAX_IMAGE_PIXELS = None  # don't reject very large images

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

JPEG_EXTS = {".jpg", ".jpeg", ".jfif", ".jpe"}
OTHER_EXTS = {".png", ".bmp", ".tif", ".tiff", ".webp", ".gif", ".heic", ".heif"}
RENAME_TO_JPG = {".jfif", ".jpe"}  # plain JPEG data with an unusual extension

DEFAULT_LOG_NAME = "mediasmith.log"

# Result statuses
COPIED = "copied"              # existing JPG copied untouched
CONVERTED = "converted"        # re-encoded as JPG (maybe resized)
KEPT_ORIGINAL = "kept_original"  # new file would have been bigger, original copied
SKIPPED = "skipped"            # not an image we handle
ERROR = "error"                # something went wrong; see .error

SIZE_OK_STATUSES = (COPIED, CONVERTED, KEPT_ORIGINAL)


@dataclass
class FileResult:
    """Everything that happened to one input file."""
    status: str
    src: Path
    rel: Path                                   # path relative to the input folder
    dst: Optional[Path] = None
    bytes_in: int = 0                           # filled for successful files only
    bytes_out: int = 0
    old_size: Optional[Tuple[int, int]] = None  # pixels, when the image was opened
    new_size: Optional[Tuple[int, int]] = None
    candidate_bytes: int = 0                    # size the rejected re-encode would have had
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Image helpers
# --------------------------------------------------------------------------- #

def to_rgb(img: Image.Image) -> Image.Image:
    """Convert any Pillow mode to RGB suitable for JPEG."""
    if img.mode == "RGB":
        return img

    # Transparency: JPEG has no alpha, so flatten onto white
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background

    # 16-bit / 32-bit grayscale -> scale down to 8-bit first
    if img.mode.startswith("I"):
        img = img.point(lambda p: p * (1 / 256)).convert("L")

    return img.convert("RGB")


def target_size(width: int, height: int, max_edge: int) -> Tuple[int, int]:
    """Return (new_w, new_h) with long edge <= max_edge, aspect ratio kept. No upscaling."""
    long_edge = max(width, height)
    if long_edge <= max_edge:
        return width, height
    scale = max_edge / long_edge
    return max(1, round(width * scale)), max(1, round(height * scale))


def save_as_jpg(src: Path, dst: Path, max_edge: int, quality: int):
    """Resize (if needed) and save as JPG. Returns (old_size, new_size) in pixels."""
    with Image.open(src) as img:
        icc_profile = img.info.get("icc_profile")
        exif = img.info.get("exif")
        old_size = img.size

        rgb = to_rgb(img)
        new_size = target_size(rgb.width, rgb.height, max_edge)
        if new_size != rgb.size:
            rgb = rgb.resize(new_size, Image.LANCZOS)

        kwargs = dict(
            format="JPEG",
            quality=quality,
            subsampling=0,      # 4:4:4 -> full color resolution
            optimize=True,
            progressive=True,
        )
        if icc_profile:
            kwargs["icc_profile"] = icc_profile
        if exif:
            kwargs["exif"] = exif

        rgb.save(dst, **kwargs)
        return old_size, new_size


def unique_path(path: Path) -> Path:
    """Avoid overwriting when e.g. photo.png and photo.jpg both exist in the source."""
    if not path.exists():
        return path
    i = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


# --------------------------------------------------------------------------- #
# Per-file processing
# --------------------------------------------------------------------------- #

def copy_original(src: Path, out_dir: Path, res: FileResult, status: str) -> FileResult:
    """Copy the original byte-for-byte (renaming .jfif/.jpe to .jpg)."""
    ext = src.suffix.lower()
    out_name = src.stem + ".jpg" if ext in RENAME_TO_JPG else src.name
    target = unique_path(out_dir / out_name)
    shutil.copy2(src, target)
    res.status = status
    res.dst = target
    res.bytes_in = src.stat().st_size
    res.bytes_out = target.stat().st_size
    return res


def process_file(src: Path, rel: Path, dst_dir: Path, max_edge: int,
                 quality: int, copy_jpg: bool) -> FileResult:
    """Handle one file and ALWAYS return a FileResult (never raises)."""
    res = FileResult(status=SKIPPED, src=src, rel=rel)
    ext = src.suffix.lower()
    if ext not in JPEG_EXTS and ext not in OTHER_EXTS:
        return res

    tmp: Optional[Path] = None
    try:
        out_dir = dst_dir / rel.parent
        out_dir.mkdir(parents=True, exist_ok=True)

        if ext in JPEG_EXTS:
            # Decide: copy untouched, or resize because it's too large
            needs_resize = False
            if not copy_jpg:
                with Image.open(src) as im:
                    res.old_size = im.size
                    needs_resize = max(im.size) > max_edge
            if not needs_resize:
                return copy_original(src, out_dir, res, COPIED)

        # Convert / resize to JPG (same base name)
        target = unique_path(out_dir / (src.stem + ".jpg"))
        tmp = target.with_name(target.name + ".tmp")
        res.old_size, res.new_size = save_as_jpg(src, tmp, max_edge, quality)

        # If the new file is not smaller than the original, keep the original
        # (copied untouched) instead of making a bigger file.
        if tmp.stat().st_size >= src.stat().st_size:
            res.candidate_bytes = tmp.stat().st_size
            tmp.unlink()
            tmp = None
            res.new_size = res.old_size
            return copy_original(src, out_dir, res, KEPT_ORIGINAL)

        tmp.replace(target)
        tmp = None
        res.status = CONVERTED
        res.dst = target
        res.bytes_in = src.stat().st_size
        res.bytes_out = target.stat().st_size
        return res

    except Exception as e:  # noqa: BLE001 - we want every failure recorded, not a crash
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        res.status = ERROR
        res.dst = None
        res.bytes_in = res.bytes_out = 0
        res.error = f"{type(e).__name__}: {e}"
        return res


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def describe(r: FileResult) -> str:
    """One console line per file."""
    if r.status == COPIED:
        renamed = f" -> {r.dst.name}" if r.dst and r.dst.name != r.src.name else ""
        return f"[COPY]    {r.rel}{renamed}"
    if r.status == KEPT_ORIGINAL:
        return (f"[KEEP]    {r.rel}  new version would be bigger "
                f"({r.bytes_in / 1024:.0f} KB -> {r.candidate_bytes / 1024:.0f} KB), original copied")
    if r.status == CONVERTED:
        o, n = r.old_size, r.new_size
        return (f"[CONVERT] {r.rel} -> {r.dst.name}  "
                f"{o[0]}x{o[1]} -> {n[0]}x{n[1]}  "
                f"({r.bytes_in / 1024:.0f} KB -> {r.bytes_out / 1024:.0f} KB)")
    if r.status == ERROR:
        return f"[ERROR]   {r.rel}: {r.error}"
    return f"[SKIP]    {r.rel}"


def summarize(results: List[FileResult]) -> List[str]:
    """Summary lines, used for both the console and the log file."""
    count = lambda s: sum(1 for r in results if r.status == s)  # noqa: E731
    converted = [r for r in results if r.status == CONVERTED]
    resized = sum(1 for r in converted if r.new_size != r.old_size)
    ok = [r for r in results if r.status in SIZE_OK_STATUSES]
    in_bytes = sum(r.bytes_in for r in ok)
    out_bytes = sum(r.bytes_out for r in ok)
    return [
        f"Converted/re-saved: {len(converted)}  (of which resized: {resized})",
        f"Copied untouched JPG: {count(COPIED)}",
        f"Original kept because the new file would be bigger: {count(KEPT_ORIGINAL)}",
        f"Skipped (non-image files): {count(SKIPPED)}",
        f"Failed: {count(ERROR)}",
        f"Total size: {in_bytes / 1048576:.1f} MB -> {out_bytes / 1048576:.1f} MB "
        f"(saved {(in_bytes - out_bytes) / 1048576:.1f} MB)",
    ]


def failure_lines(results: List[FileResult]) -> List[str]:
    return [f"  {r.rel}\n      reason: {r.error}" for r in results if r.status == ERROR]


def write_log(log_path: Path, args: argparse.Namespace, src_dir: Path, dst_dir: Path,
              results: List[FileResult], started: datetime, interrupted: bool) -> None:
    """Write the run summary and every failure (with its reason) to the log file."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    finished = datetime.now()
    failures = failure_lines(results)

    lines = [
        "Mediasmith log",
        f"Started : {started:%Y-%m-%d %H:%M:%S}",
        f"Finished: {finished:%Y-%m-%d %H:%M:%S}",
        f"Input   : {src_dir}",
        f"Output  : {dst_dir}",
        f"Options : max-edge={args.max_edge} quality={args.quality} "
        f"recursive={args.recursive} copy-jpg={args.copy_jpg}",
    ]
    if interrupted:
        lines.append("NOTE: run was interrupted (Ctrl-C); the list below is incomplete.")
    lines += ["", "--- Summary ---", *summarize(results), ""]
    if failures:
        lines.append(f"--- Failed files ({len(failures)}) ---")
        lines += failures
    else:
        lines.append("No failures.")
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Resize + convert images to high-quality JPG.")
    p.add_argument("input_folder", type=Path)
    p.add_argument("output_folder", type=Path)
    p.add_argument("--max-edge", type=int, default=2560,
                   help="Max length of the longer side in pixels (default 2560)")
    p.add_argument("--quality", type=int, default=95,
                   help="JPEG quality 1-100 (default 95)")
    p.add_argument("--recursive", action="store_true",
                   help="Also process sub-folders (structure is kept)")
    p.add_argument("--copy-jpg", action="store_true",
                   help="Copy ALL existing JPGs untouched, even if larger than --max-edge")
    p.add_argument("--log", type=Path, default=None, metavar="FILE",
                   help=f"Log file with the summary and every failure + reason "
                        f"(default: {DEFAULT_LOG_NAME} in the same folder as this script)")
    return p


def main() -> int:
    args = build_parser().parse_args()

    if args.max_edge < 1:
        print("--max-edge must be at least 1.")
        return 1
    if not 1 <= args.quality <= 100:
        print("--quality must be between 1 and 100.")
        return 1

    src_dir = args.input_folder.resolve()
    dst_dir = args.output_folder.resolve()

    if not src_dir.is_dir():
        print(f"Input folder not found: {src_dir}")
        return 1
    if src_dir == dst_dir:
        print("Output folder must be different from the input folder.")
        return 1

    dst_dir.mkdir(parents=True, exist_ok=True)
    # Default: next to this script (not inside the output folder)
    log_path = (args.log or (Path(__file__).resolve().parent / DEFAULT_LOG_NAME)).resolve()

    candidates = src_dir.rglob("*") if args.recursive else src_dir.iterdir()
    files = sorted(f for f in candidates
                   if f.is_file() and dst_dir not in f.parents and f != log_path)

    started = datetime.now()
    results: List[FileResult] = []
    interrupted = False

    try:
        for f in files:
            res = process_file(f, f.relative_to(src_dir), dst_dir,
                               args.max_edge, args.quality, args.copy_jpg)
            results.append(res)
            if res.status != SKIPPED:
                print(describe(res))
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted - writing what was done so far.")

    print("\n--- Done ---")
    for line in summarize(results):
        print(line)

    failures = failure_lines(results)
    if failures:
        print(f"\n--- Failed files ({len(failures)}) ---")
        for line in failures:
            print(line)

    try:
        write_log(log_path, args, src_dir, dst_dir, results, started, interrupted)
        print(f"\nLog written to: {log_path}")
    except OSError as e:
        print(f"\nCould not write log file {log_path}: {e}")

    if interrupted:
        return 130
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
