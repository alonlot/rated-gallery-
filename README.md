# Rated Gallery

Rated Gallery ranks your own photos by how good **you** look in each photo, compared with your other photos.
The score is only about you. Other people are only filters. Nobody else gets a score.

Everything runs on your Windows PC. After the one-time install, the app makes no internet connections
(except map tiles, if you turn the map on). There is no cloud API and no telemetry.

The phone app (Android) syncs with the PC on your home Wi-Fi. The PC analyzes and rates the photos and sends
the ratings back. The phone keeps a gallery of its own photos with the last ratings, also offline.

## Install and start (Windows)

1. Install **Python 3.11 (64-bit)** from python.org. Select "py launcher" in the installer.
2. Install (one time, needs internet, about 10 to 20 minutes):

   ```
   install.bat
   ```

3. Start:

   ```
   start.bat
   ```

   The browser opens `http://127.0.0.1:8765`. Keep the window open while you use the app.

Tests (synthetic data and public-domain portraits): `test.bat`

### Phone app

1. On the PC: **Settings > Phone and LAN access**. Set a password and turn LAN access on.
   Windows asks to allow Python through the firewall. Allow it for **private** networks only.
2. Copy the APK (`phone-app/dist/RatedGallery.apk` after `phone-app/build_apk.sh`, or the file I sent you) to the phone and open it. Android asks you to allow
   installation from this source.
3. On the PC, push **Pair phone**. Scan the QR code with the phone camera, or type the address,
   port and 6-digit code in the app.
4. In the app, allow **All photos** and **media location**. Without "All photos" the app sees only the photos you
   select. Without media location, Android removes GPS from the photos, and location filters do not work.
5. In HyperOS settings, set the app battery use to **No restrictions**. If you do not, HyperOS can stop
   the background sync.

The phone must be on the same Wi-Fi as the PC, and the PC must run the app, for sync and for full-size views of phone
photos on the PC. Give the PC a fixed IP address in your router. If the IP address changes, pair again.

## How to use it

1. **Library**: add your Takeout folder and push **Scan for new photos**. Pair the phone.
   Recommended order: scan Takeout first, select your reference faces (step 2), then pair the phone.
   Then each phone photo is uploaded only one time.
2. **Me & People**: select 5 to 10 faces of yourself. Check the uncertain matches (✓ or ✕).
   Open other groups to name people (only for filters). You can merge and split groups.
3. **Calibrate**: rate "how good do I look in this photo" from 1 to 10.
   - PC keys: `1`…`9`, `0` = 10, `S` skip, `U` undo. A/B comparison: `←` left, `→` right, `↓` equal.
   - Phone: large buttons, swipe toward the better photo in A/B.
   - The model trains again after every 10 new ratings and scores all your photos.
     The box at the top shows the quality (Spearman correlation on ratings the model did not train on).
4. **Search**: type what you look for in the search box of the gallery, in English or Hebrew (50+ languages),
   for example "river", "sunset at the beach" or "עוגת יום הולדת". The best matches come first, and all filters still apply.
   Search also works offline in the phone app (the phone downloads the 137 MB text model from the PC one time).
   Limits: counting ("3 people"), negation ("no people"), names and text inside photos do not work well.
   Hebrew results are a little weaker than English results. Use filters for people, dates and places.
5. **Gallery (PC and phone)**: quick chips at the top: Photos / Trips / Map, **Best of bursts** (one photo per burst,
   tap "+N" to see the others), **Per period** (compare each photo only with photos from about the same years, because
   your look changes over time), With me / Everyone, Selfies, 8+, Me alone. **Trips**: the app finds your home (the city with
   the most photos); a trip is photos more than 80 km from home with gaps of 2 days or less. **Events**: 6 or more photos
   taken within a few hours. On the phone you can also rate photos offline; the ratings go to the PC at the next sync.
6. **Filters**: filters (score, confidence, people, date, time of day, place, radius, camera, front/rear camera,
   number of people). Tap a photo for the score breakdown and all metadata. **Select** copies originals to a folder.

## What the app does with your files

- It only **reads** your photos. It never changes, moves or deletes them (the tests check this).
- It removes duplicates from the gallery (same photo, also across Takeout and the phone) and keeps the
  highest-resolution copy. Burst shots are **not** removed: they are grouped. Calibration does not show
  two photos of one burst.
- Phone photos are uploaded to the PC for analysis, kept in `data\spool` until the analysis is done, then deleted.
- The PC keeps: the database, thumbnails (about 25 KB per photo), face crops, and large previews of your
  photos for calibration (limit 1 GB, deleted after you rate the photo).
- All data is in the `data` folder. To start again from zero, stop the app and delete `data` (keep `data\models`).

## Hardware

| Part | Need |
|---|---|
| OS | Windows 10 or 11, 64-bit |
| CPU | 4 cores or more. Default: 5 worker processes (one core stays free). Setting: **Settings > Processing** |
| GPU | Optional. DirectX 12 GPU with DirectML (tested target: AMD RX 6600 XT). Without it, all models run on the CPU |
| RAM | 8 GB minimum, 16 GB recommended |
| Disk | About 3 GB for the app and models, plus about 0.1 GB per 1,000 photos, plus the 1 GB preview cache |
| Phone | Android 10 or newer (target: Poco F6) |

At start, each model is tried on DirectML. If DirectML fails for a model, that model runs on the CPU.
**Library > Speed on this PC** shows the device of each model and the measured speed in photos per minute.

## Expected scan time per 10,000 photos

