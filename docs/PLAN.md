# Plan (approved)

## Decisions so far
- Sources: phone (through the companion Android app) plus Google Takeout. Cross-source dedupe.
- The phone must be connected for scans of phone photos and full-size views of them on the PC.
- Calibration previews of photos of me: cached on the PC, limit 1 GB, deleted after the photo is rated or when the cache is full (oldest first).
- IQA model: TOPIQ-NR (KonIQ-10k). Exported to ONNX once at install, fixed input 1x3x384x384. Verified: ONNX output equals PyTorch output (max diff 1.5e-7).

## Architecture
- Python 3.11, FastAPI, SQLite (WAL). Plain HTML + JS frontend, no build step.
- ONNX Runtime with DirectML for SCRFD, ArcFace, CLIP ViT-B/16, TOPIQ-NR. CPU fallback per model, logged.
- MediaPipe Face Landmarker on CPU workers.
- Pass 1 (all photos): decode, metadata, thumbnail, perceptual hash, face detection, ArcFace.
- Pass 2 (photos of me only): face crop at full resolution, MediaPipe, sharpness, exposure, CLIP, IQA.
- Runtime guard blocks outbound connections from Python except LAN and loopback.

## Phone app
- Native Android app. Pairing by QR code. Phone is always the client.
- Sync: manifest -> PC asks for unknown photos -> phone uploads -> PC analyzes in memory, deletes temp copy -> PC sends result database back.
- Offline: gallery of on-device photos with the last synced ratings and the same filters. No new ratings offline.
- Same gallery UI code as the PC (WebView with local data).

## Models and licenses
| Use | Model | License |
|---|---|---|
| Face detection | SCRFD-10G (InsightFace buffalo_l) | Non-commercial research only |
| Face identity | ArcFace R50 (buffalo_l w600k_r50) | Non-commercial research only |
| Blendshapes, head pose | MediaPipe Face Landmarker | Apache 2.0 |
| Embeddings | OpenAI CLIP ViT-B/16 image encoder (Xenova ONNX) | MIT |
| Image quality | TOPIQ-NR (IQA-PyTorch) | PolyForm Noncommercial 1.0.0 |
| Reverse geocoding | GeoNames cities1000 via reverse_geocoder | CC BY 4.0 data, LGPL library |

## Milestones
1. Sources (Takeout, folder, phone sync API), dedupe, metadata, geocoding, thumbnails, progress, benchmark
2. Face detection, find me, other people
3. Features
4. Calibration, model, full-library scoring
5. Gallery, filters, export
6. LAN access, mobile UI
7. Android app

## Status
All 7 milestones are built. Tests: pytest (unit + end-to-end on CPU, synthetic data) and the Android JVM unit tests.
Not tested here: DirectML (no AMD GPU in the build container) and the phone app on a real phone (no emulator).
