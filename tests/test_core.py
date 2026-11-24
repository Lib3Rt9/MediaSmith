from pathlib import Path

import pytest
from PIL import Image

from mediasmith.core import (CONVERTED, COPIED, ERROR, KEPT_ORIGINAL, SKIPPED, Context, Job,
                             Options, PathReserver, compute_size, parse_size, process_file)

from conftest import make_noise, make_smooth


def run(src, dst, opts=None, number=1):
    opts = opts or Options()
    ctx = Context(dst_dir=dst, opts=opts, reserver=PathReserver())
    return process_file(Job(order=0, src=src, rel=Path(src.name), number=number), ctx)


# ---- sizes ---------------------------------------------------------------- #

@pytest.mark.parametrize("mode,value,upscale,expected", [
    ("max_edge", 2560, False, (2560, 1707)),
    ("max_edge", 4000, False, (3000, 2000)),     # never upscales by default
    ("max_edge", 4000, True, (4000, 2667)),
    ("width", 1500, False, (1500, 1000)),
    ("height", 500, False, (750, 500)),
    ("scale", 50, False, (1500, 1000)),
    ("scale", 200, True, (6000, 4000)),
    ("scale", 100, False, (3000, 2000)),
])
def test_compute_size(mode, value, upscale, expected):
    assert compute_size(3000, 2000, mode, value, upscale) == expected


def test_compute_size_portrait_long_edge():
    assert compute_size(2000, 3000, "max_edge", 1500, False) == (1000, 1500)


@pytest.mark.parametrize("text,expected", [
    ("500KB", 512000), ("2MB", 2097152), ("1.5mb", 1572864),
    ("300000", 300000), ("1 MiB", 1048576),
])
def test_parse_size(text, expected):
    assert parse_size(text) == expected


def test_parse_size_rejects_garbage():
    with pytest.raises(ValueError):
        parse_size("big")


# ---- options -------------------------------------------------------------- #

@pytest.mark.parametrize("kwargs", [
    dict(quality=0), dict(quality=101), dict(resize_value=0),
    dict(resize_mode="scale", resize_value=150),          # needs --upscale
    dict(lossless=True),                                   # jpg + lossless
    dict(fmt="png", target_bytes=1000),                    # lossless format
    dict(fmt="webp", lossless=True, target_bytes=1000),
])
def test_invalid_options(kwargs):
    with pytest.raises(ValueError):
        Options(**kwargs).validate()


def test_fingerprint_ignores_dry_run_but_not_settings():
    assert Options().fingerprint() == Options(dry_run=True).fingerprint()
    assert Options().fingerprint() != Options(quality=90).fingerprint()


# ---- processing ----------------------------------------------------------- #

def test_png_resized_to_jpg(tmp_path):
    src = make_noise(tmp_path / "big.png", (3000, 2000))
    res = run(src, tmp_path / "out")
    assert res.status == CONVERTED
    assert res.dst.name == "big.jpg"
    assert res.new_size == (2560, 1707)
    with Image.open(res.dst) as im:
        assert im.size == (2560, 1707) and im.format == "JPEG"
    assert res.bytes_out < res.bytes_in
    assert not list((tmp_path / "out").glob("*.tmp"))


def test_small_jpg_is_copied_byte_for_byte(tmp_path):
    src = make_noise(tmp_path / "a.jpg", (400, 300), quality=95)
    res = run(src, tmp_path / "out")
    assert res.status == COPIED
    assert res.dst.read_bytes() == src.read_bytes()


def test_jfif_extension_is_normalized_to_jpg(tmp_path):
    src = make_noise(tmp_path / "a.jfif", (100, 100), fmt="JPEG")
    res = run(src, tmp_path / "out")
    assert res.status == COPIED and res.dst.name == "a.jpg"


def test_copy_same_keeps_oversized_jpg(tmp_path):
    src = make_noise(tmp_path / "huge.jpg", (3000, 2000), quality=90)
    res = run(src, tmp_path / "out", Options(copy_same=True))
    assert res.status == COPIED and res.dst.read_bytes() == src.read_bytes()


def test_original_kept_when_result_not_smaller(tmp_path):
    src = tmp_path / "flat.png"
    Image.new("RGB", (300, 300), (10, 200, 30)).save(src)
    res = run(src, tmp_path / "out")
    assert res.status == KEPT_ORIGINAL
    assert res.dst.name == "flat.png"
    assert res.dst.read_bytes() == src.read_bytes()


