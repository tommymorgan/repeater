# The Repeater: Android feasibility

Research date: 2026-10-06. Status: source-backed exploration, **not an Android implementation or device-tested feasibility claim**. The completed native transcript-review build, saved recordings, settings and other WIP remain unchanged.

## Authorized direction

Tommy requested exploring an Android version and sharing as much Linux/Android code as practical: “It's a desire, not a mandate.” The request was relayed by the established effort router in Agent Mail request `001791327045988-6fs8tdfx`. It authorizes research, not a spike, package installation, Linux rewrite, API/security/configuration change, recording migration/upload or paid/external inference.

Code sharing is preferred; Python on Android, a common UI and rewriting Linux are not requirements. The framework and first mobile capability below are proposals, not accepted product decisions. Current Linux source and `linux-port.md` own Linux behavior; this note owns the dated feasibility findings, not a new production architecture contract.

## Recommendation

Evaluate a **native Kotlin/Compose Android shell**, optionally embedding an extracted pure-Python domain core with **Chaquopy**, and using **whisper.cpp through JNI/C++** for on-device transcription. Keep the Linux GTK app intact. Share session formats, behavior fixtures and the existing C++ inference engine regardless of whether Python embedding proves worthwhile.

The first optional spike should be an **imported-recording review companion** using synthetic session bundles. It can prove the useful shared-core boundary without promising Android call capture. A standalone microphone recorder is a credible next capability. Arbitrary telephone/VoIP Me/Them capture is not a credible ordinary-app promise under Android's public APIs.

## What the current app can share

| Current source | Realistically reusable behavior | Android-specific work |
|---|---|---|
| `linux/transcribe.py:37–157` | Transcript validation, generated/current transcript state, grouped corrections and Undo/Redo | Extract from filesystem/process imports; define a stable Kotlin/Python boundary and Android persistence adapter |
| `linux/transcribe.py:329–442` | Word decoding, segment merge, blank-audio cleanup and JSON/TXT/SRT rendering | JNI must supply equivalent genuine token/word timing; the existing decoder expects whisper-cli full JSON and millisecond offsets |
| `linux/review.py:132–161,260–280,327–340` | Exact finding anchors, current/stale review decisions and original-word excerpt selection | Separate these from network, locks, file loading and multi-file publication; Android inference/networking is not automatically provided |
| Session/transcript/edit/review JSON and PCM WAV | Track identity, original/generated/corrected text, real timing, correction provenance and portable exports | Android URIs, lane availability, durable writes and process-death recovery require explicit adapters |
| Existing whisper.cpp C++ engine | Inference implementation and compatible model format | Android build/JNI/model storage, thread serialization, cancellation, memory and thermal behavior |

These are candidates, not measured reuse percentages or proof that whole Python modules import on Android. `transcribe.py` also imports `fcntl`/`subprocess`, takes directory locks (`:23`), masks signals during publication (`:445`) and runs `whisper-cli` (`:490`). `review.py` imports its storage helpers. The domain and adapters currently share files.

The following are not a portable desktop shell:

- GTK4/libadwaita in `gui.py` and `transcript.py`, and GStreamer/playbin in `playback.py`.
- PipeWire discovery/default resolution and `pw-record` subprocesses in `capture.py:39,53,262`.
- Python CLI process groups in `desktop.py:362–464`; freedesktop Trash and `renameat2` in `desktop.py:95–160`.

Share behavior, not those platform integrations. Kotlin's UTF-16 text indices and Python's Unicode code-point offsets, canonical JSON hashing, timestamp units and rounding need compatibility fixtures before editing/anchor exchange is trusted.

## Existing Android prior art: note-taker

The inspected sibling app at `../homelab/apps/note-taker/android` supplies useful Kotlin/Compose integration patterns, not a ready-made Repeater Android implementation. Source references below are relative to its `app/src/main/kotlin/com/tommymorgan/notetaker/` directory. This comparison reinforces native Kotlin as an evaluation candidate; it does not select a framework or authorize copying its transfer defaults.

