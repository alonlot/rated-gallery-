# Rated Gallery: PC <-> phone contract

This file is the contract between the PC server (`ratedgallery/`) and the Android app (`phone-app/`).
Both sides must follow it. Version: 1.

## 1. Roles

- The PC server analyzes and rates photos. It keeps no original phone photos after analysis.
- The phone app is always the HTTP client. The phone runs no server.
- The phone keeps the last synced index and shows an offline gallery of photos on the phone.

## 2. Pairing

1. The user pushes "Pair phone" on the PC. The PC shows a 6-digit code and a QR code.
2. QR content: `ratedgallery://pair?host=<pc-lan-ip>&port=<port>&code=<6 digits>`
3. The app handles that deep link (or the user types host, port and code).
4. The app sends:

```
POST http://<host>:<port>/api/device/pair
Content-Type: application/json
{"code": "123456", "name": "Poco F6"}
```

Response `200`:

```
{"device_id": 3, "token": "<64 hex chars>", "server_name": "DESKTOP-ABC"}
```

Errors: `403 {"detail": "invalid or expired code"}`. A code is valid for 10 minutes and for one use.

All later device requests send `Authorization: Bearer <token>`.
A `401` response means the token is not valid any more. The app must show "Pair again".

## 3. Device endpoints

All bodies are JSON unless stated. All times are Unix epoch.

### `GET /api/device/ping`
`{"ok": true, "server_version": "1.0", "server_name": "DESKTOP-ABC", "device_id": 3}`
The app uses a 3 second timeout. No answer means "PC not reachable".

### `POST /api/device/manifest`
The full list of images on the phone (MediaStore.Images, all volumes).

```
{"full": true,
 "items": [{"mid": "1234", "name": "IMG_20240503_142210.jpg", "size": 3456789,
            "mtime": 1714738930, "taken": 1714738930123, "mime": "image/jpeg",
            "path": "DCIM/Camera/"}]}
```

- `mid`: MediaStore `_ID` as a string. `mtime`: `DATE_MODIFIED` (seconds). `taken`: `DATE_TAKEN` (milliseconds) or `null`.
- With `full: true`, items that the PC knows but are not in the list become "missing" on the PC.

Response: `{"known": 9000, "new": 120, "changed": 2, "missing": 5}`

### `GET /api/device/wanted?limit=20`
`{"items": ["1234", "1240"], "remaining": 118}`

The PC lists the images it wants now, most urgent first (photos the user opened on the PC come first).
The app uploads each item, then asks again, until `items` is empty.

### `PUT /api/device/file/{mid}`
Body: the raw original bytes of the image. `Content-Type`: the image mime type.
The app must read the original with `MediaStore.setRequireOriginal(uri)` so that GPS EXIF is not removed.
Response: `{"ok": true}`. `404` if the PC does not know the mid (send the manifest first).

### `GET /api/device/status`
`{"pending": 37, "busy": true, "task": "Scanning", "done": 120, "total": 157}`
`pending` is the number of uploaded photos that the PC did not process yet.

### `GET /api/device/index`
The rating index for the photos on this device only (gzip allowed). The schema is in section 4.
Each photo also has `mid`, `bd`, `fb` and `meta`.

### `GET /api/device/session?token=<token>`
Sets a session cookie and redirects to `/`. The app uses it to open the full PC web UI in a WebView ("PC" tab).

## 4. Index schema (shared by the PC web UI and the phone)

`GET /api/index` (PC web UI) and `GET /api/device/index` (phone) return:

```
{
  "version": 1,
  "generated": "2026-11-23T10:00:00",
  "model": {"labels": 120, "pairs": 30, "spearman": 0.61, "trained": "2026-11-23T09:58:00"},
  "persons": [{"id": 1, "name": "Me", "me": true}, {"id": 5, "name": "Dana", "me": false}],
  "photos": [
    {
      "id": 123,                       // PC photo id
      "t": "2024-05-03T14:22:10",      // local date and time, or null
      "lat": 41.9, "lon": 12.5,        // or null
      "cc": "IT", "country": "Italy", "admin": "Lazio", "city": "Rome",   // or null
      "cam": "Xiaomi 24069PC21G",      // or null
      "facing": "front",               // "front" | "rear" | "unknown"
      "np": 2,                         // number of people (clear faces) in the photo
      "me": true,                      // my face is in the photo
      "s": 8.2,                        // my score 1..10, or null
      "c": 0.81,                       // confidence 0..1, or null
      "prior": false,                  // true: score comes from the heuristic, not the trained model
      "r": 9,                          // my own rating, or null
      "p": [1, 5],                     // ids of recognized persons in the photo (me included)
      "w": 4000, "h": 3000,            // original size
      "src": "phone",                  // "phone" | "folder"
      "b": 77,                         // burst group id or null

      // only in /api/device/index:
      "mid": "1234",
      "fb": [0.31, 0.22, 0.52, 0.48],  // my face box, normalized x1,y1,x2,y2, or null
      "bd": [["Eyes open", 0.6], ["Sharp face", 0.3], ["Head turned", -0.4]],  // effect in score points
      "meta": {"Date": "3 May 2024 14:22", "Place": "Rome, Lazio, Italy", "Camera": "Xiaomi 24069PC21G"}
    }
  ]
}
```

## 5. Phone WebView bridge (offline gallery)

The app shows `assets/web/gallery.html` through `WebViewAssetLoader` at
`https://appassets.androidplatform.net/assets/web/gallery.html`.
The files in `assets/web/` are copied at build time from `ratedgallery/static/`.

The app adds a JavaScript interface named `RGBridge`:

| Method | Returns | Meaning |
|---|---|---|
| `getIndex()` | String (JSON) | The last synced device index. `{"version":1,"persons":[],"photos":[]}` if none. |
| `getStatus()` | String (JSON) | `{"paired":true,"host":"192.168.1.10","port":8765,"connected":true,"syncing":false,"lastSync":1714738930,"phase":"upload","done":5,"total":20,"message":""}` |
| `syncNow()` | void | Start a sync now. |
| `share(mid)` | void | Open the Android share sheet for the original image. |
| `openPC()` | void | Open the PC tab (full PC web UI). |

Image URLs served by the app (path handler on the same origin):

- `https://appassets.androidplatform.net/media/thumb/<mid>`: JPEG, about 384 px.
- `https://appassets.androidplatform.net/media/full/<mid>`: JPEG, longest side up to 2560 px (HEIC is decoded by the app).

When a new index is saved, the app calls `window.RG_onIndexUpdated && window.RG_onIndexUpdated()` in the WebView.
When the status changes, the app calls `window.RG_onStatus && window.RG_onStatus(<status json>)`.
