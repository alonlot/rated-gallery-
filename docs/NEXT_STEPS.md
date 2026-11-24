# Status and next steps (for the work on the Windows PC)

## Status

### Built
- **PC app** (`ratedgallery/`, Python 3.11, FastAPI, SQLite, plain HTML/JS):
  - Scan of Takeout and folders: dedupe with capture-time rule (bursts kept as groups), Takeout sidecars, EXIF, offline place names, thumbnails.
  - Faces: SCRFD + ArcFace (find me, uncertain matches, other people as filters only, groups, merge and split).
  - Features of my photos: MediaPipe, sharpness, exposure, CLIP ViT-B/16, TOPIQ-NR, general attractiveness model.
  - Calibration (1-10 keys, A/B every 5th, skip, undo), active learning.
  - Personal ridge model with pairwise constraints, cross-validated Spearman, scores 1-10, confidence, breakdown, per-period score.
  - Gallery: filters, search in 50+ languages (Hebrew included), best of bursts, trips and events, map (opt-in), export, LAN access with password and QR code, startup benchmark.
- **Phone app** (`phone-app/`, Kotlin): pairing by QR code, Wi-Fi sync (uploads only new photos), offline gallery, offline search, offline ratings sent at the next sync, PC tab.

### Tested
- Linux container, CPU only: 24 pytest tests (unit + end-to-end on public-domain NASA portraits), Node checks of the gallery logic, 102 Android JVM unit tests, Android lint, a full install from zero.
- Also run on a 400-photo NASA library (screenshots).
- Windows 11, Ryzen 5 5600X + RX 6600 XT: `install.bat` from zero (Python 3.11.9), 26 pytest tests pass, `start.bat`.
  All models run on DirectML. Measured: scan about 1,900 photos/min (1,500 before the speed work), analysis of photos of me about 850/min.
  MEBeauty at install: correlation 0.807 (all) and 0.780 (Caucasian men), SCUT-FBP5500 0.234 and 0.065.
- Attractiveness display and "Attractiveness" sort checked in a browser with the test library.

### NOT tested yet
- The phone app on a real phone (no emulator was available).
- Real photos and real ratings.

### Done since the last status
- **MEBeauty attractiveness model.** `install.bat` trains it on the PC from the MEBeauty dataset (`ratedgallery/beauty_train.py`). It compares it with SCUT-FBP5500 on the same test faces (all faces, and Caucasian men) and keeps it only if it is better. The install log shows the result. MEBeauty terms: non-commercial academic research only. Private use only; never in an app that is sold.
- **Separate attractiveness display.** The detail view shows "General attractiveness X/10" and the difference from my own average, plus a sort option "Attractiveness". Checked in a browser. Note: on the test library the head gives blurred faces
  higher values too (the top photo was a blurred variant), like SCUT-FBP5500.
- **DirectML fix.** ArcFace failed on DirectML for batches > 1 (its output batch is declared as 1), and the failed
  session crashed the next DirectML call. Now each model is checked in a child process with fixed shapes
  (`ratedgallery/dml_probe.py`), and ArcFace has one session per batch size.

- **Hair.** Hair measurements (MediaPipe Hair Segmenter), a "hair and head" view (CLIP), optional hair tags in Calibrate
  (H / J), a hair A/B question about every 10th item, and a small hair model. Tested on CPU here. Photos analyzed
  before this version are analyzed again at the first start (phone photos are uploaded again at the next sync).

### Known limits
- Hebrew search is weaker than English.
- Identical twins can be matched as "me".
- Plain HTTP on the LAN.
- 4 non-commercial models (see README).
- The APK is not in git (36 MB). Build it with `phone-app/build_apk.sh` (needs JDK 21 and the Android SDK), or use the APK from the chat. The signing key files from the chat go in `phone-app/keystore/`.

## Prompt for Claude Code on the PC

Copy this into Claude Code, started in the repository folder:

```
You continue the Rated Gallery project in this repository (branch claude/photo-ranking-app-xrv3wq).
Read README.md, docs/PLAN.md, docs/API.md and docs/NEXT_STEPS.md first.

PRIVACY RULES (strict):
- Do not open, read or view any image in my photo library, and do not print image contents or EXIF data from
  my library into this session. Only use test_data/ (public-domain images) for checks.
- The app must stay 100% local after the install: no cloud APIs, no telemetry.
- Do not commit data/, the APK, or anything in phone-app/keystore/.

This PC: Windows, AMD Ryzen 5 5600X, AMD RX 6600 XT (DirectML), Python 3.11.

Tasks, in this order. Stop and tell me if a step fails, with the exact error:
1. Run install.bat. Show me the end of its output, and the MEBeauty comparison lines
   ("result ... MEBeauty head ... SCUT-FBP5500 ...").
2. Run test.bat (pytest). All tests must pass on Windows. Fix Windows-only problems (paths, long paths,
   multiprocessing spawn, file locks), keep the tests green, and commit.
3. Run start.bat --no-browser in the background, open http://127.0.0.1:8765 with a headless browser and check
   Library > "Speed on this PC": which models run on DirectML and which fell back to CPU (and why).
   If a model failed on DirectML, find out why and fix it if possible (for example fixed input shapes).
4. Check the new attractiveness display in the gallery detail view and the "Attractiveness" sort, with the test
   library (scripts/make_test_library.py makes it; add its takeout folder as a source in a separate data folder:
   set RG_DATA to a temporary folder so my real data is not touched).
5. Tell me the measured scan speed (photos per minute) and give me the exact steps to scan my real Takeout folder.
Commit and push each fix with a clear message.
```
