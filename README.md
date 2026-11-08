# SHAREX

**Your Files. Your Devices. No Internet.**

Cross-platform offline file sharing between Android and Windows over a local Wi-Fi hotspot. No cloud, no internet, no external server.

---

## Status

| Phase | Description                                         | Status      |
| ----- | --------------------------------------------------- | ----------- |
| 1     | Android → Windows transfer, SHA-256 verified        | ✅ Complete |
| 2     | Windows → Android transfer, SHA-256 verified        | ✅ Complete |
| 3     | QR-code pairing (eliminate manual IP/token entry)   | 🚧 Planned  |
| 4     | PySide6 desktop GUI                                 | 🚧 Planned  |
| 5     | Advanced transfer engine (pause/resume, multi-file) | 🚧 Planned  |
| 6     | Local network device discovery                      | 🚧 Planned  |
| 7     | Wi-Fi Direct                                        | 🚧 Planned  |
| 8     | Premium UI polish                                   | 🚧 Planned  |
| 9     | Testing, packaging, release                         | 🚧 Planned  |

## Components

### Desktop (`desktop/`)

- `receiver/server.py` — FastAPI server on port 8000, receives uploads from Android
- `sender/client.py` — CLI to send files to an Android receiver
- Both compute SHA-256 during transfer, verify integrity

### Android (`android/ShareXAndroid2/`)

- Native Kotlin + Jetpack
- `MainActivity.kt` — file picker, OkHttp upload, receiver toggle
- `receiver/SimpleHttpServer.kt` — ServerSocket-based HTTP receiver
- `receiver/ReceiverService.kt` — foreground service keeping receiver alive
- SHA-256 computed during stream, verified against sender's hash

## How it works

1. Windows laptop creates a local Wi-Fi hotspot (no internet required)
2. Android phone connects to the hotspot
3. Both devices are on the `192.168.137.x` subnet
4. Sender POSTs the file over HTTP/1.1 to the receiver's port (8000 on Windows, 8080 on Android)
5. Receiver streams the body to disk, computes SHA-256 incrementally
6. Receiver returns the hash, sender verifies

## Development

See `docs/` (coming in Phase 9) for setup. Until then, the key commands:

**Windows receiver:**

```bat
cd desktop
.venv\Scripts\activate
python main.py
```
