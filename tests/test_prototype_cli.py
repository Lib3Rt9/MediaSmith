import importlib.util
import sys
from pathlib import Path

import pytest
from PIL import Image


SCRIPT = Path(__file__).parents[1] / "prototype" / "mediasmith.py"


def run_cli(module, inputs, monkeypatch, options=()):
    monkeypatch.setattr(
        sys, "argv", ["mediasmith.py", *(str(path) for path in inputs), *options]
    )
    return module.main()


@pytest.fixture
def prototype(tmp_path):
    spec = importlib.util.spec_from_file_location("prototype_mediasmith", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.__file__ = str(tmp_path / "mediasmith.py")
    return module


def test_multiple_inputs_use_indexed_output_and_log_file_results(
    prototype, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    inputs = []
    for folder in ("f1", "f2"):
        src = tmp_path / folder
        src.mkdir()
        Image.new("RGB", (20, 20), (20, 30, 40)).save(src / f"{folder}.jpg")
        (src / "notes.txt").write_text("not an image")
        inputs.append(src)

    Image.effect_noise((300, 200), 100).convert("RGB").save(inputs[0] / "noise.png")
    assert run_cli(prototype, inputs, monkeypatch, ["--max-edge", "100"]) == 0

    output = tmp_path / "output"
    assert (output / "f1" / "f1.jpg").is_file()
    assert (output / "f2" / "f2.jpg").is_file()
    log = (tmp_path / prototype.DEFAULT_LOG_NAME).read_text(encoding="utf-8")
    assert "--- Files ---" in log
    assert "[COPY]    f1/f1.jpg" in log
    assert "[COPY]    f2/f2.jpg" in log
    assert "[CONVERT] f1/noise.png -> noise.jpg  300x200 -> 100x67  (" in log
    assert "[SKIP]    f1/notes.txt" in log
    assert "Converted/re-saved: 1  (of which resized: 1)" in log

    assert run_cli(prototype, inputs, monkeypatch) == 0
    assert (tmp_path / "output_1" / "f1" / "f1.jpg").is_file()
    assert "Output folder:" in capsys.readouterr().out


def test_custom_log_option_is_removed(prototype, tmp_path):
    with pytest.raises(SystemExit):
        prototype.build_parser().parse_args(
            [str(tmp_path), "--log", str(tmp_path / "custom.log")]
        )


def test_inputs_with_same_name_get_distinct_output_subfolders(
    prototype, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    inputs = []
    for parent in ("first", "second"):
        src = tmp_path / parent / "album"
        src.mkdir(parents=True)
        Image.new("RGB", (20, 20), (20, 30, 40)).save(src / "photo.jpg")
        inputs.append(src)

    assert run_cli(prototype, inputs, monkeypatch) == 0
    assert (tmp_path / "output" / "album" / "photo.jpg").is_file()
    assert (tmp_path / "output" / "album_1" / "photo.jpg").is_file()
