import os
import random

import pytest
from PIL import Image


def make_noise(path, size=(300, 200), fmt=None, **save_kw):
    """Incompressible random image (big files, so conversions really shrink them)."""
    img = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    img.save(path, format=fmt, **save_kw)
    return path


def make_smooth(path, size=(600, 400), seed=1, **save_kw):
    """Compressible image: small random picture blown up (many sizes reachable)."""
    rnd = random.Random(seed)
    small = Image.new("RGB", (30, 20))
    small.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                   for _ in range(30 * 20)])
    img = small.resize(size, Image.BICUBIC)
    img.save(path, **save_kw)
    return path


@pytest.fixture
def folders(tmp_path):
    src = tmp_path / "in"
    dst = tmp_path / "out"
    src.mkdir()
    return src, dst
