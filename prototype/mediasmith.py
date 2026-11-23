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
- Output goes to an automatically indexed ``output`` folder in the current
  working directory, with one sub-folder per input folder; originals are never modified.
- Every file produces one result record. Failures are listed at the end of the run
  and every per-file result is written to mediasmith.log beside this script.

Requirements:  pip install pillow
Optional (HEIC/HEIF input):  pip install pillow-heif

Usage:
    python mediasmith.py -i FOLDER IMAGE.png --all
    python mediasmith.py --all -o processed
    python mediasmith.py -i FOLDER --max-edge 1920 --recursive

Exit codes: 0 = all fine, 1 = bad arguments, 2 = some files failed, 130 = interrupted.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Set, Tuple

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
    input_label: Optional[str] = None            # disambiguates files from multiple inputs


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


def save_as_jpg(src: Path, dst: Path, max_edge: int, quality: int,
                resize: bool = True):
    """Resize (if needed) and save as JPG. Returns (old_size, new_size) in pixels."""
    with Image.open(src) as img:
        icc_profile = img.info.get("icc_profile")
        exif = img.info.get("exif")
        old_size = img.size

        rgb = to_rgb(img)
        new_size = target_size(rgb.width, rgb.height, max_edge) if resize else rgb.size
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


def unique_directory(path: Path) -> Path:
    """Append an index to an existing output directory name."""
    if not path.exists():
        return path
    i = 1
    while True:
        candidate = path.with_name(f"{path.name}_{i}")
        if not candidate.exists():
            return candidate
        i += 1


def remove_empty_output_directories(root: Path) -> None:
    """Remove output directories that contain no files."""
    if not root.is_dir():
        return
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        if not any(directory.iterdir()):
            directory.rmdir()
    if not any(root.iterdir()):
        root.rmdir()


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
                 quality: int, copy_jpg: bool, convert_only: bool = False) -> FileResult:
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
            if not copy_jpg and not convert_only:
                with Image.open(src) as im:
                    res.old_size = im.size
                    needs_resize = max(im.size) > max_edge
            if not needs_resize:
                return copy_original(src, out_dir, res, COPIED)

        # Convert / resize to JPG (same base name)
        target = unique_path(out_dir / (src.stem + ".jpg"))
        tmp = target.with_name(target.name + ".tmp")
        res.old_size, res.new_size = save_as_jpg(
            src, tmp, max_edge, quality, resize=not convert_only
        )

        # If the new file is not smaller than the original, keep the original
        # (copied untouched) instead of making a bigger file.
        if not convert_only and tmp.stat().st_size >= src.stat().st_size:
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
        error_message = str(e).replace(repr(str(src)), "").rstrip()
        res.error = f"{type(e).__name__}: {error_message}"
        return res


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def describe(r: FileResult) -> str:
    """One console line per file."""
    rel = f"{r.input_label}/{r.rel}" if r.input_label else str(r.rel)
    if r.status == COPIED:
        renamed = f" -> {r.dst.name}" if r.dst and r.dst.name != r.src.name else ""
        return f"[COPY]    {rel}{renamed}"
    if r.status == KEPT_ORIGINAL:
        return (f"[KEEP]    {rel}  new version would be bigger "
                f"({r.bytes_in / 1024:.0f} KB -> {r.candidate_bytes / 1024:.0f} KB), original copied")
    if r.status == CONVERTED:
        o, n = r.old_size, r.new_size
        return (f"[CONVERT] {rel} -> {r.dst.name}  "
                f"{o[0]}x{o[1]} -> {n[0]}x{n[1]}  "
                f"({r.bytes_in / 1024:.0f} KB -> {r.bytes_out / 1024:.0f} KB)")
    if r.status == ERROR:
        return f"[ERROR]   {rel}: {r.error}"
    return f"[SKIP]    {rel}"


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
    return [
        f"  {r.input_label + '/' if r.input_label else ''}{r.rel}\n      reason: {r.error}"
        for r in results if r.status == ERROR
    ]


def describe_path_name(path: Path) -> str:
    if path.is_dir():
        kind = "folder"
    elif path.is_file():
        kind = "file"
    else:
        kind = "path"
    return f"[{kind}] {path.name or path}"


def describe_exclusion(path: Path, script_dir: Path) -> str:
    if len(path.parts) == 1:
        return f"[file/folder name] {path.name or path}"
    candidates = [path] if path.is_absolute() else [Path.cwd() / path, script_dir / path]
    target = next((candidate for candidate in candidates
                   if candidate.is_file() or candidate.is_dir()), None)
    if target is not None:
        return describe_path_name(target)
    return f"[path] {path.name or path}"


