#!/usr/bin/env python3
"""
Shrink a folder of wallpapers/images for archiving.

What it does
------------
- Reads every image in the input folder (jpg, jpeg, jfif, png, webp, tiff, bmp, gif, heic).
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

Requirements:  pip install pillow
Optional (HEIC/HEIF input):  pip install pillow-heif

Usage:
    python wallpaper_to_jpg.py INPUT_FOLDER OUTPUT_FOLDER
    python wallpaper_to_jpg.py INPUT_FOLDER OUTPUT_FOLDER --max-edge 1920 --recursive
    python wallpaper_to_jpg.py INPUT_FOLDER OUTPUT_FOLDER --copy-jpg
"""

import argparse
import shutil
import sys
from pathlib import Path

from PIL import Image

Image.MAX_IMAGE_PIXELS = None  # don't reject very large images

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

JPEG_EXTS = {".jpg", ".jpeg", ".jfif", ".jpe"}
OTHER_EXTS = {".png", ".bmp", ".tif", ".tiff", ".webp", ".gif", ".heic", ".heif"}


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


def target_size(width: int, height: int, max_edge: int):
    """Return (new_w, new_h) with long edge <= max_edge, aspect ratio kept. No upscaling."""
    long_edge = max(width, height)
    if long_edge <= max_edge:
        return width, height
    scale = max_edge / long_edge
    return max(1, round(width * scale)), max(1, round(height * scale))


def save_as_jpg(src: Path, dst: Path, max_edge: int, quality: int) -> tuple:
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


def main() -> int:
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
    args = p.parse_args()

    src_dir = args.input_folder.resolve()
    dst_dir = args.output_folder.resolve()

    if not src_dir.is_dir():
        print(f"Input folder not found: {src_dir}")
        return 1
    if src_dir == dst_dir:
        print("Output folder must be different from the input folder.")
        return 1

    dst_dir.mkdir(parents=True, exist_ok=True)

    candidates = src_dir.rglob("*") if args.recursive else src_dir.iterdir()
    files = sorted(f for f in candidates if f.is_file() and dst_dir not in f.parents)

    copied = converted = resized = skipped = failed = kept_original = 0
    in_bytes = out_bytes = 0

    for f in files:
        ext = f.suffix.lower()
        rel = f.relative_to(src_dir)

        if ext not in JPEG_EXTS and ext not in OTHER_EXTS:
            skipped += 1
            continue

        out_dir = dst_dir / rel.parent
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            if ext in JPEG_EXTS:
                # Decide: copy untouched, or resize because it's too large
                needs_resize = False
                if not args.copy_jpg:
                    with Image.open(f) as im:
                        needs_resize = max(im.size) > args.max_edge

                if not needs_resize:
                    # .jfif / .jpe are plain JPEG data with an unusual extension:
                    # copy byte-for-byte but save as .jpg for universal compatibility.
                    out_name = f.stem + ".jpg" if ext in {".jfif", ".jpe"} else f.name
                    target = unique_path(out_dir / out_name)
                    shutil.copy2(f, target)
                    copied += 1
                    in_bytes += f.stat().st_size
                    out_bytes += target.stat().st_size
                    print(f"[COPY]    {rel}" + (f" -> {target.name}" if target.name != f.name else ""))
                    continue

            # Convert / resize to JPG (same base name)
            target = unique_path(out_dir / (f.stem + ".jpg"))
            tmp = target.with_name(target.name + ".tmp")
            old_sz, new_sz = save_as_jpg(f, tmp, args.max_edge, args.quality)

            # If the new file is not smaller than the original, keep the original
            # (copied untouched) instead of making a bigger file.
            if tmp.stat().st_size >= f.stat().st_size:
                new_bytes = tmp.stat().st_size
                tmp.unlink()
                out_name = f.stem + ".jpg" if ext in {".jfif", ".jpe"} else f.name
                target = unique_path(out_dir / out_name)
                shutil.copy2(f, target)
                kept_original += 1
                in_bytes += f.stat().st_size
                out_bytes += target.stat().st_size
                print(f"[KEEP]    {rel}  new version would be bigger "
                      f"({f.stat().st_size / 1024:.0f} KB -> {new_bytes / 1024:.0f} KB), original copied")
                continue

            tmp.replace(target)
            converted += 1
            if new_sz != old_sz:
                resized += 1
            in_bytes += f.stat().st_size
            out_bytes += target.stat().st_size
            print(f"[CONVERT] {rel} -> {target.name}  "
                  f"{old_sz[0]}x{old_sz[1]} -> {new_sz[0]}x{new_sz[1]}  "
                  f"({f.stat().st_size / 1024:.0f} KB -> {target.stat().st_size / 1024:.0f} KB)")

        except Exception as e:
            failed += 1
            print(f"[ERROR]   {rel}: {e}")

    print("\n--- Done ---")
    print(f"Converted/re-saved: {converted}  (of which resized: {resized})")
    print(f"Copied untouched JPG: {copied}")
    print(f"Original kept because the new file would be bigger: {kept_original}")
    print(f"Skipped (non-image files): {skipped}")
    print(f"Failed: {failed}")
    print(f"Total size: {in_bytes / 1048576:.1f} MB -> {out_bytes / 1048576:.1f} MB "
          f"(saved {(in_bytes - out_bytes) / 1048576:.1f} MB)")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
