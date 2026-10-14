# File map

One line per source file, so you can find the right file without grepping the
tree. Line counts are rough — they flag which files are worth reading whole.

## Linux desktop and CLI

| File | Purpose |
|---|---|
| `linux/parrot.py` | Explicit recording/device/transcription CLI, private XDG defaults and inference preflight |
| `linux/capture.py` | PipeWire microphone/output-monitor capture into aligned local WAV tracks and manifest |
| `linux/transcribe.py` | Manual two-profile inference, source-scoped speaker/word schema v2, legacy reads, atomic exports and complete-generation correction/Undo history |
| `linux/review.py` | Local strict-JSON-schema model client, exact revision/phrase anchors, durable runs/decisions/playback and original-word excerpts |
| `linux/desktop.py` | Private preferences/history, Archive/Restore, whole-recording OS Trash/Undo, independent capture and resident-model FIFO transcription controller |
| `linux/gui.py` | Adaptive recording management, visible transcription queue, responsive unrelated-recording workflows, serial autosave, scoped recovery and playback lifecycle |
| `linux/transcript.py` | Shared editable Thread/Timeline buffers, inline suspect underlines/review controls, Undo/Redo and per-entry Play/Stop |
| `linux/playback.py` | Source-matched finite GStreamer WAV playback; one chunk at a time with Stop/EOS/error cleanup |
| `linux/identity.py` | The Repeater display name, local application ID and selected icon source shared by GUI and installer |
| `linux/assets/app-icon.png` | Installed 512px radio-and-mustache icon exported from the selected subtle logo concept |
| `linux/assets/logo-concepts/` | Original 1024px logo alternatives; subtle radio selected, obvious trooper not installed |
| `linux/install-desktop.py` | User-local The Repeater launcher and selected-icon installation, run before GUI startup |
| `linux/setup.sh` | Dispatches explicit one/both-profile pinned local setup; no runtime/model fallback |
| `linux/setup-parakeet.sh` | Builds isolated pinned transcribe.cpp CPU/CUDA/Vulkan provider and verifies the four-slot overlap bundle |
| `linux/native-transcribe.c` | Native Multitalker Parakeet bridge with genuine word spans, stable whole-file tags and bounded source output |
| `linux/setup-nemotron.sh` | Installs isolated frozen NeMo/Torch/Python and checksum-verified ASR/V3 checkpoints |
| `linux/nemotron-transcribe.py` | Coupled eight-slot masked parallel ASR with RNNT word emissions, lexical punctuation bounds and explicit backend preflight |
| `linux/linux.mk` | Linux syntax sanity, GUI/launcher, setup, recording and behavioral-test Make targets |
| `linux/test-capture.py` | Behavioral checks for device selection, PCM alignment, processed-input routes and capture lifecycle |
| `linux/test-transcribe.py` | Speaker/word/legacy boundaries, complete-generation Undo, venv selection, stale-save rejection and failure/cancellation rollback |
| `linux/test-nemotron.py` | Unicode/byte-fallback emissions, missing alignment, delayed punctuation, late-tail bounds, checkpoint rejection and CLI failure preservation |
| `linux/test-desktop.py` | Preferences/jobs, collection/whole-recording Trash/Undo, busy discovery and optional real public-audio inference smoke |
| `linux/test-playback.py` | Actual GStreamer source/bounds/Stop checks using synthetic WAVs and a hardware-free verification sink |
| `linux/test-review.py` | Exact/local-only HTTP boundaries, persisted decisions/Undo/staleness, cancellation/rollback and original-word excerpt mapping |
| `docs/linux-port.md` | Linux prerequisites, GUI/CLI workflows, privacy, storage schema, verification and remaining port scope |
| `docs/android-feasibility.md` | Source-backed Android/code-sharing options, note-taker prior art, platform limits and unverified device-spike proposals |

## Entry points & harnesses