| Area | Reusable prior art | Required Repeater adaptation |
|---|---|---|
| Capture/service | `audio/AudioRecordingManager.kt:73–98,139–148` uses mono 16 kHz PCM16 AudioRecord; `service/RecordingService.kt:62–97,110–172` owns foreground notification and Stop | Saved output is AAC-LC/M4A in public MediaStore, not private PCM WAV. There is no Me/Them playback capture or device selection. Preserve lane identity and implement WAV finalization/interruption recovery. |
| Playback | `ui/detail/DetailViewModel.kt:303–363,426–429` owns ExoPlayer and releases it | Whole-file play/pause is not transcript-row or word-bounded playback. Add genuine segment/word timing, finite clipping and explicit stop; no proportional timing substitute. |
| Persistence | Room entities/Flow/DAO plus explicit migration (`data/local/entity/RecordingEntity.kt:8–81`, `data/local/NoteTakerDatabase.kt:25–75`) | The row holds whole transcript text, not original/current segment data, correction operations/Undo cursor or review history. Preserve portable JSON/WAV contracts and coordinate durable bundle writes with local index state. |
| Background work | WorkManager constraints, unique jobs and retry state (`worker/UploadWorker.kt:28–104,130–184`) | Current startup/new-recording flows automatically upload audio and reconcile with server state (`NoteTakerApp.kt:57–74`, `ui/main/MainViewModel.kt:51–64`). Do not inherit upload-first or server-overwrite semantics; local-cost intent does not require on-phone inference or automatic transfer. |
| Export/delete | FileProvider grants and explicit ZIP/share plumbing (`ui/detail/DetailViewModel.kt:127–258`) | Existing ZIP omits Repeater timing, baseline, correction and review audit, so it is not a round-trip bundle. Delete is permanent/best-effort (`data/repository/RecordingRepository.kt:360–401`), not Archive/Trash/Restore. URI strings must not be treated as filesystem paths. |

Do not transplant the service lifecycle unchanged: `START_STICKY` has no null-intent restoration, and `onDestroy` does not stop/release its AudioRecord (`RecordingService.kt:83–107`). Permission-denial recovery, process death, reader-thread errors, URI fallback and coordinated DB/media publication need real Android verification. Existing whole-text transcript UI does not establish editing, Undo/Redo, local Whisper or word-clip support.

The actual Gradle source targets/compiles API 36, requires minSdk 33 and Java/JVM 21, and uses Kotlin 2.1 (`app/build.gradle.kts:12–17,37–39,59–61`; root `build.gradle.kts:4`). Do not rely on its older README JDK 17 advice. The inspected host had Java 17, no configured Android SDK environment and no connected adb device; no Android build, installation or device run was performed. These requirements are this app's choices, not Repeater's chosen minimums.

**Recommendation remains:** an imported-session review companion first, optionally followed by microphone recording. Reuse Kotlin platform patterns, JSON/WAV contracts, behavior fixtures and the whisper.cpp engine; evaluate a small extracted Python core independently. Do not rewrite Linux, copy the lossy/public/upload-first pipeline or claim arbitrary call capture.

## Implementation paths

| Path | Benefit | Cost and current evidence |
|---|---|---|
| Kotlin/Compose + Chaquopy pure core | Native Android lifecycle/UI with existing Python algorithms | Interpreter size/startup, extraction and ABI/toolchain/thread coordination. Chaquopy 17 supports Python 3.10–3.14, minSdk 24 and tested AGP 7.3–9.2. Default Python is 3.10; select a version deliberately. Python >=3.12 is 64-bit arm64-v8a/x86_64 only. [1][2] |
| Kotlin/Compose + Kotlin domain logic | No embedded Python; share JSON/WAV contracts, fixtures and C++ ASR | Port and maintain domain behavior in two implementations. Sensible fallback if Python packaging cost outweighs reuse; no Linux rewrite required. |
| Kivy/python-for-android | Python-oriented Android packaging | New Kivy UI/native adapters, Android recipes for compiled dependencies and service/lifecycle work; GTK does not carry over. Current source recipes default Python 3.14.2 and minimum NDK API 21, but not every advertised Python/ABI combination has been built here. [3][4] |
| Briefcase/BeeWare | Python application packaging with Gradle/APK/AAB | New Android UI/adapters, native-wheel constraints. Current Android template itself embeds Chaquopy; it is not a separate escape from these runtime costs. Current-main template AGP 9.3.1 is above Chaquopy's documented tested ceiling 9.2: select a coherent release/template pair, not mutable-main defaults. [5][6] |

