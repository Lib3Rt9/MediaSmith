import csv
import json
import sys

import pytest
from PIL import Image

from mediasmith.cli import DEFAULT_LOG_NAME, build_parser, main
from mediasmith.identity import COMMAND_NAME, PROJECT_NAME
from mediasmith.resume import MANIFEST_NAME

from conftest import make_noise


def populate(src):
    (src / "sub").mkdir()
    make_noise(src / "big.png", (3000, 2000))
    make_noise(src / "small.jpg", (400, 300), quality=95)
    make_noise(src / "sub" / "deep.png", (2800, 1800))
    (src / "bad.png").write_bytes(b"corrupt")
    (src / "notes.txt").write_text("hi")


def files_in(path):
    return sorted(p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file())


def test_project_identity_drives_cli_and_generated_names(capsys):
    assert build_parser().prog == COMMAND_NAME == PROJECT_NAME.lower()
    assert DEFAULT_LOG_NAME == f"{COMMAND_NAME}.log"
    assert MANIFEST_NAME == f".{COMMAND_NAME}-manifest.jsonl"
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith(f"{COMMAND_NAME} ")


def test_end_to_end_with_failure_log_and_reports(folders):
    src, dst = folders
    populate(src)
    code = main([str(src), str(dst), "--recursive", "--no-progress",
                 "--report", str(dst / "r.json")])
    assert code == 2  # one file failed
    names = files_in(dst)
    assert "big.jpg" in names and "small.jpg" in names and "sub/deep.jpg" in names
    log = (dst / DEFAULT_LOG_NAME).read_text()
    assert log.startswith(f"{PROJECT_NAME} ")
    assert "Failed files (1)" in log and "bad.png" in log and "UnidentifiedImageError" in log
    doc = json.loads((dst / "r.json").read_text())
    assert doc["summary"]["failed"] == 1 and doc["summary"]["converted"] == 2
    statuses = {f["relative_path"]: f["status"] for f in doc["files"]}
    assert statuses["bad.png"] == "error" and "notes.txt" not in statuses


def test_csv_report(folders):
    src, dst = folders
    populate(src)
    main([str(src), str(dst), "--no-progress", "--report", str(dst / "r.csv")])
    rows = list(csv.DictReader((dst / "r.csv").open(encoding="utf-8")))
    assert {r["relative_path"] for r in rows} >= {"big.png", "small.jpg", "bad.png"}
    assert [r for r in rows if r["status"] == "error"][0]["error"]


def test_custom_log_path(folders, tmp_path):
    src, dst = folders
    populate(src)
    main([str(src), str(dst), "--no-progress", "--log", str(tmp_path / "my.log")])
    assert (tmp_path / "my.log").exists() and not (dst / DEFAULT_LOG_NAME).exists()


def test_dry_run_creates_nothing(folders):
    src, dst = folders
    populate(src)
    code = main([str(src), str(dst), "--recursive", "--dry-run", "--no-progress"])
    assert code == 2 and not dst.exists()


def test_originals_never_modified(folders):
    src, dst = folders
    populate(src)
    before = {p: p.read_bytes() for p in src.rglob("*") if p.is_file()}
    main([str(src), str(dst), "--recursive", "--no-progress"])
    assert before == {p: p.read_bytes() for p in src.rglob("*") if p.is_file()}


def test_resume_skips_finished_files(folders):
    src, dst = folders
    populate(src)
    args = [str(src), str(dst), "--recursive", "--no-progress", "--resume", "--quiet"]
    main(args)
    first = files_in(dst)
    assert main(args) == 2                      # bad.png still fails, nothing else redone
    assert files_in(dst) == first               # no "_1" duplicates
    make_noise(src / "new.png", (2600, 1700))   # a new file is picked up
    main(args)
    assert "new.jpg" in files_in(dst)


def test_resume_after_interrupt_style_partial_manifest(folders):
    src, dst = folders
    make_noise(src / "a.png", (3000, 2000))
    main([str(src), str(dst), "--no-progress", "--quiet"])
    manifest = dst / MANIFEST_NAME
    with manifest.open("a") as fh:
        fh.write('{"half written')           # torn last line must not break loading
    assert main([str(src), str(dst), "--no-progress", "--quiet", "--resume"]) == 0
    assert files_in(dst).count("a.jpg") == 1 and "a_1.jpg" not in files_in(dst)


