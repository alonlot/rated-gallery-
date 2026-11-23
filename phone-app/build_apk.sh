#!/usr/bin/env bash
# Builds a signed release APK and copies it to dist/RatedGallery.apk.
# Creates keystore/release.jks (with random passwords in keystore/keystore.properties) on first run.
# Both keystore files are secrets: they are gitignored, keep a backup (you need the same key to update the app).
set -euo pipefail
cd "$(dirname "$0")"

export ANDROID_HOME="${ANDROID_HOME:-/opt/android-sdk}"
if [ ! -d "$ANDROID_HOME/platforms" ]; then
  echo "Android SDK not found at $ANDROID_HOME (set ANDROID_HOME)." >&2
  exit 1
fi

KS_DIR=keystore
KS="$KS_DIR/release.jks"
PROPS="$KS_DIR/keystore.properties"
if [ ! -f "$KS" ]; then
  echo "Creating signing key $KS"
  mkdir -p "$KS_DIR"
  chmod 700 "$KS_DIR"
  PASS="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)"
  keytool -genkeypair -noprompt \
    -keystore "$KS" -storetype PKCS12 -storepass "$PASS" -keypass "$PASS" \
    -alias ratedgallery -keyalg RSA -keysize 3072 -validity 10000 \
    -dname "CN=Rated Gallery, O=Personal" 2>&1 | grep -v '^Picked up' || true
  ( umask 077; cat > "$PROPS" <<PROPS_EOF
storeFile=keystore/release.jks
storePassword=$PASS
keyAlias=ratedgallery
keyPassword=$PASS
PROPS_EOF
  )
  chmod 600 "$KS" "$PROPS"
elif [ ! -f "$PROPS" ]; then
  echo "$KS exists but $PROPS is missing; cannot sign." >&2
  exit 1
fi

./gradlew --no-daemon clean :app:assembleRelease

APK=app/build/outputs/apk/release/app-release.apk
if [ ! -f "$APK" ]; then
  echo "Release APK not found (signing failed?)" >&2
  exit 1
fi
mkdir -p dist
cp "$APK" dist/RatedGallery.apk
APKSIGNER="$(ls -d "$ANDROID_HOME"/build-tools/*/ | sort -V | tail -1)apksigner"
if [ -x "$APKSIGNER" ]; then
  "$APKSIGNER" verify --print-certs dist/RatedGallery.apk 2>&1 | grep -v '^Picked up' | head -3
fi
ls -l dist/RatedGallery.apk
echo "Done: $(pwd)/dist/RatedGallery.apk"
