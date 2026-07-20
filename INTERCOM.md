# INTERCOM.md: SDK Badge-to-Badge Comms (Hail + Channel)

`#sdk` `#intercom` `#hail` `#multiuser` `#badge-to-badge`

> Parent doc: [README.md](README.md) — the single-badge SDK voice command pipeline this builds on.
> Related reading (context only, not binding): `../multiuser/MULTIUSER.md` (scratchpad),
> `../relay/RELAY.md`, `../relay-mobile/RELAY-MOBILE.md`, `../maincomputer/MAINCOMPUTER.md`.

## Goal

Two badges on two transceivers, one server. User 1 taps and says
**"captain to engineering"** → that utterance (the caller's actual voice) plays
on badge 2 immediately → if user 2 taps within **30 s**, a **persistent
bidirectional audio channel** opens (as close to realtime as possible, no taps
needed while open) → either side closes it with a gesture.

On completion, the learnings port to `relay/`, `relay-mobile/`, and
`maincomputer/` (separate effort, discussed before execution).

## Test Bench (validated 2026-07-04)

| Role | Host | Badge |
|---|---|---|
| Transceiver 1 | PAN | COMBADGE 1 `2C:F2:DF:45:EC:28` |
| Transceiver 2 | BOX | COMBADGE 2 `1B:B8:82:88:2F:60` |
| Server (computer.py) | CUBE | — |

- Both transceivers validated end-to-end against single-threaded computer.py
  ("computer time" round-trip from each badge). BOX's documented RTL8761B SCO
  suspension did **not** reproduce — treat that history as stale pending longer use.
- BOX launches via `sdk/transceiver.sh` (inline `sudo SDK_SERVER_HOST=cube ...` —
  plain `export` does not survive sudo's env reset).
- BOX has a severed `/c/tos` — **re-run sync.sh after every SDK code change**, then
  restart its transceiver. PAN sees CUBE's files live over sshfs (restart only).
- Server port: default 1701. If maincomputer is running on CUBE, launch the SDK
  server with `SDK_SERVER_PORT=1702` (and matching env on both transceivers).

## Locked Decisions

1. **SDK-only first.** PAN + BOX as transceivers 1 and 2. Mobile app joins only
   after the SDK phases complete. `multiuser/` is a scratchpad, not a spec.
2. **Tap socket becomes the channel.** The caller's tap connection and the
   callee's answer-tap connection each go full-duplex after channel-open. No
   new connections mid-flow; close = socket close.
3. **Alias directory is flat: alias → MAC, many-to-one.** Person, role, and
   location aliases are all just strings resolving to a badge
   (`sdk/aliases.conf`, reloaded per tap, collision-checked). "Knepfler to
   Chief Engineer" and "captain to engineering" are the same mechanism.
4. **Hail is store-and-forward.** Caller's utterance is buffered from tap
   start, finalized on silence, then delivered whole to the target badge.
   (Live streaming of the hail tail is a possible later refinement; the
   target's ~1 s cold-SCO spin-up would clip a live stream's opening anyway.)
5. **Valid hails are defined by construction.** Matching only accepts
   `"<self-alias> to <target-alias>"` where self-alias belongs to the tapping
   badge and target-alias to a *different* badge. No self-hails.
6. **Close gestures may differ per platform** (precedent: mobile gesture-free
   finalize). Linux: double-tap (btmon `AT+BVRA=1`) or just the socket
   closing. Mobile (later): any tap during SCO is a system hang-up that drops
   SCO — adopted as the close gesture, not fought.

## Protocol (SDK dialect — converging with maincomputer's)

Every connection opens with an 18-byte handshake: **1 tap-type byte + 17-byte
ASCII MAC** (colon-separated, uppercase). Legacy MAC-less clients fall back to
sentinel `00:00:00:00:00:00`.

| Byte | Direction | Meaning |
|---|---|---|
| `b'1'`+MAC | relay → server | Tap session: WAV header + PCM stream follows |
| `b'h'`+MAC | relay → server | *(Phase 2)* Register persistent downlink; held open |
| `b'c'` | server → relay | Command matched — play ack chirp |
| `b'f'` | server → relay | No match / failure — play nack chirp |
| `b'v'`+size+WAV | server → relay | Voice response (4-byte BE size). On the downlink: unsolicited playback (hail delivery) |
| `b'k'` | server → relay | Keepalive — reset recv timeout, keep waiting |
| `b'W'` | server → relay | Prewarm (downlink): a hail is being captured for this badge — bring SCO up now and hold it (≤`PREWARM_MAX_S` 25 s, `audio_lock` held) so the coming `b'v'` plays instantly. Best-effort: expiry/failure falls back to the cold path |
| `b'O'` | server → relay | Channel open — this tap socket is now the live intercom: uplink stays raw mic PCM; downlink switches to framed audio |
| `b'A'`+len+PCM | server → relay | Channel audio frame (2-byte BE len) — peer's amplified mic audio; relay pipes payload into its stdin player |
| `b'X'` | server → relay | Channel closed by the other side — play close chirp, teardown |

Close-with-drain discipline (already in place): server drains inbound PCM to
EOF before closing (avoids TCP RST truncating in-flight `b'v'` payloads);
relay half-closes (`SHUT_WR`) the moment it has the signal byte.

## Phases

### Phase 1 — Concurrency + badge identity ✓ (2026-07-04)
- [x] 18-byte handshake (`b'1'`+MAC) in listener.py and computer.py
      (legacy/MAC-less fallback to sentinel).
- [x] Thread-per-connection server: shared Vosk `Model`, per-connection
      `KaldiRecognizer`, `listen(8)`, per-connection drain+close in thread.
- [x] Badge registry `{MAC: addr, last_seen}` under a lock; per-MAC log lines
      (including which MAC each voice response was sent to).
- [x] Validated 2026-07-04: "computer time" from PAN and BOX badges, including
      near-simultaneous independent sessions. Canonical `sdk/transceiver.sh`
      added (`sudo SDK_SERVER_HOST=cube python transceiver.py`).

### Phase 2 — Persistent downlink + unsolicited push ✓ code complete (2026-07-04)
- [x] listener.py: `b'h'`+MAC downlink opened at startup on a background
      thread, held open forever (replaces probe_server, which is removed);
      15 s keepalive timeout → reconnect every 5 s, replaying
      maincomputeronline.wav on each (re)connect (audible restart detection).
      Pushed `b'v'` frames play via cold-SCO sequence (ensure_hfp_profile →
      play_wav_cold → teardown). `audio_lock` serializes tap cycles vs
      pushed audio.
- [x] computer.py: downlink registry `{MAC: {sock, lock, addr}}`; `b'k'`
      keepalive every 5 s (recv-timeout cadence); per-entry send lock
      (single-writer — keepalive can't interleave into a `b'v'` frame);
      reconnect replaces stale entry safely; cleanup on EOF/error.
- [x] Server console (stdin thread): `badges` lists registry + downlink
      status; `hail <mac-substring> [text]` pushes TTS to that badge.
- [x] Wire-level smoke test passed (2026-07-04): fake downlink client on a
      test port — registration, keepalive, console hail → full `b'v'`+WAV
      frame delivered intact.
- [x] On-badge validation (2026-07-04): both downlinks UP in `badges`;
      `hail 2F:60 ...` played on idle COMBADGE 2 (86 KB frame). (Server
      restart re-announce not yet explicitly exercised — expected to fall
      out of normal use; verify opportunistically.)

### Phase 3 — Hail flow ✓ code complete (2026-07-04)
- [x] `sdk/aliases.conf` (MAC → comma-separated aliases), per-tap reload,
      collision check. **First alias = the badge's spoken name** (used in
      responses about it) — hence the name-first ordering convention.
      Seeded: `captain, bridge` / `chief engineer, engineering`.
- [x] Tap PCM buffered from stream start; `"<self> to <target>"` matched in
      real-time (cross-product, longest-first) with a final-text late catch;
      silence-finalize (`SDK_HAIL_SILENCE_S`, 1.5 s; 20 s hard cap); whole
      utterance WAV-framed and pushed to the target downlink. Server sends
      `b'k'` every ~4 s during capture and hold; **listener.py now treats
      `b'k'` as non-terminal and slides its recording deadline** (mirrors
      the full relay).
- [x] Pending-hail state (target → caller + answered-Event, `SDK_HAIL_ANSWER_S`,
      30 s). Caller's tap socket held open (keepalives + mic drain).
      **Answer-tap brought forward from Phase 4:** a tap from a badge with a
      pending hail is consumed as the answer — Phase 3 placeholder sends
      `b'c'` to the answerer and "X acknowledges." to the caller; Phase 4
      replaces this with the `b'O'` channel-open.
- [x] Failure responses via `b'v'` TTS: "There is no listing for X." (final
      text only — partials would false-positive) / "X is not available."
      (no downlink) / "There is no response from X." (window expiry).
- [x] Wire-level test passed (2026-07-04): SAPI-synthesized "captain to
      engineering status report" streamed as the caller — hail recognized,
      silence-finalized, delivered to fake target downlink; no-answer expiry
      and answered-tap scenarios both verified.
- [x] On-badge validation (2026-07-04): hail delivery, answer tap, and
      expiry all confirmed PAN↔BOX. Findings: (a) delivered hails played at
      ~20% loudness — badge-mic SCO capture is inherently low-amplitude vs
      near-full-scale TTS/chirps; fixed server-side with `normalize_pcm()`
      (peak-normalize to ~90% FS, 20× gain cap) — **re-verify loudness**.
      (b) "captain to sickbay" failed — root cause was NOT recognition but
      a **segment-discard bug**: Vosk finalizes a segment at each pause, and
      only the latest segment was kept, so speech followed by silence
      vanished before the final checks (FinalResult() flushes only the last
      — silent — segment, heard as 'huh'). Fixed: all finalized segments
      are banked and joined for both real-time matching (cross-boundary
      phrases now match) and the final checks; unknown-name remainder
      capped at 4 words so trailing noise ('huh') can't ride into the
      spoken response. Wire-regression: full Phase 3 suite + new
      "captain to sick bay" → "no listing" test, all passing.

### Phase 4 — Channel bridge ✓ code complete (2026-07-05)
- [x] Answer tap → `run_channel_answer` / caller thread → `run_channel_bridge`:
      `b'O'` to both tap sockets, then each connection thread pumps ONE
      direction (single writer per socket; per-conn send locks serialize the
      `b'X'`). Server→relay audio framed `b'A'`+2-byte len+PCM (control bytes
      stay distinguishable); relay→server stays raw. Per-chunk software gain
      `SDK_CHANNEL_GAIN` (default 12×, clip-guarded, odd-byte carry preserves
      sample alignment) — same SCO-mic-level story as hail normalization.
- [x] Relay `run_channel()`: ffmpeg uplink continues; downlink frames piped
      into long-running `pw-cat -p … -` (stdin); `b'X'`/EOF ends the channel;
      ACK chirp plays as close confirmation on both ends; normal teardown after.
- [x] **Close gesture = SINGLE tap while channel open** (fresh evdev fd, 1 s
      grace) → SHUT_WR → server closes bridge → `b'X'` both sides.
      **Deviation from original double-tap spec, flagged to Captain:**
      double-tap emits AT+BVRA, visible only via btmon/root (out of SDK
      scope), and mobile can't gesture during SCO at all — single-tap-close
      unifies with mobile's future tap=hang-up. Double-tap close can return
      in the full-relay port where btmon exists.
- [x] Wire test passed (2026-07-05): hail → answer tap → `b'O'` both sides →
      duplex PCM bridged with 12× gain verified in both directions →
      caller tap-close → `b'X'` delivered to both.
- [x] **On-badge VALIDATED (2026-07-05, Captain: "working perfectly"):**
      full loop — hail → answer tap → duplex conversation (paplay player,
      avg-magnitude noise gate, silent idle) → double-tap close from either
      badge. Volume ladder settled: chirps + tap-cycle TTS 0.5 ·
      pushed hails `SDK_PUSH_VOLUME` 1.0 (matches channel) · startup
      announces 0.5 · BOX ALSA MASTER raised 40%→100% (host-level, outside
      SDK). Tuning knobs live: `SDK_CHANNEL_GAIN` (12), `SDK_CHANNEL_GATE`
      (250 avg, 5 s stats log), `SDK_HAIL_SILENCE_S`, `SDK_HAIL_ANSWER_S`,
      `SDK_PUSH_VOLUME`.

### Phase 5 — Port learnings ✓ COMPLETE, ON-BADGE VALIDATED (2026-07-07)

**Captain confirmed all four validation tests successful (2026-07-07):**
greetings + dual voice-print auth, console hails to both badges, full hail →
answer → duplex conversation → close from either side, PAN (EC:28, Captain/
bridge) ↔ S21 (2F:60, Chief Engineer/engineering). The production intercom is
working and demonstrable. Findings: 2F:60's audio is slightly BETTER via the
S21 than it was on BOX — so BOX contributed some of the glitchiness, but the
badge itself is still suspected marginal; **Captain plans to buy a replacement
badge** (when it arrives: pair to the host, update its MAC in TOS.conf
`[intercom]` badge_* key + the relay host's `authorized_badges`/app config).
Tuning applied on hardware: `prime_ms_listening` 0 → 250 (Captain, by ear).

Ported to production per Captain's rulings (2026-07-05/06): (1) unified b'h' —
the hello connection IS the downlink, legacy clients that close after the
greeting coexist; (2) auth-gated — no hail while the session is locked, and an
answer tap from a locked badge falls through to the voice-print prompt with the
hail left pending; (3) close gestures per platform — double-tap on Linux (via
the relay's existing btmon plumbing), SINGLE tap on mobile (the system's
tap-equals-SCO-hang-up is adopted as the gesture; Bixby owns double-tap);
(4) latency reductions in scope (prime_ms_listening 0, 0.2s sink poll, no
post-cycle debounce blackout).

- [x] **maincomputer**: new `maincomputer/intercom.py` (downlink registry,
      hail flow with large-vocab silence gating, prepare_hail_pcm, prewarm,
      channel bridge with gate+gain, console badges/hail/close). Aliases +
      knobs in TOS.conf `[intercom]` (NOT a separate conf). Hail phrases ride
      the constrained per-tap grammar; "<self> to" markers give unknown-station
      hails a spoken "no listing" response. Wire-tested end-to-end 2026-07-06
      (12/14 pass; 2 fails were harness stdout-buffering artifacts, behaviors
      confirmed in the server log).
- [x] **relay/ (Linux)**: downlink_loop replaces send_hello (reconnect
      greeting = restart detection, closes the RELAY.md open item), b'W'
      prewarm, pushed b'v' (push_volume/prime_ms_push), run_channel (paplay
      primary, double-tap close via stop_recording, ffmpeg-EOF close), SDK
      tap ordering (ffmpeg→header→chirp→connect). Compile-checked; on-badge
      validation pending PAN relay restart.
- [x] **relay-mobile**: downlink thread replaces the 3s liveness poll
      (keepalive silence = offline announce; reconnect = re-hello), prewarm =
      early SCO acquire under audioLock, pushed hails at 1.0, channel mode
      (streaming AudioTrack + capture-deadline extension), single-tap close =
      SCO-drop detection. APK built + installed on S21 2026-07-06; on-device
      validation pending.

Test bench changes for the production topology: COMBADGE 2 (2F:60) moves from
BOX to the **S21** (Chief Engineer / engineering) to isolate the badge-glitch
variable from BOX hardware; PAN keeps EC:28 (Captain / bridge).

### Phase 6 — Channel audio quality tuning ✓ ON-BADGE VALIDATED (2026-07-17)

**Symptoms observed during a longer on-badge intercom session (Captain):**
1. Choppy peer audio in chunks of ~1 second (test source: badge held near a
   TV speaker playing dialogue). No particular rhythm.
2. After each transmission, ~1 s of static as the receiving badge's speaker
   output leaked into its own mic and re-transmitted back to the sender.
3. Roughly 1 in 5 words clipped at the start on quiet-onset syllables.

Diagnosis from the `channel gate: open N/M chunks, last avg X` log: the
single-threshold gate flapped whenever avg hovered near 250 — a TV speaker
picked up at distance is exactly that signal. Adjacent 5 s windows during
the same test alternated between 42/42 open at avg 271 and 0/42 open at
avg 4. Receiver logs showed `dropped 0KB` throughout, so playback
back-pressure was ruled out.

**Fixes (SDK + production `maincomputer/intercom.py` + `TOS.conf`):**

1. **Hysteretic noise gate** replaces the single-threshold flap. Gate
   opens at `channel_gate` (default lowered 250 → 40) and stays open
   until avg drops below `channel_gate_close` (auto-derived = 40% of
   open) for `channel_gate_hold_s` (default 0.8 s). Marginal signals
   that hovered near the old 250 threshold no longer chop into
   second-long windows — they either open the gate and stay open, or
   fail to open at all. New knobs: `channel_gate_close`,
   `channel_gate_hold_s`, `channel_gate_fade_ms`. Log line reformatted:
   `channel gate: open N/M chunks, hd-muted K, last avg X (open ≥Y, close <Z)`.
2. **Boundary fade** — 10 ms linear fade at every gate open/close and
   every half-duplex release edge, kills the click a hard silence↔audio
   boundary would produce.
3. **Half-duplex mode** (`channel_half_duplex_ms`, default 800):
   whenever the local badge's speaker was fed peer audio within the
   last N ms, its mic uplink is force-silenced regardless of gate
   state. The feedback path is broken structurally — no more
   speaker-into-mic echo. Canonical comms behavior; users say
   "over". Whoever grabs the floor first keeps it until 800 ms after
   their last audio-carrying chunk. Both `_pump_audio` threads share a
   two-slot `entry["speaker_last_audio"]` dict; each pump writes its
   peer's slot on emit and reads its own slot on the next emit
   decision. Set to 0 to restore full duplex.
4. `channel_gain` lowered 12 → 6 alongside the gate drop: passing more
   borderline audio and amplifying less keeps the SCO noise floor
   inaudible without clipping soft speech.
5. **Floor control** (`channel_floor_release_ms`, default 1000; added
   2026-07-19 for same-room / filming use): one talker at a time. The
   first gate to open claims the floor (shared `entry["floor"]` dict);
   the peer's uplink is hard-muted for the entire hold, and on the
   holder's gate close the floor releases into a guard window during
   which BOTH uplinks stay muted. Rationale: half-duplex (item 3) mutes
   the *listener's* mic, but with two badges physically adjacent the
   peer-speaker bleed enters the **talker's** own open mic and
   round-trips as a feedback loop — the release guard outlasts the
   round-trip so the tail echo dies instead of grabbing a gate.
   `channel_floor_max_s` (default 12) force-releases a floor pinned
   open by echo (bleed holding avg above the close threshold), capping
   any runaway. Inert with badges in separate rooms; set
   `channel_floor_release_ms = 0` to disable. Gate log gains a
   `floor-muted K` counter. SDK env mirrors:
   `SDK_CHANNEL_FLOOR_RELEASE_MS`, `SDK_CHANNEL_FLOOR_MAX_S`.
   **Bench status 2026-07-19: NOT yet sufficient at defaults** — see
   "Adjacent-badge feedback (open)" in the pending items.

**Verdict (Captain 2026-07-17):** "very much improved… clean at distance"
(with the two badges physically separated so no acoustic coupling), some
residual choppiness that further twiddling should smooth out.

**On duplex.** The Phase 4 architecture was — and mechanically still is —
full duplex: two `_pump_audio` threads run concurrently and both
directions can carry audio at the same instant. That worked from Phase 4
on when the badges weren't within earshot of each other. What broke down
was the acoustic case: a badge speaker playing peer audio at close range
leaks into its own mic, gets amplified through `channel_gain` and
re-transmitted, and the peer hears themselves as static after every
sentence. The clean fix for that is acoustic echo cancellation (adaptive
filter subtracting the downlink from the uplink) — out of scope for a
Python-in-userspace stack. Half-duplex mute (`channel_half_duplex_ms`,
default 800) is the pragmatic alternative: while the local speaker was
recently played on, the local mic is silenced structurally, so no
feedback path exists. Users say "over" in canonical style. Setting
`channel_half_duplex_ms = 0` restores the full-duplex behavior for
setups where acoustic coupling isn't an issue (badges in separate
rooms, headset use, mobile-relay-to-badge etc.).

Half-duplex covers **same-badge** coupling (my speaker → my mic). It
cannot cover **cross-badge** coupling (peer's speaker → my mic), which
only exists when both badges share a room: the bleed arrives on the
*talker's* uplink, indistinguishable from speech, while the talker's
own mute never engages. Floor control (Phase 6 item 5) targets that
case — single talker, hard peer mute, and a release guard that
outlasts the round-trip so the tail echo can't re-open a gate — but at
default thresholds the bench still feeds back via the talker's own
pinned gate; see "Adjacent-badge feedback (open)" in the pending items
for the diagnostic and next steps.

## Resume Point (2026-07-17)

**ALL PHASES (1–6) COMPLETE AND ON-BADGE VALIDATED.** The SDK
implementation was validated PAN↔BOX (2026-07-05); the production port
(maincomputer/intercom.py + relay/relay.py + relay-mobile) was validated
PAN↔S21 (2026-07-07, all four tests passed — see Phase 5 above); Phase 6
audio-quality tuning validated on-badge 2026-07-17. The SDK tree
(`computer.py`, `listener.py`, `aliases.conf`) remains as the minimal
reference implementation; production config lives in TOS.conf
`[intercom]`, not `sdk/aliases.conf`.

**Pending / open items (production, non-blocking):**
- **Adjacent-badge feedback (open, 2026-07-19)**: same-room bench test still
  feeds back with floor control at defaults. The suspected loophole is the
  **talker's pinned gate**: the floor holder's own last words play on the
  adjacent badge, bleed back into the *holder's* mic above the very low
  gate-close threshold (auto 16), so the gate never closes, the floor is
  never released, and the bleed is legally retransmitted until the
  `channel_floor_max_s` backstop (default 12 s — reads as "still broken").
  **Diagnostic (do first, next session)**: during a feedback episode read the
  maincomputer `channel gate:` lines — (a) if `floor-muted` is 0 on both
  pumps, floor control isn't engaging: code bug, hunt it; (b) if
  `floor-muted` climbs on one side while the howl continues, it is the
  pinned gate, and `last avg X` during the howl is the bleed level at the
  mic. **Tuning relief** (live in TOS.conf `[intercom]`, read at channel
  open, no restart): `channel_floor_max_s = 3` (cap bursts), and set
  `channel_gate_close` above the measured bleed avg (e.g. 60–100) so the
  echo lets the gate close. **Structural fix if tuning confirms case (b)**:
  a holder-side close boost — a knob multiplying the effective gate-close
  threshold only while holding the floor (direct speech inches from the
  mic is far louder than speaker bleed at arm's length; the boosted
  threshold discriminates them, the gate closes when speech actually
  stops, and the release guard kills the tail echo). Small `_pump_audio`
  change in both maincomputer/intercom.py and sdk/computer.py.
- **Badge hardware**: COMBADGE 2 (2F:60) suspected marginal even on the S21 —
  replacement badge planned. On arrival: pair, update TOS.conf `[intercom]`
  badge_* MAC key and the host's badge authorization (relay
  `authorized_badges` or the mobile app), re-run the four validation tests.
- **BOX contention**: 2F:60 roams between the S21 relay-mobile and BOX
  (both target CUBE's maincomputer since 2026-07-19 — `[host-box]`
  relay.target_ip now points at CUBE, no shared fs needed). Run only ONE
  of the two relays for that badge at a time or they contend for the
  Bluetooth connection.
- **Tuning by ear** (all live in TOS.conf, no restart): `channel_gain`,
  `channel_gate` (OPEN threshold), `channel_gate_close`,
  `channel_gate_hold_s`, `channel_gate_fade_ms`, `channel_half_duplex_ms`,
  `channel_floor_release_ms`, `channel_floor_max_s`
  (watch maincomputer "channel gate:" log lines during a channel — the
  `hd-muted K` / `floor-muted K` counters show how much audio the
  half-duplex and floor mutes are stopping), `hail_silence_s`, `hail_answer_s`, `push_volume`,
  `prime_ms_push`; `prime_ms_listening` settled at 250 on PAN.
- **Teleprompter**: cosmetic CHANNEL indicator (deferred from the original
  Phase 5 scope; intercom works without it).
- **Mobile (pre-existing roadmap, unchanged)**: call-yield handler (first),
  Phase 4 config screen + status signaling, Phase 5 polish — see
  `relay-mobile/RELAY-MOBILE.md` "Agreed next steps".
- **Vestigial**: maincomputer's zero-byte liveness-probe log guard is dead
  code now that the mobile poll is gone (harmless); the old
  `receive_and_play_voice`-based hello priming notes in RELAY.md describe
  the pre-downlink flow (superseded sections marked in place).
- Concurrent-dictation edge (two badges in simultaneous large-vocab
  takeovers) remains unexercised — out of intercom scope.

Older outstanding items:
- On-badge retest of "captain to sick bay" → "no listing" (segment-discard fix
  is wire-tested but not yet badge-confirmed; verdict arrives only at the 10 s
  window end).
- Opportunistic: server-restart re-announce on both badges (Phase 2 leftover).
- **Answer-latency (Captain's directive: any mechanism that shortens silence
  padding after the last spoken word is worth pursuing and eliminating).**
  Status 2026-07-05:
  - ✅ (a)+(b) `prepare_hail_pcm()` (replaces `normalize_pcm`) trims leading
    AND trailing dead air (adaptive threshold max(250, ref/8), 200 ms grace)
    then percentile-normalizes, one pass. Unit-verified: 5.5 s capture →
    2.4 s delivered. Per-hail log: `audio: trimmed lead Xms tail Yms ...`.
    **Pending on-badge re-verification of answer-tap latency.**
  - ✅ (c) relay audit: post-playback path is pw-play return → ffmpeg
    terminate (≤0.5 s) → immediate profile-off. No hidden padding. The
    1000 ms `PRIME_MS_COLD` is pre-speech lead-in (front, not tail) —
    retained for cold-sink reliability; tunable on hardware if total
    occupancy still feels long.
  - ⏳ (d) apply the same rule to Phase 4 channel close-out when built.
  - ✅ (e) **hail-to-playback latency (2026-07-05):** ~5 s from last word to
    target playback decomposed as server gate (~2 s, mostly irreducible) +
    target cold start (~3 s). Added `b'W'` prewarm — sent to the target the
    instant the hail phrase matches, so the target brings SCO up *during*
    the caller's remaining speech + silence gate; on `b'v'` with a live
    warm state the relay plays via the hot path (`prime=False`, no 1 s cold
    prime). Capture poll tightened 0.5→0.2 s. `start_sco_capture()` /
    `terminate_ffmpeg()` factored out (shared by cold path + prewarm).
    Wire regression green (hail frames also visibly smaller post-trim:
    134 KB→62 KB). Expected ~2.5–3 s; **pending on-badge measurement.**
    Remaining knob if still too slow: `SDK_HAIL_SILENCE_S` (1.5 s default).

**Next: Phase 4 — channel bridge** (see phase checklist above). Design locked:
answer-tap's `b'c'` placeholder in `handle_hail`/answer-tap check becomes
`b'O'`; both tap sockets go full-duplex raw PCM; server bridges; `b'X'` +
chirp on close; Linux double-tap close needs btmon added to listener.py;
validate one-way first (feedback), then duplex from separate rooms.

Working files: `sdk/computer.py`, `sdk/listener.py`, `sdk/aliases.conf`.
Deploy ritual: server-only changes → restart CUBE `computer.sh`; listener
changes → restart PAN transceiver + re-run BOX sync then restart BOX
transceiver. Wire tests (no badges needed) exist in the session scratchpad
pattern: fake downlink (`b'h'+MAC`) + fake caller streaming SAPI TTS
resampled to 16 kHz; reusable approach documented by example in this log.

**Open investigation (2026-07-05):** on-badge channel opens but freezes —
no audio either way, taps ignored, PAN log silent after `CHANNEL OPEN`.
(Earlier attempt: answer tap ran as a NORMAL session — pending hail gone
after the first hail failed recognition; caller re-hailed, second worked.)
Diagnosis + hardening applied to `run_channel`:
1. **Blocking-write freeze:** `player.stdin.write` into a full pipe (pw-cat
   dead/stalled) blocks the whole select loop — no uplink, no taps, no
   close. Fixed: non-blocking `os.write`, frames DROPPED under backpressure
   (counted), pw-cat stderr → `sdk/log/pwcat.log`, pw-cat exit rc logged.
2. **Tap-close likely unobservable during SCO:** the badge button belongs
   to HFP call control while SCO is up (mobile Phase 3 finding applies to
   the badge itself) — a tap may emit no evdev event and instead hang up
   the SCO link. **ffmpeg EOF during a channel is now treated as the close
   gesture** (mobile-style); evdev tap kept as secondary path.
3. 5 s heartbeat (`channel: up/down/dropped KB`) + close-time counters in
   the relay log prove loop liveness and audio movement for the next test.

**Second on-badge run (2026-07-05 07:04, PAN log):** heartbeats prove the
BRIDGE WORKS — ~32 KB/s flowing BOTH directions (BOX audio was reaching
PAN). Failures isolated to: (1) `pw-cat exited rc=1` 1 s after channel open
— PAN had no player for the incoming audio; error text went to stdout,
which was DEVNULL'd. Fixed: `--media-role=communication` ('=' form matching
the known-working play_wav call), both player output streams → pwcat.log,
and an automatic `paplay --raw` fallback if pw-cat dies. (2) Taps during
SCO confirmed to emit NO evdev event AND leave SCO up on Linux — neither
close gesture can fire from the badge; channel only ended via Ctrl-C.
Added server console **`close`** command (b'X' to both relays) as the
recovery hatch + test unblock.

**Third run (2026-07-05): INTERCOM CHAT WORKING.** Two follow-ups, both
resolved same day:
- **Close gesture = DOUBLE-TAP (Captain-approved, "only option").**
  Ported the full relay's proven btmon-under-pty pattern
  (`stdbuf -oL btmon`, match `AT+BVRA=1`; relay/relay.py
  `monitor_bluetooth_logs`) into `run_channel` — the pty master fd folds
  straight into the channel's select loop; spawned per channel, killed at
  close. Single-tap evdev path kept as a silent secondary.
- **Idle static (serious):** CHANNEL_GAIN (12×) was amplifying the SCO mic
  noise floor continuously. Added a **noise gate** in `_pump_audio`:
  chunks below `SDK_CHANNEL_GATE` (default 400 peak) are sent as true
  silence (stream stays fed — no underruns), 0.4 s hangover protects word
  tails, and gain now applies only to speech that passed the gate.
  **Pending on-badge re-verification of both.**

**Fourth run findings + fixes (2026-07-05):** intercom chat WORKING (via the
paplay fallback). (1) pwcat.log proved this pipewire's `pw-cat` rejects raw
stdin (`sndfile: Format not recognised`) — **paplay promoted to primary
player**, pw-cat demoted to fallback. (2) PAN double-tap missed: btmon
floods the pty during SCO and the single 1 KB read per loop pass fell
behind — btmon fd now **drained to would-block (≤64 KB) per pass**. BOX
double-tap worked (close via peer's b'X' confirmed end-to-end). (3)
Residual idle static: gate switched from peak to **average magnitude**
(default 250, `SDK_CHANNEL_GATE`), with a 5 s server log line
(`gate: open n/N chunks, last avg …`) as the tuning instrument.
(4) BOX badge quieter than PAN: root cause found at the ALSA layer — BOX's
card MASTER was at 40% (alsamixer), raised to 100% by Captain, resolved.
(`pactl get-sink-volume` on the bluez sink only works while SCO is up —
"No such entity" when idle is normal.) (5) Hail-vs-channel loudness
asymmetry explained: hails played via `play_wav` at hardcoded 0.5 volume
while the channel's paplay plays at 1.0. Fixed: `play_wav`/`play_wav_cold`
take a volume param; pushed voice (hails, warm + cold paths) now plays at
`SDK_PUSH_VOLUME` (default 1.0, matching the channel); chirps and tap-cycle
TTS keep 0.5.
Supporting fixes same day: relay logging (print shadow → timestamped,
MAC/host-tagged, teed to `sdk/log/*.log`; PAN's readable from CUBE);
`RECORD_MAX_S` 10→13 s (server verdict always beats relay deadline — kills
the misleading "no/unknown signal byte: None").

## Log

- **2026-07-04** — Bench validated (PAN+BOX+CUBE). Fixed voice-response RST
  truncation (server drain-before-close + relay SHUT_WR). Fixed "computer
  time" TTS phrasing (military time). SAPI voice hinted Female (Zira).
  Phase 1 implemented: handshake, threading, registry.
- **2026-07-04** — Phase 1 validated on-air (simultaneous independent
  sessions). Phase 2 implemented: persistent downlink, keepalives,
  reconnect-with-announce, cold-SCO pushed playback, server console
  (`badges` / `hail`). Wire-level push test passed; on-badge validation
  pending. Note: with output redirected on Windows, Python's cp1252 default
  can't print the `→` banner arrows — launch with `PYTHONIOENCODING=utf-8`
  if computer.py is ever run with piped output (interactive MSYS2 terminals
  are unaffected).
- **2026-07-04** — Phase 2 validated on-badge (console hail played on idle
  COMBADGE 2). Phase 3 implemented and wire-tested (both scenarios); alias
  seed per Captain: drop "knepfler", name-first ordering. Answer-tap
  detection built early with a Phase 3 placeholder ack (`b'c'` + "X
  acknowledges.") so the full hail loop is exercisable before the channel
  exists.
- **2026-07-04** — Hail loudness fix #2: peak normalization was a no-op
  on-badge — SCO captures carry near-full-scale transients (link pops, tap
  clicks) that make the absolute peak look loud while speech stays at ~5%
  FS. `normalize_pcm()` now references the **99.5th-percentile magnitude**
  (spikes clip instead of defeating the gain) and logs
  `ref/peak/gain` per hail for on-badge tuning. Unit-verified: 5% speech +
  full-scale spike → 18x gain (was 1.0x).
- **2026-07-19** — Better log lock handling: no SDK component holds a log
  file open anymore. `listener.py` no longer hands the channel player a
  persistently open `pwcat.log` handle — player stdout+stderr now go to a
  pipe drained by a daemon pump thread that opens/appends/closes
  `log/pwcat.log` per line. A log handle held open by (or passed to) a
  long-lived process can keep the file locked — on some platforms and
  network filesystems exclusively — blocking every other reader (tail,
  grep, monitoring) for the process's entire lifetime. Every log write
  now opens, appends, and closes immediately; the `log()` writers in
  `listener.py`/`transceiver.py` already did so and are unchanged.
- **2026-07-17** — Phase 6 audio quality tuning: hysteretic noise gate
  (dual threshold + hold + boundary fade) replaces single-threshold
  flap; half-duplex mute (`channel_half_duplex_ms`, default 800)
  suppresses uplink while local speaker is playing peer audio, breaking
  the speaker-into-mic feedback path structurally; `channel_gate`
  default 250 → 40, `channel_gain` 12 → 6. New knobs
  `channel_gate_close`, `channel_gate_hold_s`, `channel_gate_fade_ms`,
  `channel_half_duplex_ms`; gate log now includes `hd-muted K`. Ported
  to `maincomputer/intercom.py` + `TOS.conf` same day so SDK reference
  and production stay in sync. Full duplex remains available
  (`channel_half_duplex_ms = 0`) for setups without acoustic coupling —
  the default just makes the same-room case sound right without
  requiring adaptive AEC.