| File | L | Purpose |
|---|---|---|
| `Parrot/ParrotApp.swift` | 178 | `@main`; parses CLI harness flags before the SwiftUI `App` starts |
| `Parrot/ProfileTest.swift` | 1750 | `--profile-test`: headless logic harness, ~540 checks |
| `Parrot/ProfileTest+Parakeet.swift` | 130 | `--profile-test` checks for Parakeet: language router, per-side probe, recommendation, rewind, imports |
| `Parrot/SnapshotTool.swift` | 1360 | Offscreen PNG renderers + transcribe/analyze/capture harnesses; `--language-test` runs the live language check on a saved track; `--echo-replay` scores a recorded call's Me lines with the echo gate; `--liveloop-test` takes `LIVELOOP_MIC` to feed both tracks; `--ask-chat-test, --ask-real` runs a real multi-turn Ask Parrot chat against Claude or Ollama |
| `Parrot/CopilotHarness.swift` | 326 | `--kb-add`, `--doc-answer-eval` (Jev precision/recall), `--copilot-replay` (question-to-card latency) |
| `Parrot/ToneHarness.swift` | 148 | `--nudge-replay [id] [--store path]` (a saved call through the live nudge rules, on a copy of the store), `--tone-snapshot <png>` (report card, pill, banner; light + dark) |
| `Parrot/ProfileTest+Nudges.swift` | 298 | `--profile-test` checks for live nudges, the tone timeline and seconds-based talk share |

## Models (SwiftData `@Model` + Codable values)

| File | L | Purpose |
|---|---|---|
| `Models/Meeting.swift` | 166 | `Meeting` record + `MeetingStatus` lifecycle + per-speaker names/embeddings |
| `Models/TranscriptSegment.swift` | 34 | One diarized, timestamped utterance |
| `Models/Insight.swift` | 65 | `CallInsight` (stored) and `Insight` (live value) |
| `Models/CallProfile.swift` | 92 | Per-call-type prompt config: kinds, sentiment gauges |
| `Models/KindStyle.swift` | 86 | Maps insight kinds to icon/color; `Color` helpers |
| `Models/KnowledgeBase.swift` | 54 | KB document/chunk/reference value types |
| `Models/AIUsage.swift` | 144 | Token accounting and per-model price table |
| `Models/SpeakerProfile.swift` | 30 | Remembered voice: name + running-mean embedding (opt-in, local) |
| `Models/Bookmark.swift` | 50 | A marked moment (time + label); merge window, prompt line |
| `Models/Nudge.swift` | 65 | A live nudge (kind, time, text, shown or held) and the Copilot's mood timeline; JSON on `Meeting` |
| `Models/Consent.swift` | 45 | How the other side was told it's recorded; notice text |
| `Models/OnboardingFlow.swift` | 79 | Setup steps per mode and path, named-step migration, memory-based model picks |

## Services