CPython's Android model is app-private embedding, not a system `python`/`pip` executable. File I/O, sockets and threads are generally POSIX-like, but the subprocess module is unsupported on Android; subprocess creation is possible but officially unsupported. Multiprocessing is unavailable. This does not mean all Android native processes are impossible; it means desktop CLI orchestration is not a sound portability boundary. Host Linux wheels, GTK/GStreamer libraries and CUDA flags cannot be assumed to work. [7][8]

The official whisper.cpp Android example supplies CMake/JNI and serialized Kotlin context access. Its sample minSdk is 26, not proof of the engine's absolute minimum. Native timestamps are 10 ms ticks. The sample returns segment text/times, not Repeater's full-JSON/token-word contract. Match the selected Linux engine revision, provide genuine word timing, cancellation/errors and safe context ownership before claiming review parity. No Android GPU, speed, memory, battery or sustained thermal claim has been verified. [9]

## Recording boundary: Android is not PipeWire

| Capability | Public-platform boundary | Product consequence |
|---|---|---|
| Microphone recording | `MediaRecorder` and PCM-reading `AudioRecord` support it; runtime permission and microphone foreground-service lifecycle apply. [10][11][15] | A voice recorder is credible. Actual device, route changes and background continuation still need proof. |
| Other-app playback | Android 10/API 29+, `RECORD_AUDIO`, explicit MediaProjection consent and same user profile. Source manifest/application/player policies must permit capture; strictest policy wins. Only MEDIA/GAME/UNKNOWN usages are eligible. [12] | Consent does not override exclusions; VOICE_COMMUNICATION is ineligible. Capture is a filtered mix, not guaranteed Them or speaker diarization. |
| Concurrent Me/Them | A single `AudioRecord.Builder` cannot combine microphone source and playback-capture config. Two paths are a candidate, not tested here. Two ordinary apps cannot both receive microphone input; calls/privacy-sensitive capture take priority and a recorder may receive silence. [11][13] | A meeting app may defeat either or both lanes. A passing media-player test does not establish meeting/call support. Speakerphone acoustic pickup is not isolated two-track parity. |
| Telephone TX/RX | VOICE_CALL/UPLINK/DOWNLINK require `CAPTURE_AUDIO_OUTPUT`, reserved for system components. VOICE_COMMUNICATION is a mic source for an app's own VoIP, not another app's call feed. [14] | Do not promise arbitrary phone/VoIP capture or suggest privileged/accessibility workarounds. |
| Background/lock | Visible user-initiated start, service types and while-in-use permissions differ from permitted background continuation. Projection revocation ends streams. Android 15 QPR1 screen projection stops on lock; exact **audio-only** projection lock behavior is unverified. [15][16][17] | Do not promise unattended startup or locked-device two-track continuity. Handle silencing, interruption and partial data honestly. Task Manager Stop provides no shutdown callback. [18] |

Use app-private persistent storage, not cache, for canonical audio/JSON bundles. App-specific files disappear on uninstall. FileProvider shares content URIs with temporary grants; SAF exports can provide durable user-chosen files. Sharing is not backup. MediaStore Trash applies to media URIs, not an atomic private JSON+WAV session directory: preserve Archive as metadata and evaluate reversible app-owned bundle Trash separately, with explicit retention. No storage policy or retention was selected here. [19][20][21]

## Optional bounded spike: separate authorization required

Recommended first proof: one isolated native Android review companion, not a product build, with an arm64 device and x86_64 emulator, synthetic sessions only, no real meetings, uploads, review-model inference or Linux migration. Reuse source only after a separately authorized clean extraction; otherwise this step cannot establish actual code sharing.