def test_parallel_matches_sequential(folders, tmp_path):
    src, dst = folders
    for i in range(6):
        make_noise(src / f"p{i}.png", (2800, 1800))
    main([str(src), str(dst), "--no-progress", "--quiet", "--jobs", "1"])
    dst2 = tmp_path / "out2"
    main([str(src), str(dst2), "--no-progress", "--quiet", "--jobs", "4"])
    assert files_in(dst) == files_in(dst2)


def test_parallel_name_clash_is_safe(folders):
    src, dst = folders
    for ext in ("png", "bmp", "tiff"):
        make_noise(src / f"same.{ext}", (2800, 1800))
    main([str(src), str(dst), "--no-progress", "--quiet", "--jobs", "3"])
    assert sorted(n for n in files_in(dst) if n.endswith(".jpg")) == \
        ["same.jpg", "same_1.jpg", "same_2.jpg"]


def test_rename_and_format(folders):
    src, dst = folders
    make_noise(src / "a.png", (3000, 2000))
    make_noise(src / "b.png", (3000, 2000))
    main([str(src), str(dst), "--no-progress", "--quiet", "--format", "webp",
          "--rename", "wall_{n:03d}"])
    assert [n for n in files_in(dst) if n.endswith(".webp")] == ["wall_001.webp", "wall_002.webp"]


def test_width_option_and_conflict(folders, capsys):
    src, dst = folders
    make_noise(src / "a.png", (3000, 2000))
    main([str(src), str(dst), "--no-progress", "--quiet", "--width", "1000"])
    with Image.open(dst / "a.jpg") as im:
        assert im.size == (1000, 667)
    with pytest.raises(SystemExit):   # argparse rejects two resize options
        main([str(src), str(dst), "--width", "10", "--height", "10"])


def test_bad_arguments_return_1(folders, capsys):
    src, dst = folders
    assert main([str(src / "missing"), str(dst)]) == 1
    assert main([str(src), str(dst), "--quality", "500"]) == 1
    assert main([str(src), str(dst), "--scale", "200"]) == 1
    assert main([str(src), str(dst), "--rename", "{bogus}"]) == 1
    assert main([str(src), str(dst), "--report", "x.txt"]) == 1
    assert main([str(src), str(src)]) == 1
    assert "error:" in capsys.readouterr().err


@pytest.mark.skipif(sys.version_info < (3, 11), reason="TOML config needs 3.11+ or tomli")
def test_config_file_and_cli_override(folders, tmp_path):
    src, dst = folders
    make_noise(src / "a.png", (3000, 2000))
    cfg = tmp_path / "ms.toml"
    cfg.write_text('max-edge = 1000\nquality = 80\nformat = "webp"\n')
    main([str(src), str(dst), "--no-progress", "--quiet", "--config", str(cfg)])
    with Image.open(dst / "a.webp") as im:
        assert im.size == (1000, 667)
    dst2 = tmp_path / "out2"
    main([str(src), str(dst2), "--no-progress", "--quiet", "--config", str(cfg), "--width", "600"])
    with Image.open(dst2 / "a.webp") as im:
        assert im.size == (600, 400)   # CLI size option beats the config's max-edge


def test_json_config_and_unknown_key(folders, tmp_path):
    src, dst = folders
    make_noise(src / "a.png", (3000, 2000))
    cfg = tmp_path / "ms.json"
    cfg.write_text('{"max_edge": 800}')
    main([str(src), str(dst), "--no-progress", "--quiet", "--config", str(cfg)])
    with Image.open(dst / "a.jpg") as im:
        assert max(im.size) == 800
    cfg.write_text('{"nonsense": 1}')
    assert main([str(src), str(dst), "--config", str(cfg)]) == 1


def test_target_size_via_cli(folders):
    src, dst = folders
    make_noise(src / "a.png", (1200, 800))
    main([str(src), str(dst), "--no-progress", "--quiet", "--target-size", "300KB",
          "--min-quality", "5"])
    assert (dst / "a.jpg").stat().st_size <= 300 * 1024