**These numbers for your PC are estimates, not measurements.** I could not run the app on your PC.
The benchmark in the app gives the real number after the first start.

Measured in the development container (4 virtual CPU cores, no GPU, all models on the CPU, 3 workers):
195 photos per minute for the scan and 116 per minute for the analysis of photos of you.
That is about 51 minutes per 10,000 photos for the scan.

Estimates for a Ryzen 5 5600X + RX 6600 XT:

| Step | DirectML works | DirectML fails (CPU only) |
|---|---|---|
| Scan, Takeout on SSD, mostly JPEG | 5 to 10 min | 25 to 40 min |
| Scan, many HEIC files (no reduced-size decoding for HEIC) | 10 to 20 min | 30 to 45 min |
| Scan, phone over Wi-Fi (about 40 GB) | 20 to 45 min (limit: Wi-Fi) | 30 to 50 min |
| Analysis of the photos of you (about 3,000 of 10,000) | 3 to 6 min | 15 to 25 min |
| Retrain and score all photos | under 10 s | under 10 s |

Low priority mode uses half of the workers at idle priority and pauses between GPU batches. Scans then take
about two times longer.

## Models and licenses

| Use | Model | License |
|---|---|---|
| Face detection | SCRFD-10G (InsightFace buffalo_l) | **Non-commercial research only** |
| Face identity | ArcFace R50 (buffalo_l w600k_r50) | **Non-commercial research only** |
| Blink, smile, mouth, brows, head pose | MediaPipe Face Landmarker | Apache 2.0 |
| Face and photo embeddings | OpenAI CLIP ViT-B/16 image encoder (ONNX by Xenova) | MIT |
| Image quality | TOPIQ-NR, KonIQ-10k weights (IQA-PyTorch) | **PolyForm Noncommercial 1.0.0** |
| General attractiveness (one input of your model) | ResNet-18 trained on SCUT-FBP5500 (official weights, converted to ONNX at install) | **SCUT-FBP5500: non-commercial research only** |
| Offline place names | GeoNames cities1000 via reverse_geocoder | CC BY 4.0 (data), LGPL (library) |
| Text search, photo side | OpenAI CLIP ViT-B/32 image encoder (ONNX by Xenova) | MIT |
| Text search, text side (50+ languages, Hebrew included) | sentence-transformers clip-ViT-B-32-multilingual-v1, int8 | Apache 2.0 |

Four models are for non-commercial use only. Personal use is permitted. If you sell or publish this app,
you must replace them. Libraries: onnxruntime-directml (MIT), FastAPI (MIT), uvicorn (BSD), Pillow (MIT-CMU),
pillow-heif (BSD-3, includes LGPL libheif), imagehash (BSD-2), NumPy and SciPy (BSD), qrcode (BSD),
psutil (BSD), Leaflet (BSD-2, stored in `ratedgallery/static/vendor`). Map tiles: OpenStreetMap (ODbL).
Phone app: AndroidX (Apache 2.0), OkHttp (Apache 2.0).

## How the score works

- **Before you rate**: a simple rule score (eyes open, sharp face, good exposure, natural smile, small head angle,
  image quality). Scores with `~` are these estimates.
- **After 10 or more ratings**: a ridge regression on about 27 measured values plus reduced CLIP embeddings
  (16 dimensions for the face, 8 for the photo). A/B comparisons are pairwise ranking constraints in the same model.
  The regularization is selected by cross-validation.
- **General attractiveness model (SCUT-FBP5500)**: it is only one input of your model, never a score of its own.
  At each training, the app measures the held-out Spearman with and without it, on the same folds. It uses the model
  only if it improves your Spearman by 0.01 or more. The Calibrate page shows the result. Its input is the mean of
  4 views of the face crop, because one crop gives unstable values. This model was trained on front-facing studio
  photos rated by a small group of people. It has a known bias, and it often gives blurred faces a higher value.
- The model output is mapped to 1 to 10 so that the library has the same distribution as your own ratings.
- **Confidence** is low when your face is small, blurred, turned away or partly hidden, and when the model is unsure
  (20 bootstrap models disagree).
- **Score breakdown**: the effect of each measured value in score points. "Overall look of my face (learned)"
  and "Scene and style of the photo (learned)" are the CLIP parts. They cannot be explained in simple words.

## Known limits

- I tested the DirectML path and the phone app only by build and unit tests. I could not run them on your PC or phone.
- Front or rear camera comes from the EXIF lens field. Many Android photos do not have it. Then the app copies the
  value from other photos of the same phone with the same focal length, or shows "unknown".
- Place names are the nearest town in GeoNames (towns with 1,000 or more people), not street names.
- No model detects occlusion (sunglasses, hands). The app uses detection confidence and landmark failure instead.
- LAN traffic uses plain HTTP. The password and device key protect access, but someone on your Wi-Fi with network tools
  can read the traffic.
- After a new scan, the app groups unnamed faces again. Merges and splits of **unnamed** groups are lost then. Named
  people stay.
- The phone app signing key is in `phone-app/keystore` and is not in git. If you lose it, uninstall the app before you
  install a new build. The phone database is then empty until the next sync.

## Project layout

```
ratedgallery/      Python app (FastAPI server, pipeline, models, ranking) and static/ web UI
phone-app/         Android app (Kotlin). Build: phone-app/build_apk.sh. APK: phone-app/dist/
docs/API.md        Contract between the PC and the phone app
scripts/           Test data: fetch public-domain portraits, make the synthetic library
tests/             pytest suite (unit + end-to-end on CPU) and the gallery filter check (Node)
```