| File | L | Purpose |
|---|---|---|
| `Services/RecordingManager.swift` | 1267 | Orchestrates a recording session end-to-end; the hub; "Still recording?" reminder; live speaker sweeps (stable/window mapping, power pacing) |
| `Services/AudioCaptureManager.swift` | 1022 | System audio (tap on 15+, SCK on 14.x/rescue) + mic tap, buffer conversion |
| `Services/SystemAudioTap.swift` | 294 | Core Audio process tap: audio-only capture, no Screen Recording (macOS 15+) |
| `Services/EchoCanceller.swift` | 138 | Swift wrapper over vendored SpeexDSP AEC |
| `Services/EchoGate.swift` | 123 | Pure: skips a Me clip whose loudness just follows the other side's (speaker echo), once the call shows the mic hears the speakers |
| `Services/TranscriptionEngine.swift` | 1753 | On-device WhisperKit or Parakeet; `AudioSource` routing; per-side language check (holds a Parakeet side until known, recheck + rewind); lazy fallback Whisper; live preview decode; Silero voice gate before every decode |
| `Services/LanguageRouter.swift` | 142 | Pure: which engine each side of a Parakeet call uses (held, Parakeet, Whisper), recheck schedule; `LanguageProbe` gathers each side's first 10 s of speech |
| `Services/ParakeetTranscriber.swift` | 38 | Parakeet TDT 0.6B v3 via FluidAudio (25 European languages, ~0.5 GB): load, transcribe with a script hint |
| `Services/EngineRecommendation.swift` | 25 | The model a new install starts on: Parakeet only when every Mac language and past call fits its 25 |
| `Services/CloudTranscription.swift` | 392 | Opt-in Groq (batch) and Deepgram (streaming) backends + WAV encode |
| `Services/DiarizationEngine.swift` | 156 | FluidAudio pyannote diarization (CoreML): labels + per-speaker embeddings; whole file or a live 60 s tail |
| `Services/AnalysisProvider.swift` | 605 | `AnalysisProvider` protocol, request/result types, prompt building, **Keychain helpers** (~L575) |
| `Services/OpenAICompatibleProvider.swift` | 528 | OpenAI-shaped LLM client (incl. Ollama); provider switching |
| `Services/CallAnalysisEngine.swift` | 815 | Drives live Copilot passes; per-pace question floor; Jev fast path ("From your docs" excerpt) |
| `Services/JevDocMatcher.swift` | 175 | TypeSafe "Jev" client: one probability per KB chunk that it answers the question; same-issue verdicts for card dedup |
| `Services/KnowledgeBaseService.swift` | 532 | Ingests/chunks KB docs (heading-aware), on-device multilingual embeddings (re-embeds stale vectors), hybrid BM25 + embedding retrieval |
| `Services/ProfileStore.swift` | 111 | Persists and mutates `CallProfile`s |
| `Services/ProfilePresets.swift` | 170 | Built-in starter profiles (seven, incl. the buyer-side "Vendor call") |
| `Services/ExportService.swift` | 265 | Export: TXT, SRT, Markdown (front matter, next-step checklist instead of repeated sections); `Parts` limits what an AI app gets |
| `Services/PermissionFlow.swift` | 150 | System Audio (15+) / Screen Recording (14) + microphone grant flows |
| `Services/AppUpdater.swift` | 56 | Sparkle updater: daily signed appcast check, installs on quit |
| `Services/BugReport.swift` | 120 | Pre-filled GitHub issue: diagnostics, own-window screenshot, URL builder |
| `Services/SpeakerProfileStore.swift` | 85 | Voiceprint matching (cosine ≥ 0.65), narrowed to calendar invitees; remember/forget |
| `Services/Receipts.swift` | 175 | Report receipts: parse `[mm:ss]` stamps, verify against the transcript, commitment/placeholder rules |
| `Services/GlobalHotKey.swift` | 101 | Carbon system-wide shortcut (⌃⌥M mark), registered only while recording |
| `Services/CallDetector.swift` | 281 | Mic-in-use reading (Core Audio process list) + pure call start/end state machine, app names; ignores Siri, Parrot's own capture, dictation apps |
| `Services/CallWatcher.swift` | 356 | Polls the detector; Ask/Auto modes; notification actions + delegate; calendar reminders; `NotificationAccess` |
| `Services/CalendarService.swift` | 250 | EventKit read-only: current event match, notes cleaning, invite context, title → profile |
| `Services/LoginItem.swift` | 60 | "Open Parrot at login" via SMAppService.mainApp |
| `Services/NudgeDetector.swift` | 293 | Pure rules for the nine live nudges (timing + Copilot passes) and the rate limiter; `Tuning` holds every threshold |
| `Services/LiveNudgeSession.swift` | 60 | One call's nudges: detector, mood snapshots, the nudge on screen; fed by RecordingManager |
| `Services/ToneTimeline.swift` | 147 | Report maths: talk seconds per minute, talk share, turning points, numbered moments |
| `Services/MeetingMemory.swift` | 300 | Local index of finished meetings (chunks + on-device vectors, one file per meeting), hybrid search (optionally one kind of passage) |
| `Services/AskChatStore.swift` | 170 | Ask Parrot's saved chats: AskMessage/AskChat values, one JSON file, rename/delete/stale sweep, titles, day groups |
| `Services/AskEngine.swift` | 260 | Ask Parrot prompt/context, `[M2 12:34]` citation parsing + checks; LastCallBrief (previous meeting, open items) |
| `Services/RecordingManager+Memory.swift` | 110 | meetingFinished hook, memory sync, previous meeting, `ask()` |
| `Services/FollowUpEmail.swift` | 90 | Follow-up email prompt, subject/body split, open in Mail |
| `Services/Integrations.swift` | 230 | Apple Reminders, export folder (security-scoped bookmark), webhook (payload, HMAC, send) |
| `Services/RecordingManager+Integrations.swift` | 90 | After-call actions, follow-up drafting, next steps → Reminders |
| `Services/MCPServer.swift` | 330 | `--mcp`: read-only stdio MCP server (async loop; list/get/search meetings with meaning search and date/person filters, transcript pages, commitments, prompts, read-only annotations, export to Downloads/Parrot Exports, talk-time stats, read profiles; share settings via MCPAccess), opt-in, private meetings hidden |
| `Services/MCPCommitments.swift` | 55 | Pure: commitment bullets from a report, owner = speaker of the cited receipt line |
| `Services/MCPPrompts.swift` | 115 | Pure: the four ready-made MCP prompts (weekly digest, follow-up email, call prep, PRD from calls) |
| `Services/ProfileFile.swift` | 250 | Pure: portable `.parrotprofile` JSON (encode a CallProfile, decode with limits, unknown fields kept) |
| `Services/MCPAccess.swift` | 110 | What AI apps may see (share checkboxes, excluded call types), the gate every MCP tool reads through, activity counters |
| `Services/MCPBundle.swift` | 150 | One-click connect: Claude Desktop `.mcpb` (manifest, launcher that finds a moved app, icon), Cursor link, Claude Code / Codex commands |
| `Services/ParrotLink.swift` | 45 | Open-in-Parrot links: AI apps get `openparrot.app/open#m=<id>&t=` (the site hands on to `openparrot://`), ParrotAppDelegate opens them, reopening the window if closed |
| `Services/CloudGate.swift` | 60 | On-device-only switch: global or per-call holds; checked by every cloud path |
| `Services/Redactor.swift` | 200 | Hide emails/phones/cards/IBANs/names from cloud AI and restore them; request/result helpers |
| `Services/Retention.swift` | 55 | Automatic clean-up rules (audio / whole meetings after N days) |
| `Services/RecordingManager+Privacy.swift` | 100 | Consent recording, retention run, PrivacyLedger ("what left this Mac") |
| `Services/CopilotSetupState.swift` | 91 | Settings each Copilot path writes, auto-enable when the Ollama model lands, Copilot status for Ready and Home |
| `Services/ProviderKeyCheck.swift` | 56 | One-request Claude and Deepgram key checks |
| `Services/OllamaService.swift` | 134 | App-wide Ollama server state and the one model pull; resumes at launch |
| `Services/OllamaInstaller.swift` | 133 | Downloads, unpacks, signature-checks and opens the official Ollama app |

