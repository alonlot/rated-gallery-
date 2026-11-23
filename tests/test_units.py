"""Fast tests without models."""
import json
import socket
from datetime import datetime

import numpy as np
import pytest
from PIL import Image

from ratedgallery import metadata, ranking
from ratedgallery.pipeline import same_capture_time


def test_media_key_variants():
    assert metadata.media_key("IMG_1.JPG") == ("img_1.jpg", 0)
    assert metadata.media_key("IMG_1(2).JPG") == ("img_1.jpg", 2)
    assert metadata.media_key("IMG_1-edited.JPG") == ("img_1.jpg", 0)
    assert metadata.media_key("IMG_1-edited(1).jpg") == ("img_1.jpg", 1)


def _side(d, name, title, ts=1700000000, lat=0.0, lon=0.0):
    (d / name).write_text(json.dumps({"title": title, "photoTakenTime": {"timestamp": str(ts)},
                                       "geoData": {"latitude": lat, "longitude": lon}}))


def test_sidecar_matching(tmp_path):
    _side(tmp_path, "a.jpg.json", "a.jpg", 1)
    _side(tmp_path, "b.jpg.supplemental-metadata.json", "b.jpg", 2)
    _side(tmp_path, "c.jpg(1).json", "c.jpg", 3)
    _side(tmp_path, "c.jpg.json", "c.jpg", 4)
    long = "IMG_20240505_101010_with_a_very_long_file_name_me_1.jpg"
    _side(tmp_path, long[:46] + ".json", long, 5)
    _side(tmp_path, "d.jpg.supplemental-metad.json", "d.jpg", 6)
    (tmp_path / "metadata.json").write_text(json.dumps({"title": "Album"}))
    idx = metadata.index_sidecars(list(tmp_path.glob("*.json")))

    def ts(name):
        return metadata.read_sidecar(metadata.match_sidecar(name, idx)).get("taken_ts")

    assert ts("a.jpg") == 1
    assert ts("b.jpg") == 2
    assert ts("c(1).jpg") == 3
    assert ts("c.jpg") == 4
    assert ts("a-edited.jpg") == 1
    assert ts(long) == 5
    assert ts("d.jpg") == 6
    assert metadata.match_sidecar("zzz.jpg", idx) is None


def test_sidecar_zero_gps_is_no_gps(tmp_path):
    _side(tmp_path, "a.jpg.json", "a.jpg", 10, 0.0, 0.0)
    assert "lat" not in metadata.read_sidecar(str(tmp_path / "a.jpg.json"))
    _side(tmp_path, "b.jpg.json", "b.jpg", 10, 41.9, 12.5)
    assert metadata.read_sidecar(str(tmp_path / "b.jpg.json"))["lat"] == 41.9


def test_exif_and_merge_priority(tmp_path):
    ex = Image.Exif()
    ex[271], ex[272] = "Google", "Pixel 7"
    ex[0x8769] = {36867: "2024:05:03 14:22:10", 42036: "Pixel 7 front camera 2.2mm f/2.2"}
    ex[0x8825] = {1: "N", 2: (10.0, 30.0, 0.0), 3: "W", 4: (20.0, 0.0, 0.0)}
    Image.new("RGB", (32, 32)).save(tmp_path / "x.jpg", exif=ex.tobytes())
    info = metadata.parse_exif(Image.open(tmp_path / "x.jpg").getexif())
    assert info["lat"] == pytest.approx(10.5) and info["lon"] == pytest.approx(-20.0)
    m = metadata.merge(info, {})
    assert m["facing"] == "front" and m["taken_local"] == "2024-05-03T14:22:10"
    m2 = metadata.merge(info, {"taken_ts": 1000.0, "lat": 1.0, "lon": 2.0})
    assert m2["taken_ts"] == 1000.0 and m2["lat"] == 1.0        # sidecar first
    assert m2["taken_local"] == "2024-05-03T14:22:10"           # local time of day from EXIF


def test_facing_from_lens():
    assert metadata.facing_from_lens("iPhone 12 front camera 2.71mm f/2.2") == "front"
    assert metadata.facing_from_lens("iPhone 12 back dual wide camera 4.2mm f/1.6") == "rear"
    assert metadata.facing_from_lens(None) == "unknown"


def test_same_capture_time():
    assert same_capture_time(1000.0, 1000.4)            # copy
    assert not same_capture_time(1000.0, 1001.0)        # burst shot 1 s later
    assert same_capture_time(1000.0, 1000.0 + 3 * 3600)  # time zone difference between sources
    assert not same_capture_time(1000.0, 1000.0 + 86400)
    assert same_capture_time(None, 5.0)


def test_offline_guard_blocks_internet():
    from ratedgallery import offline

    offline.enable()
    s = socket.socket()
    with pytest.raises(ConnectionRefusedError):
        s.connect(("8.8.8.8", 53))
    s.close()
    # loopback stays allowed (the connection itself may be refused, but not by the guard)
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", 9))
    except ConnectionRefusedError as e:
        assert "offline guard" not in str(e)
    except OSError:
        pass
    s.close()