1. **Shared behavior:** open a synthetic Linux-format bundle, edit a paragraph, Undo/Redo, save/reopen and export JSON/TXT/SRT. Pass: track IDs/times and correction history survive; both runtimes agree on outputs and fingerprints, including Unicode/empty/manual-marker cases.
2. **Review/replay:** load clearly marked fixture findings, replay the matching original word interval, Keep/Commit/Skip, Undo and leave/re-enter the same recording. Pass: edits/stale decisions and audio boundaries match the Linux contract; no fake/proportional word timing or listening-required gate. Fixture findings are not evidence of AI inference.
3. **Persistence/package:** force process death, reopen partial writes, import/export through URIs and Trash/Restore the bundle. Pass: paired audio/metadata, explicit recoverable state and no silent corruption. Measure APK size, startup, boundary latency and native-library/page-size compatibility; do not infer acceptable costs from docs.
4. **Only if standalone recording is selected:** add isolated mic/lifecycle and JNI-ASR proofs using consented test speech. Test a 20-minute background/screen-off run, permission/privacy-toggle denial, route changes and forced stop. Prove finite valid WAVs, partial recovery, actual local inference/cancellation and genuine word timing; measure sustained memory/thermal speed on the intended handset. For eligible playback, separately test explicit opt-out, voice-communication usage, competing mic, projection revocation/lock and each named conferencing app. A proposed <=100 ms marker-alignment bound is a spike criterion, not a platform guarantee.

No spike was built or run. The first outstanding product decision is **import/review companion versus standalone microphone recorder**. Arbitrary phone/VoIP recording should be treated as an unsupported general guarantee, not silently folded into either choice. Before a device spike, specify actual handset/Android/QPR, desired headset routes and any named meeting apps; those details are unnecessary to complete this research.

## Verification performed

- Read current Repeater source and first-party platform/framework documentation; no implementation changes or Android toolchain installation.
- Executed existing pure functions on in-memory synthetic JSON on Linux: edit → Undo → Redo, exact phrase anchoring, original word excerpt selection and JSON export round trip passed. Excerpt: Me, approximately 0.82–2.18 seconds. No audio capture, model/network request or file persistence was used.
- This is **host behavior proof only**. Android imports/build/runtime, recording permissions, playback, performance, JNI word-timing parity and app-store distribution remain untested.

## Primary sources

[1] https://chaquo.com/chaquopy/doc/current/android.html

[2] https://chaquo.com/chaquopy/doc/current/versions.html

[3] https://kivy.org/doc/stable/guide/packaging-android.html and https://python-for-android.readthedocs.io/en/latest/recipes.html

[4] https://github.com/kivy/python-for-android/tree/94ffd5f31d816414ad1fe66c0fe587c61daac757/pythonforandroid/recipes/python3 and https://github.com/kivy/python-for-android/tree/94ffd5f31d816414ad1fe66c0fe587c61daac757/pythonforandroid/recipes/hostpython3

[5] https://briefcase.beeware.org/en/stable/reference/platforms/android/gradle/

[6] https://github.com/beeware/briefcase-android-gradle-template/tree/e1752348fe610d33e182185d772af4a4be0c46ee

[7] https://docs.python.org/3.14/using/android.html

[8] https://docs.python.org/3.14/library/intro.html#mobile-platforms and https://docs.python.org/3.14/library/subprocess.html

[9] https://github.com/ggml-org/whisper.cpp/tree/d1be6fde11ac6e0407606b4e42fe72d34add8037/examples/whisper.android (JNI CMake, `jni.c` and `LibWhisper.kt`)

[10] https://developer.android.com/media/platform/mediarecorder

[11] https://developer.android.com/reference/android/media/AudioRecord and https://developer.android.com/reference/android/media/AudioRecord.Builder

[12] https://developer.android.com/media/platform/av-capture and https://developer.android.com/reference/android/media/AudioPlaybackCaptureConfiguration

[13] https://developer.android.com/media/platform/sharing-audio-input

[14] https://developer.android.com/reference/android/media/MediaRecorder.AudioSource

[15] https://developer.android.com/develop/background-work/services/fgs/service-types

[16] https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start

[17] https://developer.android.com/media/grow/media-projection

[18] https://developer.android.com/develop/background-work/services/fgs/handle-user-stopping

[19] https://developer.android.com/training/data-storage/app-specific

[20] https://developer.android.com/training/secure-file-sharing/share-file and https://developer.android.com/training/data-storage/shared/documents-files

[21] https://developer.android.com/reference/android/provider/MediaStore
