import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RG_MODELS", str(ROOT / "data" / "models"))
os.environ["RG_NO_BENCHMARK"] = "1"


@pytest.fixture(scope="session")
def library():
    """Synthetic test library (public-domain portraits). Downloads the base portraits once if needed."""
    base = ROOT / "test_data" / "base"
    if not any(base.glob("me_*.jpg")):
        subprocess.check_call([sys.executable, str(ROOT / "scripts" / "fetch_test_faces.py")])
    subprocess.check_call([sys.executable, str(ROOT / "scripts" / "make_test_library.py")])
    return ROOT / "test_data" / "generated"


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory):
    """A fresh data folder (database, caches) for one test module."""
    from ratedgallery import config, db, models, offline

    offline.enable()
    d = tmp_path_factory.mktemp("rgdata")
    config.DATA = d
    config.reset_settings_for_tests(d / "settings.json")
    config.settings().update(workers=3)
    db.set_path(d / "ratedgallery.db")
    models.reset()
    return d


def need_models():
    from ratedgallery import models

    miss = models.missing_models()
    if miss:
        pytest.skip("models missing (run python -m ratedgallery.setup_models): " + ", ".join(miss))
