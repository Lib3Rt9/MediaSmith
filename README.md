# MediaSmith

A Python command-line tool (and library) that shrinks and converts big image collections (wallpapers, photos, digital art, scans) for archiving, while keeping visual quality as close to the original as possible.

Today it handles images. Video and audio (via FFmpeg), AI upscaling and a desktop app are planned, see the [Roadmap](#roadmap).

## Features

- **Many input formats:** JPG, JPEG, JFIF, PNG, WebP, TIFF, BMP, GIF, AVIF, HEIC/HEIF (AVIF and HEIC need support installed, see below)
- **Output formats:** JPG (default), PNG, WebP (lossy or lossless), AVIF
- **High-quality JPG:** quality 95, 4:4:4 chroma (full colour detail), optimised encoding
- **Resize modes:** longest edge, width, height or percentage. Nothing is ever enlarged unless you pass `--upscale`
- **Target file size:** `--target-size 500KB` finds the highest quality that fits
- **Keeps metadata** (EXIF + ICC colour profile) by default; `--strip-metadata` removes it (rotation is applied first, so photos don't turn sideways)
- **Zero-loss copies:** a file already in the output format and within the size limit is copied byte for byte
- **Never makes files bigger:** if the converted file would not be smaller, the original is copied instead
- **Safe:** originals are never modified; everything goes to a separate output folder
- **`--dry-run`:** see exactly what would happen, including estimated sizes, without writing anything
- **Parallel processing:** `--workers 4`
- **Resume:** `--resume` skips files already processed
- **Failure log:** every failed file is listed with its reason, on screen and in a log file
- **JSON / CSV report** of every file
- **Progress bar** (with `tqdm`), **config file**, optional sub-folder recursion

## Install

Requires Python 3.8+.

The canonical display name is `PROJECT_NAME` in `mediasmith/identity.py`; the CLI name
and generated output filenames are derived from it in lowercase. Python packaging
requires the distribution name and installed command to be declared in `pyproject.toml`,
and the `mediasmith` import package is a compatibility-facing identifier. If you rebrand,
update the `[project.scripts]` command there as well; the import package can stay unchanged.

```bash
pip install .                # from a clone of this repo
pip install ".[all]"         # + HEIC input, AVIF output, progress bar
```

Optional extras: `heic` (pillow-heif), `avif` (pillow-avif-plugin; recent Pillow versions can write AVIF natively), `progress` (tqdm).

After installing, run `mediasmith ...`. From the repository folder you can also use
`python -m mediasmith ...`; `python media ...` does not work because there is no `media.py`
script. Start with `python -m mediasmith --help` to see all options.

## Usage

```bash
# Existing two-path form: input folder, then output folder
mediasmith INPUT_FOLDER OUTPUT_FOLDER [options]

# Prototype-style form: one or more files/folders; default output is ./output
mediasmith INPUT [INPUT ...] [options]
```

Put paths that contain spaces in quotes, for example `"D:\My Wallpapers"`.
The two-path form is kept for compatibility. To pass two inputs without ambiguity, use
`-i INPUT -i INPUT` (or `-i INPUT1 INPUT2`) and set the output with `-o`.
In this form each input folder gets its own output subfolder, while selected image files go
directly into the output folder. Repeated runs use an indexed output folder (`output_1`, etc.);
`--resume` reuses the requested output folder.

From a clone, a basic run looks like this:

```powershell
python -m mediasmith "D:\Photos" "D:\Photos-small"
# Or use the prototype-style defaults:
python -m mediasmith "D:\Photos" --recursive
```

### Options

| Option | Default | Description |
|---|---|---|
| `PATH ...` | | Input files or folders. Exactly two positional paths without `-o` retain the legacy `INPUT_FOLDER OUTPUT_FOLDER` form |
| `-i`, `--input PATH ...` | | Add input files or folders; repeat `-i` to add more |
| `--all` | off | Also include images and folders in the current directory |
| `-o`, `--output DIR` | `output` | Output directory for the flexible input form |
| `--exclude PATH_OR_NAME` | | Exclude an exact path (including folder contents) or any matching file/folder name; repeat as needed |
| `--exclude-extension EXT` | | Exclude files ending in this extension (e.g. `.tmp`, `.tar.gz`); repeat as needed |
| `--max-edge PX` | 2560 | Longer side becomes at most PX pixels |
| `--width PX` / `--height PX` | | Width / height becomes at most PX pixels |
| `--scale PERCENT` | | Set each dimension to this percentage of the original: below 100 shrinks, 100 keeps size, above 100 enlarges |
| `--upscale` | off | Permit enlargement; required for `--scale` above 100 or when a resize limit would enlarge a smaller image |
| `--format jpg\|png\|webp\|avif` | jpg | Output format |
| `--quality N` | 95 | Quality 1-100 for jpg / webp / avif |
| `--lossless` | off | Use lossless WebP encoding; PNG is inherently lossless, and this option is WebP-only |
| `--target-size SIZE` | | Aim for at most SIZE (e.g. `500KB`, `2MB`) by lowering quality |
| `--min-quality N` | 30 | Quality floor when finding a quality that fits `--target-size`; ignored otherwise |
| `--strip-metadata` | off | Remove EXIF (ICC colour profile kept) |
| `--strip-icc` | off | Also remove the ICC colour profile |
| `--recursive` | off | Also process sub-folders, keeping the structure |
| `--copy-same` | off | Copy files already in the output format untouched, even if larger than the limit |
| `--convert-only` | off | Convert formats without resizing; `--quality` and `--target-size` still apply |
| `--dry-run` | off | Show what would happen; write nothing |
| `--resume` | off | Skip files already processed with the same options |
| `--workers N` | 1 | Worker threads (0 = all CPU cores) |
| `--log FILE` | `mediasmith.log` in output | Human-readable run summary and failures |
| `--report FILE` | | Per-file report (`.json` or `.csv`); JSON includes the complete effective setup |
| `--config FILE` | | Read default options from a TOML or JSON file |
| `--quiet` | | Hide per-file details; progress remains visible |
| `--show-setup` | | Print the effective setup before processing |

Only one of `--max-edge`, `--width`, `--height`, `--scale` can be used at a time.
`--scale 50` makes each dimension half as large. `--scale 100` keeps the original size;
values above 100 require `--upscale`. Without `--upscale`, resize modes never enlarge images.
`--min-quality` sets the lowest encoder quality allowed while `--target-size` searches for a
quality that fits the requested size. If that floor is reached before the target is met, the
result can remain larger than the target. Without `--target-size`, `--min-quality` has no effect.
`--log` is the path override for the text log, which is already written by default (except in
dry runs). `--report` is different: it opts into a per-file CSV or JSON report; no such report
is written by default. The hidden `.mediasmith-manifest.jsonl` file is resume bookkeeping, not
a report.

### Examples

```bash
# Basic run: long edge 2560, JPG q95
mediasmith ./wallpapers ./wallpapers_small

# Flexible inputs; each folder is kept separate under ./processed
mediasmith -i ./wallpapers ./photos -o ./processed --recursive

# Convert a single image without resizing
mediasmith ./photo.png --output ./converted --convert-only

# Half size, WebP, sub-folders, 4 threads
mediasmith ./photos ./photos_small --scale 50 --format webp --recursive --workers 4

# Every file at most 500 KB, EXIF removed
mediasmith ./photos ./web --target-size 500KB --strip-metadata

# Preview first, then do it and keep a CSV report
mediasmith ./photos ./out --dry-run
mediasmith ./photos ./out --report ./out/report.csv

# Large collection: safe to stop with Ctrl-C and continue later
mediasmith ./photos ./out --recursive --workers 0 --resume
```

### Config file

Options can live in a TOML (or JSON) file. Keys are the long option names. Anything typed on the command line wins over the file.

```toml
# mediasmith.toml
max-edge = 1920
quality = 92
format = "webp"
recursive = true
workers = 4
strip-metadata = true
```

```bash
mediasmith ./photos ./out --config mediasmith.toml
```

TOML needs Python 3.11+ or `pip install tomli` (installed automatically with the package on older Pythons).

### Resume

Finished files are recorded in `.mediasmith-manifest.jsonl` inside the output folder, one line per file as soon as it is done. With `--resume` a file is skipped when its source is unchanged, it was processed with the same options, and its output still exists. Failed files are retried. If you change options, use a fresh output folder to avoid duplicates.

### Reading the result

- **Failures:** printed at the end with the reason, and written to the log. The exit code is `2` when any file failed (`0` = all fine, `1` = bad arguments, `130` = interrupted).
- **Report:** JSON contains options, a summary and every file (status, sizes in bytes and pixels, quality, error). CSV contains one row per file.

## How it works

| Input | Result |
|---|---|
| Other format (PNG, WebP, TIFF, BMP, GIF, HEIC...) | Resized if needed and saved in the output format |
| Already in the output format, within the size limit | Copied untouched |
| Already in the output format, larger than the limit | Resized and re-saved (or copied with `--copy-same`) |
| `.jfif` / `.jpe` | Same as JPG, saved with a `.jpg` extension |
| Result not smaller than the original | Original copied instead |
| Transparent image to JPG | Flattened onto white (PNG, WebP and AVIF keep transparency) |
| Animated GIF / WebP | Only the first frame is converted |

Resizing uses the Lanczos filter. Note that `--strip-metadata`, `--strip-icc` and `--target-size` always re-encode, so a JPG that would otherwise be copied untouched gets re-saved.

## A note on quality

JPEG, WebP lossy and AVIF are lossy formats, so converting from another format can never be truly lossless. JPG quality 95 with 4:4:4 chroma is visually indistinguishable from the original for most images and is a good balance for archiving. WebP lossy is limited to 4:2:0 chroma; JPG is the most compatible choice. Re-encoding an already low-quality JPG never improves it, which is why the tool keeps the original when the new file would be bigger. HEIC is supported as input only.

## Library use

```python
from pathlib import Path
from mediasmith.core import Context, Job, Options, PathReserver, process_file

opts = Options(fmt="webp", resize_mode="width", resize_value=1920, quality=90)
opts.validate()
ctx = Context(dst_dir=Path("out"), opts=opts, reserver=PathReserver())
result = process_file(Job(order=0, src=Path("a.png"), rel=Path("a.png"), number=1), ctx)
print(result.status, result.dst, result.bytes_in, result.bytes_out, result.error)
```

## Development

```bash
pip install -e ".[dev,all]"
pytest
```

## Roadmap

### Complete

- [x] Core image conversion library
- [x] Installable CLI
- [x] Flexible file/folder inputs
- [x] Resize, quality, format and target-size controls
- [x] Dry-run, resume, parallel processing and reports
- [x] Configuration and exclusions
- [x] Packaging and automated tests

### Next

- [ ] Public, stable Python library API
- [ ] Improved error handling and recoverability
- [ ] Thumbnail previews
- [ ] Preserve directory metadata
- [ ] Plugin or FFmpeg support for video and audio
- [ ] Upscaling
- [ ] Desktop GUI (?)
- [ ] Android app (?)

## Contributing

Issues and pull requests are welcome.

## License

MIT, see [LICENSE](LICENSE).