def _transparent_noise_png(path, size=(800, 800)):
    """Noisy colour on the top half, fully transparent bottom half (big PNG, so JPG wins)."""
    import os
    w, h = size
    rgba = Image.new("RGBA", size, (255, 0, 0, 0))
    top = Image.frombytes("RGBA", (w, h // 2), os.urandom(w * (h // 2) * 4))
    top.putalpha(255)
    rgba.paste(top, (0, 0))
    rgba.save(path)
    return path


def test_alpha_survives_png_to_webp(tmp_path):
    src = _transparent_noise_png(tmp_path / "alpha.png")
    res = run(src, tmp_path / "out", Options(fmt="webp", quality=80))
    assert res.status == CONVERTED
    with Image.open(res.dst) as im:
        assert im.format == "WEBP" and "A" in im.mode
        assert im.convert("RGBA").getpixel((400, 700))[3] == 0  # still transparent


def test_transparency_flattened_on_white_for_jpg(tmp_path):
    src = _transparent_noise_png(tmp_path / "clear.png")
    res = run(src, tmp_path / "out")
    assert res.status == CONVERTED
    with Image.open(res.dst) as im:
        r, g, b = im.convert("RGB").getpixel((400, 700))
        assert min(r, g, b) > 240  # white, not black or red


def test_corrupt_file_is_an_error_not_a_crash(tmp_path):
    src = tmp_path / "bad.png"
    src.write_bytes(b"not a png")
    res = run(src, tmp_path / "out")
    assert res.status == ERROR and "UnidentifiedImageError" in res.error
    assert str(src) not in res.error
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


def test_non_image_skipped(tmp_path):
    src = tmp_path / "notes.txt"
    src.write_text("hi")
    assert run(src, tmp_path / "out").status == SKIPPED


def test_dry_run_writes_nothing_but_reports_result(tmp_path):
    src = make_noise(tmp_path / "big.png", (3000, 2000))
    out = tmp_path / "out"
    res = run(src, out, Options(dry_run=True))
    assert res.status == CONVERTED and res.dst.name == "big.jpg" and res.bytes_out > 0
    assert not out.exists()


def test_name_clash_gets_suffix(tmp_path):
    a = make_noise(tmp_path / "pic.png", (800, 800))
    b = make_noise(tmp_path / "pic.bmp", (800, 800))
    out = tmp_path / "out"
    ctx = Context(dst_dir=out, opts=Options(), reserver=PathReserver())
    r1 = process_file(Job(0, a, Path(a.name), 1), ctx)
    r2 = process_file(Job(1, b, Path(b.name), 2), ctx)
    assert {r1.dst.name, r2.dst.name} == {"pic.jpg", "pic_1.jpg"}


def test_other_output_formats(tmp_path):
    src = make_smooth(tmp_path / "s.png", (800, 600))
    for fmt, pil in (("webp", "WEBP"), ("png", "PNG"), ("jpg", "JPEG")):
        res = run(src, tmp_path / f"out_{fmt}", Options(fmt=fmt, quality=80))
        if res.status == CONVERTED:
            with Image.open(res.dst) as im:
                assert im.format == pil


def test_same_format_png_copied(tmp_path):
    src = make_noise(tmp_path / "p.png", (200, 200))
    res = run(src, tmp_path / "out", Options(fmt="png"))
    assert res.status == COPIED and res.dst.read_bytes() == src.read_bytes()


def test_lossless_webp(tmp_path):
    src = make_smooth(tmp_path / "s.png", (400, 300))
    res = run(src, tmp_path / "out", Options(fmt="webp", lossless=True))
    assert res.status in (CONVERTED, KEPT_ORIGINAL)
    if res.status == CONVERTED:
        with Image.open(src) as a, Image.open(res.dst) as b:
            assert a.convert("RGB").tobytes() == b.convert("RGB").tobytes()


def test_upscale_and_scale(tmp_path):
    src = make_noise(tmp_path / "s.png", (400, 300))
    res = run(src, tmp_path / "out", Options(resize_mode="max_edge", resize_value=800,
                                             upscale=True))
    assert res.status in (CONVERTED, KEPT_ORIGINAL)
    if res.status == CONVERTED:
        assert res.new_size == (800, 600)


def test_target_size(tmp_path):
    src = make_smooth(tmp_path / "s.png", (900, 600))
    full = run(src, tmp_path / "full", Options(quality=95))
    target = full.bytes_out // 2
    res = run(src, tmp_path / "small", Options(target_bytes=target, min_quality=5))
    assert res.status == CONVERTED and res.target_met is True
    assert res.bytes_out <= target
    assert res.quality < 95


def test_target_size_unreachable_is_flagged(tmp_path):
    src = make_noise(tmp_path / "n.png", (600, 600))
    res = run(src, tmp_path / "out", Options(target_bytes=1000, min_quality=30))
    assert res.status == CONVERTED and res.target_met is False and res.quality == 30


def test_strip_metadata_applies_rotation_and_drops_exif(tmp_path):
    src = tmp_path / "rot.jpg"
    img = Image.frombytes("RGB", (300, 200), __import__("os").urandom(300 * 200 * 3))
    exif = Image.Exif()
    exif[0x0112] = 6   # rotate 90 degrees on display
    exif[0x010F] = "TestCam"
    img.save(src, exif=exif, quality=100)
    res = run(src, tmp_path / "out", Options(strip_metadata=True, quality=90))
    assert res.status == CONVERTED
    with Image.open(res.dst) as im:
        assert im.size == (200, 300)            # rotation was applied
        assert len(im.getexif()) == 0            # and EXIF is gone


def test_exif_kept_by_default(tmp_path):
    src = tmp_path / "e.jpg"
    img = Image.frombytes("RGB", (3000, 2000), __import__("os").urandom(3000 * 2000 * 3))
    exif = Image.Exif()
    exif[0x010F] = "TestCam"
    img.save(src, exif=exif, quality=100)     # big source, so the q80 re-encode is smaller
    res = run(src, tmp_path / "out", Options(quality=80))
    assert res.status == CONVERTED
    with Image.open(res.dst) as im:
        assert im.getexif().get(0x010F) == "TestCam"


def test_output_name_is_preserved(tmp_path):
    sub = tmp_path / "holiday"
    sub.mkdir()
    src = make_noise(sub / "IMG_1.png", (3000, 2000))
    ctx = Context(dst_dir=tmp_path / "out", opts=Options(), reserver=PathReserver())
    res = process_file(Job(0, src, Path("holiday/IMG_1.png"), 7), ctx)
    assert res.dst.name == "IMG_1.jpg"
    assert res.dst.parent.name == "holiday"