def write_log(log_path: Path, args: argparse.Namespace, input_paths: List[Path],
              skipped_inputs: List[str], empty_directories: List[Path],
              skipped_directories: List[Path], dst_dir: Path, output_created: bool,
              results: List[FileResult], started: datetime, interrupted: bool,
              script_dir: Path) -> None:
    """Write each file result, the run summary, and every failure to the log file."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    finished = datetime.now()
    failures = failure_lines(results)

    lines = [
        "Mediasmith log",
        f"Started : {started:%Y-%m-%d %H:%M:%S}",
        f"Finished: {finished:%Y-%m-%d %H:%M:%S}",
        f"Input files : {', '.join(path.name for path in input_paths if not path.is_dir()) or 'None'}",
        f"Input folders: {', '.join(path.name for path in input_paths if path.is_dir()) or 'None'}",
        f"Output  : {dst_dir}" + ("" if output_created else " (not created)"),
        f"Setup   : format=jpg max-edge={args.max_edge} quality={args.quality} "
        f"convert-only={args.convert_only} recursive={args.recursive} "
        f"copy-jpg={args.copy_jpg} exclude="
        f"{', '.join(describe_exclusion(path, script_dir) for path in args.exclude) or 'None'}",
    ]
    if skipped_inputs:
        lines += ["", f"--- Skipped input paths ({len(skipped_inputs)}) ---"]
        lines.extend(f"  {message}" for message in skipped_inputs)
    if empty_directories:
        lines += ["", f"--- Empty directories ({len(empty_directories)}) ---"]
        lines.extend(f"  [EMPTY] {path}" for path in empty_directories)
    if skipped_directories:
        lines += ["", f"--- Directories with no output files ({len(skipped_directories)}) ---"]
        lines.extend(f"  [SKIP DIR] {path}" for path in skipped_directories)
    if interrupted:
        lines.append("NOTE: run was interrupted (Ctrl-C); the list below is incomplete.")
    lines += ["", "--- Files ---", *(describe(r) for r in results),
              "", "--- Summary ---", *summarize(results), ""]
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
    p = argparse.ArgumentParser(description="Convert selected images to JPG.")
    p.add_argument("-i", "--input", dest="input_paths", type=Path, nargs="+",
                   action="append", metavar="PATH",
                   help="File(s) or folder(s); repeat -i to add more inputs")
    p.add_argument("--all", action="store_true",
                   help="Also include images and folders beside this script")
    p.add_argument("--exclude", action="append", type=Path, default=[], metavar="PATH_OR_NAME",
                   help="Exclude exact paths (including folder contents) or every file/folder "
                   "with a matching bare name; repeat as needed")
    p.add_argument("-o", "--output", dest="output_folder",
                   type=Path, default=Path("output"),
                   help="Output directory (default: output)")
    p.add_argument("--max-edge", type=int, default=2560,
                   help="Max length of the longer side in pixels (default 2560)")
    p.add_argument("--quality", type=int, default=95,
                   help="JPEG quality 1-100 (default 95)")
    p.add_argument("--recursive", action="store_true",
                   help="Also process sub-folders (structure is kept)")
    p.add_argument("--copy-jpg", action="store_true",
                   help="Copy ALL existing JPGs untouched, even if larger than --max-edge")
    p.add_argument("--convert-only", action="store_true",
                   help="Convert to JPG without resizing; --quality still controls JPEG encoding")
    return p


def main() -> int:
    parser = build_parser()
    if len(sys.argv) == 1:
        parser.print_help()
        return 0
    args = parser.parse_args()

    if args.max_edge < 1:
        print("--max-edge must be at least 1.")
        return 1
    if not 1 <= args.quality <= 100:
        print("--quality must be between 1 and 100.")
        return 1
    if not args.input_paths and not args.all:
        parser.print_help()
        return 0

    script_dir = Path(__file__).resolve().parent
    requested_dst = args.output_folder.resolve()
    if not args.output_folder.is_absolute():
        requested_dst = (Path.cwd() / args.output_folder).resolve()

    requested_paths = [path for group in (args.input_paths or []) for path in group]
    skipped_inputs: List[str] = []
    if args.all:
        all_paths = [
            path for path in sorted(script_dir.iterdir())
            if path.is_dir() and path.resolve() != requested_dst
            or path.is_file() and path.suffix.lower() in JPEG_EXTS | OTHER_EXTS
        ]
        requested_paths.extend(all_paths)
        if not all_paths:
            skipped_inputs.append("--all: no image files or folders found beside the script")

    input_paths: List[Path] = []
    seen_inputs: Set[Path] = set()
    for path in requested_paths:
        resolved_path = path.resolve()
        if resolved_path in seen_inputs:
            continue
        seen_inputs.add(resolved_path)
        input_paths.append(resolved_path)

    valid_inputs: List[Tuple[Path, bool]] = []

    exclusion_paths = [path.resolve() if path.is_absolute()
                       else (Path.cwd() / path).resolve() for path in args.exclude]

    def is_excluded(path: Path) -> bool:
        resolved = path.resolve()
        for exclusion, raw in zip(exclusion_paths, args.exclude):
            if resolved == exclusion or exclusion in resolved.parents:
                return True
            if len(raw.parts) == 1 and any(
                item.name == raw.name for item in (path, *path.parents)
            ):
                return True
            script_relative = (script_dir / raw).resolve()
            if resolved == script_relative or script_relative in resolved.parents:
                return True
        return False

    for resolved_path in input_paths:
        if is_excluded(resolved_path):
            skipped_inputs.append(f"{resolved_path}: excluded by --exclude")
            continue
        if resolved_path.is_dir():
            valid_inputs.append((resolved_path, True))
        elif resolved_path.is_file() and resolved_path.suffix.lower() in JPEG_EXTS | OTHER_EXTS:
            valid_inputs.append((resolved_path, False))
        else:
            if resolved_path.is_file():
                message = f"{resolved_path}: not a supported image file"
            else:
                message = f"{resolved_path}: not found or not a file/folder"
            if not resolved_path.exists():
                print(f"Skipping missing or invalid input: {resolved_path}")
            else:
                print(f"Skipping invalid input: {message}")
            skipped_inputs.append(message)

    log_path = (script_dir / DEFAULT_LOG_NAME).resolve()
    started = datetime.now()
    for input_path, is_dir in valid_inputs:
        if is_dir and (input_path == requested_dst or input_path in requested_dst.parents):
            print("Output folder must not be inside an input folder.")
            return 1

    if not valid_inputs:
        print("No valid inputs; output folder was not created.")
        try:
            write_log(log_path, args, input_paths, skipped_inputs, [], [],
                      requested_dst, False, [], started, False, script_dir)
            print(f"Log written to: {log_path}")
        except OSError as e:
            print(f"Could not write log file {log_path}: {e}")
        return 1

    dst_dir = unique_directory(requested_dst)
    input_outputs: List[Tuple[Path, bool, Path]] = []
    reserved_output_dirs: Set[Path] = set()
    for input_path, is_dir in valid_inputs:
        if is_dir:
            base = dst_dir / (input_path.name or "input")
            input_output = base
            index = 1
            while input_output.exists() or input_output in reserved_output_dirs:
                input_output = base.with_name(f"{base.name}_{index}")
                index += 1
            reserved_output_dirs.add(input_output)
        else:
            input_output = dst_dir
        input_outputs.append((input_path, is_dir, input_output))

    results: List[FileResult] = []
    empty_directories: Set[Path] = set()
    skipped_directories: Set[Path] = set()
    interrupted = False

    try:
        for input_path, is_dir, input_output in input_outputs:
            if is_dir:
                directories = [input_path]
                directories.extend(sorted(path for path in input_path.rglob("*")
                                         if path.is_dir() and dst_dir not in path.parents))
                candidates = input_path.rglob("*") if args.recursive else input_path.iterdir()
                files = sorted(f for f in candidates
                               if f.is_file() and dst_dir not in f.parents and f != log_path
                               and not is_excluded(f))
            else:
                files = [input_path]
                directories = []

            output_directories: Set[Path] = set()
            for f in files:
                rel = f.relative_to(input_path) if is_dir else Path(f.name)
                res = process_file(f, rel, input_output,
                                   args.max_edge, args.quality, args.copy_jpg,
                                   args.convert_only)
                if len(valid_inputs) > 1:
                    res.input_label = input_output.name if is_dir else input_path.name
                results.append(res)
                if res.status != SKIPPED:
                    print(describe(res))
                if res.dst is not None and is_dir:
                    directory = f.parent
                    while directory == input_path or input_path in directory.parents:
                        output_directories.add(directory)
                        if directory == input_path:
                            break
                        directory = directory.parent

            for directory in directories:
                if is_excluded(directory) or directory in output_directories:
                    continue
                if any(directory.iterdir()):
                    skipped_directories.add(directory)
                else:
                    empty_directories.add(directory)
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted - writing what was done so far.")

    remove_empty_output_directories(dst_dir)
    output_created = dst_dir.is_dir()
    if output_created:
        print(f"\nOutput folder: {dst_dir}")
    else:
        print(f"\nOutput folder not created: {dst_dir}")
    print("\n--- Done ---")
    for line in summarize(results):
        print(line)

    failures = failure_lines(results)
    if failures:
        print(f"\n--- Failed files ({len(failures)}) ---")
        for line in failures:
            print(line)
    for directory in sorted(empty_directories):
        print(f"[EMPTY] {directory}")
    for directory in sorted(skipped_directories):
        print(f"[SKIP DIR] {directory} (no output files)")

    try:
        write_log(log_path, args, input_paths, skipped_inputs,
                  sorted(empty_directories), sorted(skipped_directories),
                  dst_dir, output_created, results, started, interrupted,
                  script_dir)
        print(f"\nLog written to: {log_path}")
    except OSError as e:
        print(f"\nCould not write log file {log_path}: {e}")

    if interrupted:
        return 130
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
