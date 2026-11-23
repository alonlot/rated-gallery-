"""DirectML check for one ONNX model, run in a child process by models.py.

A model that fails on DirectML can leave DirectML broken for the whole process: the next DirectML call of
another model then crashes Python (access violation, seen with ArcFace on an RX 6600 XT). So the app never
tries an unknown model on DirectML in its own process. This script loads the model with the same fixed shapes
as the app, runs it on DirectML and on the CPU with the same random input, and prints one JSON line:
{"ok": true/false, "error": "...", "rel_diff": ...}. A crash of this process means "not ok".

Usage: python dml_probe.py '{"path": "...", "feeds": {"input": [1, 3, 640, 640]}, "dims": {"batch": 1}}'
Only NumPy and ONNX Runtime are imported (no app code), so this file also runs as a plain script.
"""
from __future__ import annotations

import json
import sys

MAX_REL_DIFF = 1e-2


def probe(path: str, feeds: dict, dims: dict) -> dict:
    import numpy as np
    import onnxruntime as ort

    if "DmlExecutionProvider" not in ort.get_available_providers():
        return {"ok": False, "error": "DirectML is not available in this ONNX Runtime"}
    rng = np.random.default_rng(0)
    x = {k: rng.random(tuple(v)).astype(np.float32) for k, v in feeds.items()}
    so = ort.SessionOptions()
    so.log_severity_level = 3
    so.enable_mem_pattern = False
    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    for k, v in dims.items():
        so.add_free_dimension_override_by_name(k, int(v))
    try:
        dml = ort.InferenceSession(path, so, providers=["DmlExecutionProvider", "CPUExecutionProvider"])
        y = dml.run(None, x)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    cpu = ort.InferenceSession(path, providers=["CPUExecutionProvider"]).run(None, x)
    rel = max(float(np.abs(a - b).max()) / (float(np.abs(b).max()) + 1e-6) for a, b in zip(y, cpu))
    if not rel < MAX_REL_DIFF:
        return {"ok": False, "error": f"DirectML output differs from the CPU output (relative difference {rel:.3g})",
                "rel_diff": rel}
    return {"ok": True, "error": None, "rel_diff": rel}


def main():
    spec = json.loads(sys.argv[1])
    res = probe(spec["path"], spec["feeds"], spec.get("dims") or {})
    print("DML_PROBE " + json.dumps(res), flush=True)


if __name__ == "__main__":
    main()