def test_solve_pairs_only_and_mapping():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(60, 4))
    true = X @ np.array([1.0, -0.5, 0, 0])
    pairs = []
    for _ in range(80):
        a, b = rng.choice(60, 2, replace=False)
        pairs.append((a, b, 1 if true[a] > true[b] else -1))
    w, _ = ranking._solve(X, np.array([]), np.array([], dtype=int), pairs, 1.0)
    assert np.corrcoef(X @ w, true)[0, 1] > 0.9
    m = {"raw_sorted": np.sort(true), "ratings_sorted": np.array([3, 5, 5, 6, 7, 7, 7, 8, 9])}
    s = ranking.to_score(m, np.sort(true))
    assert np.all(np.diff(s) >= -1e-9)              # monotonic
    assert len(np.unique(np.round(s, 3))) > 30       # continuous, no plateaus
    assert 1 <= s.min() and s.max() <= 10


def test_password_hash():
    from ratedgallery import auth

    h = auth.hash_password("secret123")
    assert auth.check_password("secret123", h) and not auth.check_password("nope", h)


def test_confidence_low_for_small_blurred_turned_faces():
    good = {"face_w": 0.2, "sharp": 5.0, "yaw": 5.0, "det": 0.85, "mp_ok": 1.0}
    q = ranking.quality_conf
    assert q(good) > 0.9
    assert q({**good, "face_w": 0.03}) < 0.5 * q(good)
    assert q({**good, "sharp": 1.5}) < 0.5 * q(good)
    assert q({**good, "yaw": 70.0}) < 0.5 * q(good)


def test_wordpiece_matches_official_tokenizer():
    from pathlib import Path

    from ratedgallery import config
    from ratedgallery.search import WordPiece

    vocab = config.MODELS / "mclip_vocab.txt"
    if not vocab.exists():
        pytest.skip("search vocab missing (run setup_models)")
    t = WordPiece(vocab)
    cases = json.loads((Path(__file__).parent / "data" / "mclip_tokens.json").read_text("utf-8"))
    for c in cases:
        assert t.encode(c["q"]) == c["ids"], c["q"]


def test_search_rank_rule():
    from ratedgallery.search import rank

    sims = np.array([0.10, 0.31, 0.25, 0.20, 0.30])
    ids = np.array([1, 2, 3, 4, 5])
    assert [i for i, _ in rank(sims, ids)] == [2, 5, 3]   # best - 0.10 = 0.21 is the cut
    assert rank(np.array([]), np.array([])) == []


def test_beauty_head_fit_and_compare():
    from ratedgallery import beauty_train

    rng = np.random.default_rng(0)
    E = rng.normal(size=(300, 512))
    w_true = rng.normal(size=512) / 20
    split = ["train_2022"] * 200 + ["val_2022"] * 40 + ["test_2022"] * 60
    keep = [(s, "male" if i % 2 else "female", "caucasian", float(e @ w_true + 5)) for i, (s, e) in enumerate(zip(split, E))]
    w, b, alpha, r, n = beauty_train.fit(E, keep)
    assert r > 0.6 and n == 300 and w.shape == (512,)
    scut = np.array([np.nan if k[0] != "test_2022" else rng.normal() for k in keep])
    c = beauty_train.compare(E @ w + b, scut, keep)
    assert c["head_all"] > 0.6 and abs(c["scut_all"]) < 0.5 and "head_caucasian_men" in c


def test_dml_check_failure_uses_cpu_and_is_cached(tmp_path, monkeypatch):
    """A failed DirectML check (child process) gives a CPU session; DirectML is never tried in this process."""
    import shutil

    from ratedgallery import config, models

    src = config.MODELS / models.FILES["scrfd"]
    if not src.exists():
        pytest.skip("models missing")
    shutil.copy(src, tmp_path / src.name)
    monkeypatch.setattr(config, "MODELS", tmp_path)
    config.reset_settings_for_tests(tmp_path / "settings.json")
    monkeypatch.setattr(models, "_providers", lambda: ["DmlExecutionProvider", "CPUExecutionProvider"])
    calls = []

    def fake_probe(name, feeds, dims):
        calls.append((name, feeds, dims))
        return {"ok": False, "error": "DirectML check crashed (exit code 0xc0000005)"}

    monkeypatch.setattr(models, "_run_probe", fake_probe)
    models.reset()
    try:
        det = models.SCRFD()
        assert det.sess.get_providers() == ["CPUExecutionProvider"]
        assert models.DEVICES["scrfd"]["device"] == "CPU"
        assert "crashed" in models.DEVICES["scrfd"]["error"]
        models.reset()
        models.SCRFD()
        assert calls == [("scrfd", {"input.1": [1, 3, 640, 640]}, {})]   # second start: result from dml_probe.json
    finally:
        models.reset()


def test_arcface_runs_on_directml_with_fixed_batch():
    """ArcFace (w600k_r50) declares its output batch as 1; with a free batch size its last BatchNormalization
    fails on DirectML for batches > 1. With the batch size fixed it must work (only on PCs with DirectML)."""
    import onnxruntime as ort

    from ratedgallery import config, models

    if "DmlExecutionProvider" not in ort.get_available_providers():
        pytest.skip("no DirectML on this PC")
    if not (config.MODELS / models.FILES["arcface"]).exists():
        pytest.skip("models missing")
    res = models._run_probe("arcface", {"input.1": [8, 3, 112, 112]}, {"None": 8})
    assert res["ok"], res
