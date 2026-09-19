# INTERCOM.md: SDK Badge-to-Badge Comms (Hail + Channel)

`#sdk` `#intercom` `#hail` `#multiuser` `#badge-to-badge`

> Parent doc: [README.md](README.md) — the single-badge SDK voice command pipeline this builds on.
> Related reading (context only, not binding): `../multiuser/MULTIUSER.md` (scratchpad),
> `../relay-linux/RELAY.md`, `../relay-mobile/RELAY-MOBILE.md`, `../maincomputer/MAINCOMPUTER.md`.

## Goal

Two badges on two transceivers, one server. User 1 taps and says
**"captain to engineering"** → that utterance (the caller's actual voice) plays
on badge 2 immediately → if user 2 taps within **30 s**, a **persistent
bidirectional audio channel** opens (as close to realtime as possible, no taps
needed while open) → either side closes it with a gesture.

On completion, the learnings port to `relay-linux/`, `relay-mobile/`, and
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
   **Superseded by Phase 10 (2026-09-12):** one gesture everywhere, a single
   tap. On Linux it is the same hang-up, seen as `AT+CHUP` on btmon.

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
| `b'X'` | server → relay | Channel closed by the other side — speak "Channel closed." (`channelclosed.wav`), teardown |
| `b'H'` | server → relay | *(Phase 9)* Hail pending (downlink), sent just before the hail itself: the next tap answers it, so skip the `listening.wav` chirp. Payload-free |
| `b'E'` | server → relay | *(Phase 9)* Hail ended (downlink): the answer window closed unanswered (or delivery failed); taps are commands again. Payload-free |

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
Tuning applied on hardware: TOS.conf `[relay] prime_ms_listening` 0 → 250
(Captain, by ear). The SDK's counterpart is the module constant
`PRIME_MS_LISTENING` in `listener.py`, which had sat at 0 for two months — the
tuning had travelled one way only.

⚠ **The SDK's value is 160, not 250, and the difference is not an oversight.**
What protects the chirp is the total silence at the head of the stream: the
prime PLUS the chirp file's own lead-in. The two projects play different chirps
— TOS's is 309 ms and starts at full level; the SDK's is 671 ms and opens with
90 ms of silence. 160 + 90 gives the same 250 ms of protection, and copying 250
would add 90 ms of dead air to every tap. Confirmed by blind A/B on PAN
(12 trials): 0 ms clipped 4/4, 160 ms and 250 ms clean 4/4 each.

Ported to production per Captain's rulings (2026-07-05/06): (1) unified b'h' —
the hello connection IS the downlink, legacy clients that close after the
greeting coexist; (2) auth-gated — no hail while the session is locked, and an
answer tap from a locked badge falls through to the voice-print prompt with the
hail left pending; (3) close gestures per platform — double-tap on Linux (via
the relay's existing btmon plumbing), SINGLE tap on mobile (the system's
tap-equals-SCO-hang-up is adopted as the gesture; Bixby owns double-tap);
(4) latency reductions in scope — the SDK's `PRIME_MS_LISTENING` 0 carried into
TOS.conf as `prime_ms_listening` 0, 0.2s sink poll, no post-cycle debounce
blackout. (TOS later moved to 250 and the SDK to 160 — the same protection through
different chirp assets; see the hardware-tuning note above.)

- [x] **maincomputer**: new `maincomputer/intercom.py` (downlink registry,
      hail flow with large-vocab silence gating, prepare_hail_pcm, prewarm,
      channel bridge with gate+gain, console badges/hail/close). Aliases +
      knobs in TOS.conf `[intercom]` (NOT a separate conf). Hail phrases ride
      the constrained per-tap grammar; "<self> to" markers give unknown-station
      hails a spoken "no listing" response. Wire-tested end-to-end 2026-07-06
      (12/14 pass; 2 fails were harness stdout-buffering artifacts, behaviors
      confirmed in the server log).
- [x] **relay-linux/**: downlink_loop replaces send_hello (reconnect
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

### Phase 7 — TOS hardware findings ported back ✓ (2026-09-07)

The intercom traveled SDK → TOS. **This is the return trip.** TOS spent an
evening on real badges chasing "crap shoot transmission, static ping-ponging
between the badges even 15 feet apart", and the fix is two defaults, both of
which are structural findings rather than room tuning. Applied here.

| knob | was | now |
|---|---|---|
| `SDK_CHANNEL_HALF_DUPLEX_MS` | 800 | **150** |
| `SDK_CHANNEL_GATE_HOLD` | 0.8 | **0.5** |

**1. The half-duplex mute was guarding a path the hardware already guards, and
charging the reply for it.** These badges advertise `AT+BRSF=671` with bit 0
(EC/NR) set — they have their own echo canceller, and nothing in the stack
disables it. HFP puts the canceller in the headset by design, which is also why
these badges do ordinary full-duplex phone calls.

Worse, `speaker_last_audio` is refreshed on **every** emitted chunk, so `N` was
never "N ms of decay" — it was *the peer's whole transmission, plus N ms*. At
800 that tail lands precisely where an answer begins. Measured: peaks of **445
and 400** in the audio the mute discarded, against a 3-6 idle floor and speech
at 145-370. It was throwing away real speech. Engagement fell from **35% of
chunks to 7%** at 150.

**2. `CHANNEL_GATE_HOLD` is a transmit tail.** For that long after the last
word the gate is still open and still sending room noise, amplified by
`CHANNEL_GAIN`, out of the peer's speaker — where it can re-open the peer's
gate. That is the ping-pong. 0.8 → 0.5 cut gate-open time from 47% to 32% and
took post-transmission static to near zero. **0.4 is the measured floor**; below
it, mid-sentence pauses get chopped.

#### The two room calibrations, and why they are shipped anyway

`SDK_CHANNEL_GATE` **40 → 50** and `SDK_CHANNEL_GAIN` **6 → 3**, matching TOS.

These two *are* room calibrations rather than findings, and the first instinct
was to leave them — calibrations do not transfer between rigs, and copying one
is a mistake this project has made before. **That reasoning was wrong here, and
the Captain caught it:**

> given that these two badges and my environment are the ONLY things ever tested
> with TOS or the SDK to my knowledge, I would think that we should apply the
> `channel_gate` and `channel_gain` as well, since at least it's a known-good
> config even if specific to my badges and my environment. Better for new users
> to start with that, than with guesses.

The old 40 and 6 were not neutral defaults — they came from **the same two
badges in the same rooms**, just an earlier and less-informed session. There is
no second rig. Choosing between two calibrations from one environment, the one
that was measured last and demonstrably held a clean channel wins.

**So: where these numbers come from, stated plainly.** Two Star Trek combadges
in one home, in relatively quiet rooms with some hum, hiss and cricket noise.
Measured bounds there: room noise max **5.6**, quietest quarter of speech
**p25 = 90**. That environment is why 50 works; a workshop, an office with HVAC,
or a badge with a hotter mic will want different numbers. **Expect to tune
these two**, and use the 5 s gate log line as the instrument.

`GATE` is calibrated against a **128 ms** averaging window, which is what this
stack naturally produces — `listener.py` uplinks with
`ffmpeg.stdout.read(4096)`, a blocking pipe read, and 4096 B at 16 kHz mono s16
*is* 128 ms. Relevant if you replace the transceiver; see the known gap below.

`GAIN` applies **after** the gate, so it sets loudness to the listener and does
nothing for quiet speech getting through. **Raise it first if the peer sounds
too quiet.**

The **ordering lesson** travels even where the numbers do not, and it is
counter-intuitive enough to be worth stating: **fix the transmit tails first,
then set the gate.** Lowering `GATE` to make quiet speech carry also makes
returning echo easier to latch, so TOS's 40 → 25 helped the Captain be heard and
fed the runaway at the same time. Once the tails were fixed there was far less
for the threshold to trip on — so TOS *raised* it to 50 and got a **more**
responsive channel.

#### ⚠ Also ported: a guard against inverted hysteresis

**`CHANNEL_GATE_CLOSE >= CHANNEL_GATE_OPEN` is an oscillator, not a gate.** It
opens at `OPEN`, and the average is already above `CLOSE`, so the hold timer
resets forever and the channel transmits continuously — room noise, amplified by
`CHANNEL_GAIN`, straight into the peer's speaker.

Not hypothetical: it shipped for an evening on TOS (2026-09-07). `CLOSE` was
pinned at 16 while `OPEN` was lowered to 10 to chase microphone sensitivity, the
pair inverted, and it was audible as continuous **static ping-pong** between the
badges. It cost a test session before the printed thresholds gave it away.

The SDK is *more* exposed than TOS was, because `SDK_CHANNEL_GATE_CLOSE` can be
pinned by env var while `SDK_CHANNEL_GATE` is tuned independently. There is now
a load-time clamp (to 40% of open, with a message saying what it did) plus a
warning when `CLOSE` falls below **8**, the floor below which measured room noise
keeps the hold timer alive. ~~The default is left as the **auto 40%-of-`OPEN`
rule** rather than TOS's pinned 16, because the rule survives someone re-tuning
`OPEN` and a pinned number would not — that gives `CLOSE` = 20 here against
TOS's 16, a small difference in the safe direction.~~

**Changed 2026-09-18 (Captain): the default is now TOS's pinned 16**, the value
tested at 5 ft and in one room, in place of the untested 20. The cost is the one
the struck reasoning names: 16 does not follow `OPEN`. The clamp covers the
dangerous direction. Lower `SDK_CHANNEL_GATE` to 16 or below without also
setting `SDK_CHANNEL_GATE_CLOSE`, and close falls back to 40% of open, with the
warning. The pair 50/16 is the one TOS has run and heard through every
intercom session since 2026-09-07, including the 5 ft settlement and the
same-room retest. How the two relate, and how to move them together: *How
`GATE` and `CLOSE` relate* under the defaults table below.

⚠ Both warnings are **ASCII-only on purpose**: they run at module import, before
`__main__` reconfigures stdout to UTF-8, so a non-ASCII character in them is a
hard `UnicodeEncodeError` at startup on a cp1252 console — the default on
Windows. A warning about a misconfiguration must not itself crash the server.

#### ⚠ Known gap, not ported: the gate's averaging window is a transport artefact

`_pump_audio` averages over whatever one `recv()` returns. TOS measured **128 ms
per chunk on one relay and 17 ms on another** — a 7.5x difference in averaging
time against one shared threshold, with the short-window end running roughly
double the gate-open rate on the *same* channel. TOS fixed it with a
fixed-interval window (`channel_gate_window_ms`); the SDK has not.

If both ends of your channel run the same transceiver this is symmetric and
harmless. If they do not, your thresholds mean different things at each end, and
that is worth knowing before you tune either.

#### What none of this fixes

The **cross-badge** path — badge A's speaker into badge B's mic. Neither badge
has any reference for the other's output, so no canceller at any layer can
subtract it, and no host-side AEC (NLMS, WebRTC AEC3, PipeWire
`module-echo-cancel`) can either. Below roughly **5 feet** it runs away into
feedback. TOS's answer is proximity detection with automatic channel
termination; it is not built yet, and more suppression is not the answer.

Measured operating envelope after this change, on TOS hardware: **usable down to
5 ft**, with the nearer badge winning, and static near zero.

⚠ **Superseded by Phase 8:** with muted audio no longer able to open the gate,
the same badges were measured usable *within 3 ft*, and self-calming when
nearly touching.

#### Current channel defaults — the authoritative list

⚠ **Read these, not the numbers quoted in Phases 4 and 6.** Those are accurate
records of what was true *at that phase* and several have moved since; a couple
(`GAIN` 12, `GATE` 250/400-peak) predate the switch from peak to average
magnitude entirely.

| env var | default | kind |
|---|---|---|
| `SDK_CHANNEL_GAIN` | **3** | calibration — per badge/host. Raise if the peer is too quiet |
| `SDK_CHANNEL_GATE` | **50** | calibration — per badge/room/transport. The one to tune first |
| `SDK_CHANNEL_GATE_CLOSE` | **16** | calibration — TOS's tested value, pinned 2026-09-18 (was 40% of `GATE` = 20). Clamped to 40% of `GATE` if >= `GATE`; warns below 8 |
| `SDK_CHANNEL_GATE_HOLD` | **0.5** | finding — floor is 0.4 |
| `SDK_CHANNEL_GATE_FADE_MS` | 10 | structural |
| `SDK_CHANNEL_HALF_DUPLEX_MS` | **150** | finding — **do not go to 0** (Phase 8) |
| `SDK_CHANNEL_FLOOR_RELEASE_MS` | **400** | finding — the tested configuration in both placements (Phase 8); 0 disables floor control |
| `SDK_CHANNEL_FLOOR_MAX_S` | 12 | cap on a pinned floor |

The **kind** column is what matters when you tune. A *finding* is about the
badge hardware or this code and should hold anywhere. A *calibration* is about
one quiet home with two badges — shipped because it is the only configuration
ever measured and a known-good starting point beats a guess, but it is a
starting point and not a universal default.

**As a whole this set is a tested configuration**, not an assembly of
independently-chosen numbers: two badges held a clean channel on it down to
about 5 ft. Change one at a time and watch the gate log.

#### How `GATE` and `CLOSE` relate — tune them as a pair

The gate has two thresholds and they do different jobs. **`SDK_CHANNEL_GATE`
(open) decides what starts a transmission. `SDK_CHANNEL_GATE_CLOSE` decides
when it ends.** Once open, the gate stays open until the level has been below
close for `SDK_CHANNEL_GATE_HOLD` (0.5 s). Both are in the same units, the
mean |sample| over a 128 ms window. That is one 4096-byte read from the stock
transceiver, and the same figure the server's `channel gate:` log line prints.

The defaults, against what was measured on the calibration rig (two badges,
one quiet home, TOS `controlpanel/TUNING.md` §4b and Part I):

| measured at the badge mic | level | what it means for the pair |
|---|---|---|
| room noise | max **5.6** | close must sit clearly above it or the gate never shuts. 16 is about 3x |
| this badge's own speaker, still ringing in its mic after the far side stops talking | peaks **~18-36** | open must sit above it or every transmission bounces back (Phase 8). 50 does |
| quietest quarter of the talker's speech | **~90** (p25) | open must sit below it or quiet words are lost. 50 does |
| typical speech | **145** median, 370 at p90 | comfortably above both |

So the working band is **noise floor < close < bounce level < open < quiet
speech**: 5.6 < 16 < ~36 < 50 < 90. Two rules keep it working:

1. **Close strictly below open.** Equal or above is an oscillator (above).
   The code clamps it to 40% of open and warns.
2. **Close clearly above the room.** Below 8 the gate may never shut, and
   floor control releases only on a close, so one badge can hold the channel.
   The code warns and does not clamp.

**Moving them.** Tune open first. Raise `SDK_CHANNEL_GAIN` first if the
far side is only *quiet*: gain is applied after the gate decision, so it
makes speech louder without changing what opens the gate. Then set close
**explicitly** to about a third of open (16/50 = 32%). It is pinned, not
derived, so it will not follow open on its own. If you lower open
toward 20, close ends up near the noise floor. That is the point to measure
your own room rather than keep lowering.

### Phase 8 — Same-room use, no more static loops: muted audio no longer opens the gate ✓ (2026-09-11; verified in TOS and on-badge in the SDK)

**The flaw.** `_pump_audio` decided the gate on every chunk and only *then*
masked it with the half-duplex and floor mutes. So audio that was never going
to be sent could still **open** the gate. The gate then held for
`CHANNEL_GATE_HOLD` after the mute ended, and sent that hold: room noise times
`CHANNEL_GAIN`. The far badge played the noise. About 0.45 s later it unmuted
into its own room ring, and its gate did the same thing back. The result was a
self-sustaining exchange of held-open gates, one round every ~1.3 s: *"static
every second or two, looping."*

Why the far badge hears itself at all: its echo control is a **switch, not a
canceller**. It mutes its own mic while its speaker carries any sound, and lets
go 10–25 ms after the speaker stops, while the room is still ringing with that
sound for up to ~250 ms. That was measured in TOS with a second microphone as
witness, on both relay platforms.

**Found and verified in TOS**, with a per-chunk trace of the maincomputer's
channel pump:

| | spoken | bounces | longest chain |
|---|---|---|---|
| before, noisy room | 7 | 55 | 16 |
| before, quiet room | 17 | 11 | 4 |
| **after** | 20 | **0** | 0 |

After the fix, nine gate openings were blocked, each of them what used to
start a chain, and no static was heard. Then, in one room: the nearer badge
transmits within **3 ft**, and when the badges are nearly touching, a slight
echo dies away on its own. TOS has shelved its planned proximity
auto-termination on the strength of it.

**The fix.** A chunk that the half-duplex or floor mute will discard now
reaches the gate as silence (`last_avg = 0`), so it can neither open the gate
nor hold it open. `muted_input` is computed ahead of the gate decision; the
floor block afterwards can only add a mute for that chunk, never lift one.

**SDK differences to know:**
- The SDK decides the gate per chunk rather than over a fixed window (Phase
  7's known gap), so the zeroing applies per chunk.
- **Floor control now ships ON, at 400 ms** (`SDK_CHANNEL_FLOOR_RELEASE_MS`;
  it was 0, disabled). That is the value every same-room result above was
  measured with, and the SDK's on-badge test passed on it. Set it to 0 to
  disable floor control; the fix still covers every half-duplex mute.

**Correcting Phase 7:** `SDK_CHANNEL_HALF_DUPLEX_MS` at 0 is **not** "a small
step". The mute is what keeps the badge's own ring out of the gate in the
moments after its speaker stops. Keep it at 150.

### Phase 9 — The channel says so ✓ (2026-09-11)

Found in the first SDK on-badge test of Phase 8 (PAN + BOX, server on CUBE),
by the Captain. The SDK's cues are *spoken* clips, where TOS plays tones, so
two of them said the wrong thing:

- **The answering tap said "listening."** The listener plays `listening.wav`
  at step 4 of every tap, before it has connected and so before it can know
  the tap answers a hail. Now the server sends **`b'H'`** down the target's
  downlink just *before* pushing the hail. The downlink handles bytes in
  order, so the listener knows the next tap answers by the time the hail has
  finished playing, and that tap skips the chirp. **`b'E'`** clears it when
  the window closes unanswered or delivery fails; so do any tap and any
  downlink reconnect, with a 45 s backstop in case `b'E'` never arrives.
- **Opening now says "Channel open."** — on both badges, since both receive
  `b'O'`, which also tells the caller that the hail was answered. It plays in
  a thread, so the pump starts at once and peer audio never queues behind the
  clip. The uplink is sent as **silence** while it plays and for 0.35 s after
  (`ANNOUNCE_TAIL_S`). That covers the badge unmuting into its own ring of the
  clip, which is the Phase 8 bounce trigger. Without it, the announcement
  itself could open the gate.
- **Closing said "command executed."** It now says **"Channel closed."**
  (`channelclosed.wav`), on both badges.

Both clips were generated with `tts.sh`, the same engine as every other
spoken asset. Both new bytes are payload-free, so an older listener logs them
as unknown and keeps its old cues, and an older server simply never sends
them.

### Phase 10 — One tap ends whatever the badge is doing ✓ code (2026-09-12)

Ported from TOS, where it was built and verified on the badge the same day on
relay-windows, relay-linux and relay-mobile (TOS `controlpanel/TUNING.md` →
item 1c). **The rule: one tap starts a recording, the next tap ends it — or
closes a channel.**

- **Why a single tap did nothing on Linux.** While SCO is up the badge button
  is call control, and a single tap sends a hang-up. Captured on PAN with
  btmon, one tap mid-recording:
  `21 ef 11 41 54 2b 43 48 55 50 0d 80  !..AT+CHUP..`. BlueZ has no call to
  hang up, and the listener watched only for `AT+BVRA=1` (double tap), so the
  badge chirped and nothing happened. The older note that single taps "emit
  nothing at all" during SCO was true only of evdev.
- **The channel** now closes on either `AT+CHUP` or `AT+BVRA=1`
  (`_btmon_taps`, shared). evdev and ffmpeg-EOF stay as secondary paths.
- **A command recording** now watches btmon too. The monitor starts after
  the listening chirp, so the starting tap cannot end its own cycle, and it is
  handed to `run_channel` if the tap answers a hail. It is skipped in Game
  Mode. On a tap, `_finish_tapped_recording` half-closes and waits
  `TAP_FINALIZE_WAIT_S` (1.5 s) for the verdict. (The capture now runs on,
  drained, until the cycle's last sound has played; see the end of Phase 12.) A verdict plays as
  usual, so a dictation ended by tap is kept and confirmed. `b'f'` or no
  verdict plays **"Cancelled."** (`cancelled.wav`, new, spoken with `tts.sh`
  like the other SDK clips; TOS plays a tone here).
- **Not a recall.** The server finalizes a half-closed stream on the audio it
  has (`computer.py` treats EOF like end of speech), so a command it has
  recognised still runs. Deliberate: once spoken, execution is considered
  inevitable, and a tap exists to abandon a recording.
- **No protocol change, no server change.** The server already finalized on
  EOF and sent `b'f'` on no match.

`cancelled.wav`: 1139 ms, 22050 Hz mono, 95 ms of lead-in. It plays through
`play_wav()` with the default `PRIME_MS` 200, like the ack and nack clips.

Behaviour-tested off-badge, 8/8: `_finish_tapped_recording` against a real
socketpair (b'f' returned in 0.05 s; keepalive then b'l' in 0.10 s; no verdict
gives up at 1.51 s), and `_btmon_taps` against a real pipe (the `AT+CHUP`
hexdump line with ANSI codes, `AT+BVRA=1` split across two reads, unrelated
HCI traffic, EOF). ✅ **Verified on the badge 2026-09-13** (PAN, `:60`): a
tap ended a silent recording with "Cancelled.", and a tap straight after
"computer time" still delivered the time. Both sounds were inaudible until
the fix at the end of Phase 12.

**Single-tap channel close, verified in TOS on the badge (2026-09-12),**
across every relay pairing: hailed mobile → Windows, closed from mobile;
hailed Windows → mobile, closed from Windows; hailed Windows → Linux, closed
from Linux. The Linux close is the `AT+CHUP` path this phase ports.

### Phase 11 — The SCO hold: a follow-up tap skips the link rebuild ✓ code (2026-09-13)

Ported from TOS relay-linux the night it was verified on PAN (TOS
`relay-linux/RELAY.md` → *the SCO hold*). Tap → listening chirp is ~1.2 s on
Linux and most of it is rebuilding the audio link the previous cycle tore
down. Simply not tearing down was tested in TOS on 2026-09-05 and failed: the
badge dropped the link on its own timetable while the relay assumed it was
up. This holds the link the way the 2026-09-13 btmon test did — with a
running capture — and watches that capture.

- **The capture is the link.** At the end of a cycle, `_sco_hold.begin()`
  takes the still-running ffmpeg *before* the confirmation plays (its pipe
  must be drained or ffmpeg blocks and drops the link) and lingers
  `SCO_HOLD_MS` (5000). **The cycle ends silently on a held link** — no cue
  at all, by the Captain's ruling after two attempts at one failed on the
  badge; see the end of Phase 12. If the linger expires instead, the badge's
  own teardown chirp closes the sequence. On expiry,
  `drop()` reaps and tears down as before. If the capture ends on its own,
  the hold is gone, the profile is switched off to resync, and the next tap
  arrives on evdev and takes the full path.
- **Taps arrive differently while held.** With the link up a single tap is
  `AT+CHUP` on btmon and **no evdev event at all** (measured in TOS,
  `log/btmon_hold_test_20260913_002925.txt`), so the cycle's btmon comes
  along into the hold and `main()` selects on `_sco_hold.watch_fd()` beside
  the evdev fd. A tap there claims the capture and btmon and runs
  `stream_and_handle_response(reuse)`, which skips steps 1–3.
- **No btmon, no hold.** On an install without HCI privileges a held link
  would be deaf for the whole linger, so the hold needs a live btmon at the
  end of the cycle. Also no hold in Game Mode (its cue *is* the teardown
  chirp), after a server that never answered, or after a tap-ended cycle
  (that path reaps the capture before its verdict wait).
- **Chokepoints:** `start_sco_capture()` and `force_sco_teardown()` both
  drop any hold first, so `play_wav_cold`, the prewarm and the downlink
  paths behave exactly as before.
- **Differs from TOS in two ways.** TOS reads `sco_hold_ms` live from
  TOS.conf; here it is a constant. And TOS never holds after a spoken answer
  (its `b'v'` path reaps the capture before playing); the SDK has always
  played `b'v'` with the capture open, so it holds after those too.

TOS measured on PAN: tap → server accepting the stream ~240–340 ms on a
held link against ~490 ms cold; the audible gap is larger since the output
side is warm too. Captain: *"noticeably faster, more like the Windows relay
already does."* Off-badge harness 10/10 (lifecycle, claim hands back
capture + btmon, expiry, capture death, drop without teardown, btmon death).
✅ **Verified on the badge 2026-09-13** (Captain: test passed).

### Phase 12 — the tap windows: no tap vanishes silently ✓ ON-BADGE (2026-09-13)

Ported from TOS hours after Phase 11, once TOS had **measured** what a badge
does with a tap the relay is not ready for (TOS `controlpanel/TUNING.md`
item 7, thirteen cued trials on PAN). Its 4.9-14.3 s of silent deafness was
already gone — a tap during a recording ends it, a held link takes the next
one — but three narrow windows remained, and the same three exist here.

- **A tap between the link coming up and btmon starting was seen by nobody.**
  Phase 10 started btmon *after* the listening chirp, reasoning that the tap
  which began the cycle could not then end it. But the link is live from the
  moment the capture opens, and from that moment a tap is `AT+CHUP` and not a
  key event — so it fell in the gap. TOS measured taps landing there at +749
  and +744 ms. **btmon now starts as soon as the capture is live** (step 3b,
  before the chirp), and `TAP_CANCEL_GUARD_S` (0.3 s) keeps the starting tap
  from ending its own cycle.
- **The teardown guard was the tap debounce, and too long for the job.**
  `drop()` and `_died()` bumped `_tap_clock["last"]`, so `TAP_DEBOUNCE`'s
  2.0 s applied after an expiry teardown. On TOS that ate a deliberate tap 1
  time in 3, while seven teardowns produced no re-fire at all. Now
  `_tap_clock["teardown"]` with **`TEARDOWN_TAP_GUARD_S` = 0.5 s**, and both
  refusals are decided in one place, `tap_blocked_reason()`.
- **Every refusal now prints**, plus a `READY for the next tap` line at the
  instant another tap can be acted on. A tap that produces nothing is
  indistinguishable from a dead badge, which is the whole complaint these
  guards create; the listener can at least say which guard ate it.

⚠ **The third TOS window: queued during a confirmation, and a tap during a
spoken answer now cuts it — both verified on the badge.** In TOS a tap during
the confirmation is queued and run once the cycle frees its lock. Here the
tap waits in btmon's pty buffer and is read when `main()` loops, and whether
it fires depends on `HOLD_TAP_GUARD_S`, which counts from the hold's start:

- **During a confirmation clip, it fires.** The hold begins before the clip,
  and the SDK's clips are spoken (1.4–1.6 s plus the prime), so the guard has
  expired by the time the tap is read. A tap during "Command failed." was
  accepted `reused after 1917ms` and recorded normally (2026-09-13). Two
  differences from TOS remain, both accepted: the cue's 1.2 s wait counts
  from when the queued cycle starts rather than from the tap, so "Listening."
  comes later than it needs to, and nothing logs the tap as queued.
- **During a spoken answer, it was refused, and is now a cut.** That tap was
  read the instant the hold began, inside the guard: `tap IGNORED — within
  0.5s of the hold starting`, four times. The Captain first ruled that
  acceptable, then ruled that such a tap should cancel. It now cuts the
  answer off; see the end of Phase 12.

Off-badge harness 7/7 (both clocks, the hold writing the teardown clock at
expiry and at capture death, claim still handing back capture + btmon).
✅ **On the badge 2026-09-13** (PAN, `:60`): refusals print; a tap 1 s after
the hold's teardown is accepted where the old 2.0 s guard refused it (its
cue clipped to "ning", TOS's accepted 7d case); and taps that end a
recording work. The narrow window A was not hit deliberately: no tap landed
between link-up and the end of the chirp.

### ⚠ Found on the badge, and fixed: the ready cue said "Listening."

The first SDK badge test of Phase 11 failed, and on the one thing this
project has been caught by before. The Captain: *"tap > 'computer time' > I
hear the time > I then hear 'listening' (unexpected) > skeptical, I say:
'computer time' — no response, as I thought, it wasn't really listening."*

Phase 11 copied TOS's ready cue, which is `ready_chirp = listening.wav`. In
TOS that file is a **309 ms tone**, quieter than the badge's hardware chirp
and distinguishable from it by ear (Captain). Here the file of the same name
is the **spoken word "Listening."** — measured 2026-09-13: 1050 zero
crossings per second with 71% of its energy at 150 Hz, against TOS's
10275/s and 40% at 4 kHz. So the cue announced a recording that was not
running, the Captain reasonably spoke, and nothing was listening.

This is exactly the fault **Phase 9** fixed for the answering tap and the
closing channel — *the SDK's cues are spoken, so they must say the true
thing* — reintroduced by porting a TOS filename rather than a TOS meaning.

The first fix was a new `ready.wav`, spoken **"Ready."**. It was accurate,
and the Captain ruled it out on the next test anyway:

> *"I think the answer is, we don't need to play ready.wav at all — if I tap
> within 5s and it reuses the link, great — it should definitely play
> listening.wav on that tap if able... if I don't tap within the 5s and I
> hear the teardown chirp, that's fine and seems to work ok from there."*

**So a held cycle now ends silently.** The answer it just played is the
signal that it is over; what the next tap needs to hear is "Listening.",
which that cycle plays for itself; and an expiring linger still ends in the
badge's own chirp. `ready.wav` is deleted rather than left unused.

⚠ **Two rules this leaves behind.** First, *a TOS asset name is not an SDK
asset meaning* — check what a file actually says before reusing either side's
cue. Second, *an accurate cue is not automatically a wanted one*: the test
that matters is whether the sound tells the user something they need at a
moment they need it.

⚠ **OPEN — answered below, and the answer is that the badge's own chirp lands
on top of it: "Listening." is not heard on a REUSED tap, and the software is
not why.** The Captain, twice: on a tap inside the linger he hears the badge's
own tap chirp and **no "Listening." at all**, then speaks and finds it had
been recording. Step 4 is reached on a reused cycle and now times itself:

    listening chirp: 1015ms (cold link)      <- heard
    listening chirp: 1033ms (reused link)    <- not heard
    listening chirp: 1018ms (cold link)      <- heard
    listening chirp: 1007ms (reused link)    <- not heard

`pw-play` runs for the same ~1.0 s on both paths (160 ms prime + a 671 ms
clip + overhead), so the clip is being played identically and the question is
whether the badge EMITS it. That is the "ok=True while silent" shape again,
and the relay cannot see it: only a second microphone can.

Two candidates, both testable: the held link's output side may have gone idle
in a way that swallows the first playback, or the prime itself may be the
problem — TOS.conf records that priming a HOT link actively hurts, because
the gap between the two plays lets PipeWire release the path and clip what
follows. TOS does not show this, for two reasons worth keeping in mind: its
reuse cue is a 309 ms **tone**, which survives what speech does not, and TOS
never holds after a spoken answer, which is exactly where this appears.

**The Yeti run was inconclusive, and the analysis was the reason.** Recorded
2026-09-13 04:24 with the badge beside the microphone; the SDK log shows the
six chirps, the recording is good (146 s, peak −4.6 dBFS), but scoring it
against `listening.wav` proved nothing: a 671 ms speech envelope matches
almost any speech, including the Captain's own voice, so every window scored
0.83-0.95 against a whole-file baseline whose p90 is already 0.44. One cold
window even landed on digital silence, which says the recording's t=0 was
inferred rather than measured. Saved as
`log/sdk_chirp_yeti_20260913_042354.wav` for a better-anchored re-score; the
method needs a measured anchor, a band-limited detector and a null control.

### ⭐ The Captain's redirection: stop fighting for the clip, use the badge's own chirp

> *"if we can figure out a trick to trigger the hardware chirp when it's
> listening instead of playing listening.wav, that would make the experience
> more consistent... right now, some taps (cold) use listening.wav to indicate
> readiness, while other taps (warm) may simply do the hardware chirp yet be
> equally ready — so I'd think that forcing listening.wav into a pipeline that
> is resisting it seems harder than using the hardware chirp as the reliable
> listening indicator."*

He is describing a real inconsistency, and the cheap test confirmed it again:
a tap inside the linger still produces the badge's chirp and no "Listening.",
and the badge is **equally ready either way**. The clip is the unreliable
half of the pair, and it is the half we are pushing uphill.

**What the badge is known to chirp on**, from tonight's btmon work: its own
button while SCO is up (the `AT+CHUP` it sends is refused by BlueZ and the
badge beeps anyway), and the SCO link dropping (the teardown chirp, which is
already TOS's end-of-cycle cue). Unknown, and the key question: whether it
also chirps on SCO coming UP — because if it does, a cold tap already has a
hardware marker at the exact moment recording becomes live, and
`listening.wav` is redundant everywhere rather than merely unreliable.

**Routes to triggering it deliberately, honestly assessed.** Cycling SCO
works and is what the teardown chirp already is, but it costs the link and
the ~900 ms rebuild, which defeats the purpose on a warm tap. The HFP way is
to have the AG send an unsolicited `RING`, which makes an HF with in-band
ringing off play its own alert — but the AG side here is PipeWire's native
HFP backend, so injecting one means patching that backend or going through
oFono, and both are well outside a reference implementation. An indicator
change (`+CIEV`) is the same problem. So the realistic finding may be that we
cannot *trigger* the chirp, only *rely* on the ones the badge already makes.

**Next, and it needs proper anchoring this time:** btmon and the Yeti
together, as the 2026-09-13 02:29 hold test did — btmon timestamps every SCO
transition exactly, so the recording can be aligned to real events rather
than to a schedule, and the question "does the badge chirp on SCO up" is then
a direct read. That experiment also answers whether `listening.wav` can be
dropped from the SDK altogether.

### ⭐ The probe ran, and it refutes the redirection: the badge is SILENT on link-up

`controlpanel/chirp_probe.py`, run 2026-09-13 (`chirp_probe_20260913_093105`),
anchored the way the two failed analyses were not — PAN↔CUBE clock offset
measured NTP-style over SSH, and a 1 kHz marker tone *found in the recording
by frequency* rather than assumed from a schedule. Bursts were scored by
hi/lo band ratio, which separates the badge's chirp from room noise and from
speech. The result, three ways:

| event | bursts | level | hi/lo ratio |
|---|---|---|---|
| SCO link **down** | **5/5** | −5 to −8 dBFS | 15–443 |
| SCO link **up** | **0/5** | −39.9 to −45.9 dBFS | 0.04–0.96 |
| tap on a live link | **3/3** | −4.6 to −10.3 dBFS | 175–644 |

The link-up row is the finding. At all five link-up instants the loudest
thing near the event is room noise — the same level and the same flat
spectrum as the silence around it. **The badge does not chirp when SCO comes
up**, so the redirection's premise does not hold: there is no hardware marker
available at the moment a COLD tap's recording goes live, and the hardware
chirp therefore cannot become the cue everywhere. It can only ever cover the
warm half.

⚠ **A parser bug of mine nearly buried this, and my first explanation of it
was wrong.** btmon logged 5 `Setup Synchronous Connection` commands but the
probe reported only **one** link-up. I blamed interleaved Max Slots Change
events stealing a pending `Handle:` line, and changed the parser to mark on
the event header instead. The re-run on 2026-09-13 10:13 still scored 1 of 4,
which is how the real cause surfaced: **btmon pads headers to a fixed width
and truncates the event NAME by whatever the trailing packet number needs** —
`Synchronou..` (#10), `Synchrono..` (#421), `Synchron..` (#1318) all in one
run. A name prefix matches only the narrow-numbered packets; `Disconne..` is
short enough to survive every truncation, which is why link-downs scored 5/5.
Now matched by **opcode** (`0x2c` up, `0x05` down), which sits inside the
padded region and cannot truncate.

**The acoustic conclusion never depended on either bug** — link-up times come
from the marker anchor and the probe's own schedule, and every one of them was
checked directly in the recording. Worth keeping as a method note: *a parser
that under-reports events makes a silent event and a missed event look
identical*, and only the independent anchor told them apart.

**The third row is the fix.** A tap on a live link produces *two* bursts: a
tiny one at the `AT+CHUP` instant, then the real chirp **+0.51, +0.51, +0.52 s
later**, running ~0.4 s. Our warm-tap clip occupied roughly +0.05 to +0.9 s —
straight through it. A headset playing its own local tone owns the speaker
while it does, which is why `pw-play` reported a clean ~1.0 s run on every
warm tap and the Captain heard nothing, twice.

### ⭐ The Captain's ruling: consistency wins, so wait the chirp out

> *"Since cold taps always play listening.wav, we should continue to do so on
> warm taps — consistency is the most important thing. The fact that cold taps
> play listening.wav and cannot use the hardware chirp is itself what makes the
> hardware chirp undesirable as an indicator, since it's not ALWAYS the
> indicator, however reliable it may be in that scenario... perhaps wait it out,
> then play listening.wav immediately after the duration of that?"*

The redirection is therefore withdrawn by the same reasoning that proposed
it — it was offered to *remove* an inconsistency, and the measurement shows it
would instead make one permanent (cold taps can never use the hardware chirp).

`WARM_CHIRP_WAIT_S` in `listener.py`: a **reused** cycle sleeps past the
badge's chirp before priming and playing, so the clip lands in clear air. A
cold cycle does not wait and must not — nothing chirps on link-up to collide
with, and the cold path is already the slow one.

### ✅ The wait is SWEPT, not guessed — and 1.0 was one notch above failing

`controlpanel/warm_wait_sweep.py` holds the link up for a whole run (so every
tap is warm), waits W after each tap, plays the SDK's own `listening.wav`
through the SDK's own `pw-play` invocation, and scores what comes out against
an uncontested **reference play** — because absolute milliseconds always
undercount, the clip's quiet head and tail sitting below the room floor.
Ten taps, 2026-09-13 10:51, **Captain scoring by ear in parallel**:

| W | by ear | by microphone |
|---|---|---|
| 0.0 | not heard | SILENT (control reproduces) |
| 0.8 | **clipped to "ning"** | 0/2 |
| 1.0 | heard in full | 2/2 |
| 1.2 | heard in full | 2/2 |
| 1.4 | heard in full | 2/2 |

**Set to 1.2, not the verified-good 1.0.** The boundary is close and a missed
cue is the entire defect. Measured chirp end ranged **0.62–0.98 s** over 7
detections; 1.0 leaves ~0.5 s against that spread, 1.2 leaves ~0.7 s, and
200 ms is cheap against the thing the Captain ruled paramount.

⭐ **The chirp's acoustic end does not explain the cutoff, which is the real
finding.** At W=0.8 the clip begins ~0.30 s *after* the last chirp energy and
still dies; at W=1.0 it begins ~0.50 s after and lives. So the badge holds its
speaker roughly **0.3 s past the tone going quiet**, and what has to clear the
chirp is the 160 ms **prime**, not the clip. Anyone tempted to tighten this
constant by reading chirp-end alone will set it too low.

**What it costs:** the warm cue moves from ~300 ms to ~1.5 s. The recording is
live for the whole wait, so a word spoken into it is still captured — what is
delayed is the confirmation, not the listening.

⚠ **Two analyser artifacts, recorded because they nearly inverted the result.**
The first scoring pass called W=0.0 a **FULL** clip at −5.4 dBFS, when real
clips measure −27 to −29: at W=0 the chirp lands inside the clip's window, and
a chirp blended with clip audio drags hi/lo back under `CHIRP_RATIO`. The ear
said otherwise and the ear was right. Now excluded two ways — runs starting
before the detected chirp ends, and runs louder than the reference +12 dB (the
backstop for the 3 trials of 10 where the chirp itself was not detected).
**The microphone is a proxy for the Captain's ear, and when they disagree the
ear wins.**

### ✅ Found on the badge, and fixed: a tap-ended cycle played into the tap chirp

The Phase 12 badge test turned it up at once. A tap that ends a recording
always lands on a **live** link, so the badge chirps ~0.5 s later, exactly
as it does on a reused tap. The cycle then played its last sound ~0.3 s
after the tap, straight into that chirp:

- **Nothing said, then a tap:** the Captain heard two hardware chirps and no
  "Cancelled." at all.
- **"computer time", then a tap the instant it was said:** the badge chirp
  stepped on the answer, which was *"only slightly partially"* heard.

**Fix, and it reuses the swept numbers rather than inventing new ones.**
`_play_after_tap_chirp()` plays every sound in a tap-ended cycle ("Cancelled.",
a spoken answer, the ack) at **tap + `WARM_CHIRP_WAIT_S`, then the
160 ms listening prime, then the sound**. That is the arrangement the warm-wait
sweep verified. It is dated from the tap, so a verdict that arrives late
waits less.

⚠ **The capture is no longer killed at the tap.** `_drain_capture()` reads
and discards it until that last sound has played, and it is reaped straight
after, even if playback fails. On Linux the capture *is* the link; a 1.2 s
idle gap on a hot link is the documented way to clip what follows. A
tap-ended cycle still never holds, so a cancel drops the link at once, as
before.

**Verified on the badge 2026-09-13** (PAN, `:60`): `after the tap chirp:
cancelled.wav at +1200ms`, *"hardware chirp + 'cancelled' + another hardware
chirp"*; `answer at +1200ms`, *"time response + another chirp, clean"*. The
warm reused tap was re-checked and is unchanged. Off-badge harness 12/12
(timing, late verdict, `b'f'` / no verdict / `b'v'`, temp-file cleanup, an
8 MB capture pipe drained without blocking).

**TOS relay-linux had the same fault, and got the same fix the same night**
(`wait_out_tap_chirp`, badge-verified; TOS `relay-linux/RELAY.md`). One
difference: TOS reaps the capture *before* a spoken answer rather than
draining it underneath, because TOS recorded a corrupted-packet flood with a
capture open under a voice answer (2026-08-23).

### ✅ A tap during the spoken answer cuts it off

The Captain's ruling, 2026-09-13: a tap during the answer means *"i no longer
care about this response"*. It does not recall the command, which has
already run; it cancels the request. *"If it cuts it off and plays
cancelled.wav that would seem to be best."* Ported from TOS relay-linux the
same night, where it was badge-verified first.

- **`_play_cuttable()`** plays the answer through `Popen`. The listener is
  single-threaded, so the playback loop itself selects on the cycle's btmon
  every 50 ms. A tap stops `pw-play` and returns the tap's time; a tap already
  waiting cuts before playback starts. With no btmon the answer just plays.
- **All three answer paths use it:** the normal `b'v'`, the BrokenPipe
  recovery, and an answer after a tap-ended recording. On that last path,
  `_discard_btmon()` first throws away btmon's backlog, which holds only the
  tap that ended the recording, so it cannot cut its own answer. A second tap
  made during the 1.2 s wait is lost with it, as on TOS.
- **A cut ends as a cancel:** the capture is drained, "Cancelled." plays via
  `_play_after_tap_chirp` dated from the cutting tap, then the link is torn
  down. The cycle does not hold.

**Verified on the badge 2026-09-13** (PAN, `:60`):

| test | log | heard |
|---|---|---|
| tap mid-answer | `answer cut off after 0.88s` → `cancelled.wav at +1200ms` | badge chirp, "Cancelled.", link-drop chirp |
| tap as the answer begins | `answer cut off after 0.92s` | answer cut, badge chirp, "Cancelled.", link-drop chirp |
| tap just after "computer time" | `ending the recording` → `answer at +1200ms` | badge chirp, the whole time, link-drop chirp |
| no extra tap | `lingering` → `linger expired` | the time, ~5 s, link-drop chirp |
| cancel a silent recording | `cancelled.wav at +1200ms` | badge chirp, "Cancelled.", link-drop chirp |
| warm tap | `reused after 3971ms` | badge chirp, "Listening.", the time |
| tap during "Command failed." | `reused after 1917ms` | badge chirp, "Listening.", the time |

⚠ **Not reached on the badge:** a second tap during an answer that follows a
tap-ended recording. The run meant for it produced an ordinary mid-answer cut
(`cut off after 1.09s`, with no `ending the recording` before it). That path
is covered only by the off-badge harness.

Off-badge harness 8/8. It covers an answer played in full with no tap; a tap
at 0.5 s cutting it and ending the player; a waiting tap cutting before
playback; no btmon meaning no cut; a stale report not cutting its own answer
while a fresh tap does; and `_finish_tapped_recording` returning
`(b'v', cut_at)` and `(b'f', 0.0)`.

## Resume Point (2026-07-17)

**ALL PHASES (1–6) COMPLETE AND ON-BADGE VALIDATED.** The SDK
implementation was validated PAN↔BOX (2026-07-05); the production port
(maincomputer/intercom.py + relay-linux/relay.py + relay-mobile) was validated
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
  `prime_ms_push`; `prime_ms_listening` settled at 250 on PAN (the SDK's
  own `PRIME_MS_LISTENING` settled at 160 — different chirp asset).
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
  (`stdbuf -oL btmon`, match `AT+BVRA=1`; relay-linux/relay.py
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