## Views

| File | L | Purpose |
|---|---|---|
| `Views/ContentView.swift` | 200 | Root split view (`MainPage`: dashboard/settings/ask/aiApps/meeting) + empty state + corner bug button |
| `Views/SidebarView.swift` | 361 | Meeting list, rows, talk-ratio strip |
| `Views/DashboardView.swift` | 350 | Landing stats + recent meetings |
| `Views/CopilotHomeCard.swift` | 110 | Home card: turn on Copilot, finish setup, waiting for the model, just turned on |
| `Views/LiveRecordingView.swift` | 774 | In-call screen: chat bubbles, mic level, side tabs |
| `Views/CopilotPanelView.swift` | 770 | Live insight cards, pinned blockers, suggested replies |
| `Views/BriefViews.swift` | 147 | Brief summary line, documents-in-play row, live "Briefed" card (dashboard + copilot panel) |
| `Views/SettingsCards.swift` | 187 | Settings building blocks: page, titled card, row, tag chip (the landing-page window look) |
| `Views/MeetingDetailView.swift` | 1250 | Post-call tabs: transcript, insights, report; receipts actions, bookmarks card/rows; speaker naming popover (+ invitee suggestions) |
| `Views/BugReportSheet.swift` | 150 | Bug/idea report form + the corner ladybug button |
| `Views/ReportContentView.swift` | 473 | Report section cards, talk-ratio bar, prose parser (incl. one-line local reports), receipt chips + popover |
| `Views/SentimentStripView.swift` | 60 | Sentiment gauge strip |
| `Views/SettingsView.swift` | 970 | All settings sections, provider keys, KB docs |
| `Views/ProfilesSettingsView.swift` | 720 | Call-profile editor: kinds, gauges, icon picker |
| `Views/OnboardingView.swift` | 182 | Setup sheet shell: step routing, footer, 600×680; PermissionRow, ModelOption |
| `Views/Onboarding/OnboardingModel.swift` | 81 | Sheet state (mode, path, step, switch, key results); move/decide later/finish |
| `Views/Onboarding/OnboardingParts.swift` | 140 | Shared rows and cards: StepHeader, CopilotHeroCard, DownloadRow, PendingRow, SpeechDownloadRow |
| `Views/Onboarding/WelcomeStep.swift` | 22 | Welcome step |
| `Views/Onboarding/PermissionsStep.swift` | 120 | Permissions step |
| `Views/Onboarding/MeetCopilotStep.swift` | 111 | Meet Copilot step: example call + benefits |
| `Views/Onboarding/CopilotPathStep.swift` | 86 | Private / Balanced / Cloud / Decide later choice |
| `Views/Onboarding/SpeechModelStep.swift` | 82 | Speech model step (pick from languages, past calls and memory; starts download) |
| `Views/Onboarding/CopilotSetupStep.swift` | 164 | Copilot path setup: Ollama install/pull, Claude/Deepgram key fields |
| `Views/Onboarding/KeyCheckField.swift` | 83 | One provider key field + Check key button and result |
| `Views/Onboarding/AutomaticStep.swift` | 104 | In-progress step while a path finishes on its own |
| `Views/Onboarding/AIAppsStep.swift` | 110 | Claude/Cursor/Codex step before Ready (not on the Private path): honest "not private" note, Connect Claude |
| `Views/Onboarding/ReadyStep.swift` | 70 | Final "Ready" screen |
| `Views/ModelDownloadProgressView.swift` | 34 | Whisper model download progress bar |
| `Views/OllamaModelStatusView.swift` | 65 | Settings → Copilot model status, a thin view over OllamaService |
| `Views/AudioImport.swift` | 108 | Drag-drop / file import of existing audio |
| `Views/AppCommands.swift` | 319 | `AppSession`, menu commands, context menus, notifications |
| `Views/MenuBarView.swift` | 78 | Menu bar extra |
| `Views/Theme.swift` | 160 | Single source of colors, fonts, metrics |
| `Views/NudgePill.swift` | 156 | The floating nudge pill (non-activating panel, hidden from screen capture) and the Copilot panel's nudge banner |
| `Views/ToneTimelineCard.swift` | 193 | Report card "How the call went": talk bars, mood line, numbered moments with Play |
| `Views/AutomationSettingsViews.swift` | 250 | Login item row, Call Detection + Calendar cards, detected-call banner |
| `Views/AskPageView.swift` | ~330 | Ask Parrot page: saved-chat list, conversation, AI menu, Stop |
| `Views/AskAnswerView.swift` | ~130 | ParrotAvatar + AskAnswerView (answer lines, citation chips, sources) |
| `Views/ConnectionsPrivacySettings.swift` | 255 | Settings → Connections (folder, email, webhook, switch + link to Claude & AI Apps) and → Privacy (lock, redaction, consent, clean-up) |
| `Views/AIAppsPageView.swift` | 440 | Claude & AI Apps page (connect buttons, what apps see, six jobs, activity line), first-connection banner, meeting "Ask Claude" menu, weekly report tip; `AIApps` pure rules |

## Build & non-source

| Path | Purpose |
|---|---|
| `Makefile` | Canonical build: `swift build` + manual `.app` assembly |
| `.github/workflows/ci.yml` | macOS CI: build, `--profile-test`, snapshot renders (artifact), ad-hoc `.app` assembly |
| `project.yml` | xcodegen input; `Parrot.xcodeproj` is generated from it |
| `Package.swift` | SwiftPM deps (WhisperKit, vendored CSpeexDSP) |
| `scripts/release.sh` | Release packaging; mirrors the Makefile's bundle step; builds `Parrot.mcpb` and `server.json` |
| `scripts/assemble-help.sh` | Builds the Apple Help Book into the .app from `docs/help/` (both builders call it) |
| `integrations/claude-plugin/` | Claude plugin for the connector directory: `.mcp.json` + `server/launch.sh` (same launcher as the release `.mcpb`, harness-checked), four skills mirroring the MCP prompts, README, PRIVACY |
| `server.json` | MCP Registry entry, written by `scripts/release.sh` per release (version, `.mcpb` URL, sha256) |
| `docs/help/` | User guide: one HTML set serving GitHub Pages AND the in-app Help menu |
| `Vendor/CSpeexDSP/` | Vendored C echo canceller — do not modify |
| `docs/IMPROVEMENT-ROADMAP.md` | Roadmap + build notes (incl. the Xcode race) |
| `docs/PERFORMANCE.md` | Performance findings |
| `docs/superpowers/` | Design specs and plans |
