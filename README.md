# License

See <LICENSE.md>.  This SDK is essentially a component of TOS and subject to its licensing terms.

# Combadge SDK

Turn a Bluetooth combadge into a working communicator and control system with a "main computer" behind it.

(While this SDK was primarily designed with the Fametek TNG Combadge in mind, it's built on Linux & Windows and is wired into the Bluetooth stack, and therefore can work with any bluetooth headset with a standard HFP button - essentially compatible with any two-way communication device (mic+speakers) with a standard Play/Pause button, such as the Sony WH1000XM3, as one of countless examples of devices with this capability.)

The FameTek Star Trek: TNG combadge pairs as an ordinary Bluetooth Hands-Free headset — out of the box it can answer your phone (bi-directional comms), play audio (music, notification sounds), and it has a single- and double-tap button for interaction.  What I see in this device is the 24th century modality of human-computer interaction brought to life.  Thank you, Fametek:  As far as building the rest of the ship:  I'll take it from here.  :)

This SDK gives the badge what it always implied: a computer on the other end. Three small Python scripts turn a badge tap into a complete voice command pipeline — tap the badge, hear a chirp, speak a phrase, and a synthesized voice answers back through the badge speaker. What a phrase *does* is yours to define in a plain Python dict: report the time, run a shell command, hit an API, switch the lights — anything the server machine can reach.

With two badges it becomes an **intercom**: tap and say *"captain to engineering"* and your actual voice plays from the other badge; a tap there answers, opening a live two-way channel between the badges until either wearer closes it. (See <INTERCOM.md> for the full design.)  Define your own user names, locations, hail and communicate with them *canonically* - from anywhere in the world that TCP/IP can reach, including space.

There is no AI during use.  No cloud dependency - EVERYTHING can run locally. Speech recognition is Vosk (offline), responses are local text-to-speech, and the transport is your own LAN — no cloud, no accounts, no LLM. The badge side needs a Linux machine with Bluetooth (a spare laptop or a Pi is plenty); the server side runs anywhere Python runs — Linux, macOS, or Windows, on the same machine or another on the network, or self-hosted on the internet if your crew is dispersed.

From this, I built TOS - an AI harness & guidance system, a home & system automation controller, even a theatrical co-performer with a famous starship computer voice - see it in action at https://tos.md and use this SDK to build your own.

While AI was obviously used for generating code - EVERY prompt was human-written or verified, EVERY file edit and every code change was human-supervised and audited.  Never auto-mode (see <LICENSE.md> for philosophy on that.)  It took nearly 6 months to produce the mere ~44K tokens that make up this SDK, because I started with the bigger TOS system, which itself is a mere ~300K tokens due to careful architecture and slow building.  This is no slop, it is a human-driven labor of love - there isn't a single line of code that I don't personally understand and had thought about carefully when it was being written - though I admit I am trusting a *bit* more to Claude now without line-by-line auditing, and working faster now than when I started.

### Table of Contents

0. [Quick Start](#quick-start)
1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Pairing and Connecting the Badge](#pairing-and-connecting)
4. [Minimal `transceiver.py` — Connection Manager](#transceiver-py)
5. [Minimal `listener.py` — Tap and Audio Handler](#listener-py)
6. [Minimal `computer.py` — Receive, Recognize, Respond](#computer-py)
7. [Badge Playback](#badge-playback)
8. [The Voice Command Pipeline at a Glance](#complete-loop)
9. [Known Gotchas](#known-gotchas)
10. [Vibe Control — driving a terminal by voice](#vibe-control)
11. [Large-Vocabulary Dictation — captain's log and computer transcribe](#large-vocab)

## <a name="ai-assisted-quick-start"></a>-1.  AI Assisted Quick Start

Point your AI at this repo and `make it so`.  Note that this approach is technically a 'permissible license violation' with remediation steps described within <LICENSE.md> (in short: If you scan this with AI, you have to admit you did so, to avoid being in violation of the license, since this was made for humans. :)

## <a name="quick-start"></a>0. Quick Start

Two terminal windows are all you need. The **relay host** is the Linux machine the badge is paired to. The **server** (running `computer.py`) can be the same machine or any other machine on the same network — Linux, macOS, or Windows.

**Step 1 — Start the server** (any OS with Python):
```bash
# Install dependencies (Linux/macOS)
pip install vosk
sudo apt install espeak-ng       # Linux — skip on Windows (SAPI built-in)

# Download Vosk model (~40 MB): https://alphacephei.com/vosk/models
# Unpack vosk-model-small-en-us-0.15/ somewhere, then:
python3 computer.py /path/to/vosk-model-small-en-us-0.15
```

The small model is all you need for commands. A **second, optional** argument
enables free-form dictation — see [§11](#large-vocab):

```bash
python3 computer.py /path/to/vosk-model-small-en-us-0.15 \
                    /path/to/vosk-model-en-us-0.22
```

**Step 2 — Start the relay** (Linux relay host, must be root):
```bash
pip install evdev

# If computer.py is on the same machine:
sudo SDK_USER=$USER python3 transceiver.py

# If computer.py is on a different machine:
sudo SDK_USER=$USER SDK_SERVER_HOST=192.168.x.x python3 transceiver.py
```

**Launcher scripts (optional, recommended):** the two commands above are wrapped
in one-line launchers so the full invocation isn't retyped each session:

- `computer.sh` (server host) — runs `computer.py` with the small model.
  As shipped it expects the unpacked Vosk model in a `.vosk/` directory one level
  above this folder — edit the path to wherever yours lives. If the **large**
  model is sitting beside it, the launcher passes that too and dictation
  ([§11](#large-vocab)) switches itself on; if it isn't, nothing changes.
- `transceiver.sh` (relay host) — `sudo SDK_SERVER_HOST=<server-ip-or-hostname> python transceiver.py`.
  The env var **must** be inline on the sudo command line: `sudo` resets the
  environment by default, so a prior `export SDK_SERVER_HOST=...` is silently
  stripped and listener.py falls back to `localhost` (symptom: endless
  "waiting for server localhost:1701" while the server runs fine elsewhere).
  Add `SDK_SERVER_PORT=<port>` inline the same way if not using 1701.
  Hostname targets (e.g. `myserver`) work if the relay host resolves them.

**Step 3 — Test it:**  
Tap the badge once. You hear a chirp from the badge speaker. Speak one of these phrases:

| Say this…             | Badge plays back…      |
|-----------------------|------------------------|
| "computer hello"      | "Hello."               |
| "computer status"     | "All systems nominal." |
| "computer time"       | current time readout   |
| "computer goodbye"    | "Acknowledged."        |

Edit the `COMMANDS` dict in `computer.py` to add your own phrases and responses.

> **If you outgrow the dict** — say you move commands to an external file (CSV,
> JSON, whatever) so you can edit them without restarting the server — resist
> the obvious shortcut of re-reading the file on every tap. It feels free with
> 20 commands, but the cost grows with the file and is invisible until it
> isn't: at a few thousand phrases, the per-tap reparse costs ~90 ms of pure
> waste (measured on the full TOS at 5,488 phrases). The right pattern costs
> ~10 lines: on each tap, `os.stat` the file and reparse **only when its
> mtime/size changed**, otherwise serve the previously parsed result from
> memory. You keep live editing (a save changes the mtime, so the next tap
> picks it up) and taps stay stat-cheap forever. No polling, no watcher
> threads — just a lazy check inside the tap handler, exactly where the
> file read used to be. (The in-memory `COMMANDS` dict itself scales fine —
> matching is a substring scan — this note is only about file re-reading.)

**Before your first run — checklist:**
- [ ] Badge paired to the relay Linux host (`bluetoothctl pair <MAC>` + `trust <MAC>`)
- [ ] WirePlumber seat-monitoring fix applied if running headless/SSH (see §3)
- [ ] `sudo apt install ffmpeg pipewire pipewire-pulse wireplumber pulseaudio-utils` on relay
- [ ] `pip install evdev` on relay
- [ ] `pip install vosk` on server host
- [ ] Vosk model downloaded and unpacked
- [ ] `sudo apt install espeak-ng` on server host (Linux) — not needed on Windows

---

The three scripts in this folder (`transceiver.py`, `listener.py`, `computer.py`) are deliberately minimal and heavily commented. They carry only the voice command pipeline (badge tap > command execution > voice response to badge) and the intercom — no user identity, no command-file dispatch, no dictation modes — so every piece that remains is essential and understandable. Read a section here for context, then read the corresponding file for the code. They are a foundation to build on, not a framework to configure.

(`vibewin.py` and `vibekeys.py` are a fourth, **optional** piece — Vibe Control, §10. They are Windows-only, `computer.py` imports them defensively, and nothing else depends on them.)

---

## <a name="overview"></a>1. Overview

The TNG combadge presents itself to a Linux Bluetooth host as **two devices in one**:

1. A **Bluetooth Hands-Free (HFP)** audio device — bidirectional 16 kHz mono audio over an SCO link. Visible in PipeWire as a `bluez_card.<MAC>` with profile `headset-head-unit`, exposing a `bluez_input.<MAC>` source and `bluez_output.<MAC>.1` sink.
2. An **HID input device** — the physical badge tap surfaces as keypress events on `/dev/input/eventX`. A single tap fires `KEY_PAUSECD` (key code 201; some firmware revisions use 200). A double tap is *not* a separate keycode — it's an `AT+BVRA=1` command that the badge issues over the HFP control channel; you watch for it with `btmon`.

The SDK builds the smallest end-to-end voice command pipeline that exercises both:

```
badge tap (evdev) → chirp (pw-play) → mic capture (ffmpeg)
                  → TCP stream → Vosk recognize on server
                  → server picks an action → response WAV back over TCP
                  → playback through badge speaker (pw-play)
```

Two processes on the relay host, one on the server:

| Process          | Host          | User    | Job                                                                  |
|------------------|---------------|---------|----------------------------------------------------------------------|
| `transceiver.py` | relay (Linux) | root    | Discover paired badge, hold the BT connection, launch `listener.py`. |
| `listener.py`    | relay (Linux) | invoker | Wait for taps, capture mic, stream to server, play response.         |
| `computer.py`    | any host      | any     | Accept TCP, run Vosk, decide action, send response.                  |

`transceiver.py` is split off from `listener.py` for one reason: connection management requires root (for `runuser` and `sg input`); audio capture and `pw-play` require the user's session bus. Splitting them lets each run with the correct privileges.

## <a name="prerequisites"></a>2. Prerequisites

**Hardware:**
- Combadge (or compatible HFP/HID Bluetooth badge).
- Linux host with a Bluetooth adapter that supports HFP Audio Gateway. Verified working: built-in Intel adapters, generic CSR dongles, TP-Link UB500 (Realtek RTL8761B). *(Historical note: early testing attributed an SCO packet fragmentation bug to the RTL8761B chipset; that attribution proved stale — the same adapter later passed full round-trips, and a second host ran the identical chipset without issue throughout.)*

**OS:** Debian/Ubuntu-flavored Linux is the tested baseline. Anything with PipeWire ≥ 0.3.50 and BlueZ ≥ 5.60 should work. The Main Computer side runs anywhere Python and Vosk run (Linux, macOS, Windows).

**System packages:**
```bash
sudo apt install bluez bluez-tools pipewire pipewire-pulse wireplumber \
                 pulseaudio-utils ffmpeg python3 python3-pip
```

**Python packages (relay host):**
```bash
pip install evdev
```

**Python packages (server host):**
```bash
pip install vosk
```

**Vosk model:** download `vosk-model-small-en-us-0.15` (~40 MB) from https://alphacephei.com/vosk/models and unpack it anywhere and pass the path on the command line to `computer.py`.

**TTS for responses:** `computer.py` handles TTS automatically — no configuration needed. On Linux/macOS it uses `espeak-ng`; on Windows it uses the built-in PowerShell `System.Speech` synthesizer (no install required). Install `espeak-ng` on the server if you're running it on Linux:
```bash
sudo apt install espeak-ng
```
You can verify it works independently:
```bash
espeak-ng -v en-us -w /tmp/test.wav "Acknowledged."
```

## <a name="pairing-and-connecting"></a>3. Pairing and Connecting the Badge

Pair once interactively with `bluetoothctl`. Power the badge on (long-press until it chirps) and run:

```bash
bluetoothctl
[bluetooth]# power on
[bluetooth]# agent on
[bluetooth]# default-agent
[bluetooth]# scan on
# wait for "TNG COMBADGE" to appear, note its MAC, then:
[bluetooth]# scan off
[bluetooth]# pair  <MAC>
[bluetooth]# trust <MAC>
[bluetooth]# connect <MAC>
[bluetooth]# exit
```

`AlreadyExists` on `pair` is fine — it means the badge was paired previously; just `trust` and `connect`.

**Verify HFP is registered:**
```bash
bluetoothctl show | grep -i handsfree
# expect: UUID: Handsfree Audio Gateway   (0000111f-0000-1000-8000-00805f9b34fb)
```

If that line is missing, the host has BlueZ but no HFP profile registered — almost always WirePlumber's bluetooth monitor isn't running (see Gotchas: WirePlumber Seat Monitoring). Apply the seat-monitoring fix below before continuing, or HFP will never come up on a headless/SSH-only system.

**Verify the audio card and sink:**
```bash
pactl list cards short | grep bluez
# bluez_card.AA_BB_CC_DD_EE_FF   module-bluez5-device.c   ...

pactl set-card-profile bluez_card.AA_BB_CC_DD_EE_FF headset-head-unit

pactl list sinks short | grep bluez
# bluez_output.AA_BB_CC_DD_EE_FF.1  ...
pactl list sources short | grep bluez
# bluez_input.AA:BB:CC:DD:EE:FF    ...
```

Note the asymmetry: **sinks** use underscores in the MAC and append `.1`. **Sources** use colons and have no suffix. This trips everyone — keep it visible.

### WirePlumber seat-monitoring fix (required on headless/SSH systems)

WirePlumber's bluetooth monitor gates on logind reporting `seat0` as "active". On a host with no graphical session, that never happens, so the HFP profile is never registered and `bluetoothctl connect` fails with `br-connection-profile-unavailable`. Disable the gate:

```bash
mkdir -p ~/.config/wireplumber/wireplumber.conf.d
cat > ~/.config/wireplumber/wireplumber.conf.d/51-bluez-no-seat.conf <<'EOF'
wireplumber.profiles = {
  main = {
    monitor.bluez.seat-monitoring = disabled
  }
}
EOF
sudo systemctl restart bluetooth
systemctl --user restart wireplumber
```

## <a name="transceiver-py"></a>4. Minimal `transceiver.py` — Connection Manager

`transceiver.py` runs as **root** and runs **two loops, not one** (see *Why two loops* below — it is the most important design point in the file):

**DETECT loop** (main thread, every `SDK_DETECT_INTERVAL`, default 2 s) — cheap, and never blocks:

1. Find **all** paired devices whose name contains `TNG COMBADGE` via `bluetoothctl paired-devices` (cached 60 s), optionally narrowed by `SDK_BADGE_MAC`.
2. Ask which of them is connected — one `bluetoothctl devices Connected` query for all of them (falls back to `info <MAC>` on BlueZ < 5.65).
3. On a **new link, however it was established**: poll `pactl list cards short` until `bluez_card.<MAC>` appears, set the card profile to `headset-head-unit` so the HFP source and sink are exposed, wait for the sink.
4. Spawn `listener.py` as the invoking user, in the `input` group, with the user's session environment. Restart it if it exits; stop it when the badge goes away.

**PAGE loop** (background thread, every `SDK_PAGE_GAP`, default 5 s, only while disconnected):

5. `bluetoothctl connect <MAC>` — reach out to a badge that has not reached out to us.

### Why two loops

The obvious design is one loop that checks, then connects, then sleeps. **Do not write that.** It is what this file used to be, and it is slow for a reason that is invisible until you measure it.

`bluetoothctl connect` against a badge that is switched off or out of range does **not** fail fast. It blocks for the controller's **page timeout** — BlueZ's default is `0x2000` slots × 0.625 ms = **5.12 s** — before reporting failure. Checking whether a badge is connected, by contrast, is a D-Bus property read costing milliseconds.

Put both in one loop and the cheap operation is held hostage by the expensive one. That matters more than it sounds, because **a badge often connects itself**: powering it on makes it page the host it was last paired with. Measured in the reference TOS deployment over 8,951 retry cycles, **24% of all links were badge-initiated** — and for every one of those, the connect attempt was pointless while the badge sat unnoticed for a mean of 8 s behind a loop busy paging a badge that was already on the line.

Split them and detection costs whatever you set `SDK_DETECT_INTERVAL` to, while paging carries on in the background at its own pace.

| | Before (one loop) | After (split) |
|---|---|---|
| Badge connects itself | mean 8 s, worst 16 s | **≤ ~2 s** |
| Badge must be paged | mean 8 s | mean ~7.5 s |
| Wasted card-poll per failed retry | **15 s** | 0 |

### Bugs worth learning from

All of these were live in this file. They are stated plainly because every one of them is easy to write again, and because four of the five fail *silently* — the system does the wrong thing without ever reporting an error:

1. **Never poll for the side effect of an operation you did not confirm succeeded.** The old `connect_badge()` issued `bluetoothctl connect`, *ignored the result*, then polled up to 15 s for an audio card that cannot appear when the connect just failed. Every failed retry cost ~20 s of dead time on top of the 5 s page timeout. `page_badge()` now returns a checked boolean.
2. **Post-connection setup must not live inside the connect path.** A badge that connects itself never calls your connect function, so any HFP setup hidden in there is skipped — leaving roughly a quarter of links on A2DP, where the badge microphone does not exist. `ensure_audio_ready()` is therefore called by the *detect* loop, for every link, however it was established.
3. **Identity is the MAC, not the name.** Every TNG COMBADGE reports the same device *name*, so `find_paired_badges()` used to return the first name match and stop — which is whichever badge `bluetoothctl` happened to print first, not the one that is switched on. Pair two, carry one, and the transceiver pages the badge in the drawer forever while the badge in your hand is never even looked at. It now collects every match and the caller tries each.
4. **A privileged helper must be given the target user's session, and omitting it fails as silence.** This one cost the most time, so it is worth the detail. `transceiver.py` runs under `sudo`, which **strips `XDG_RUNTIME_DIR`**; `runuser` without `-l` does not create a login session, so it does not restore it. A `pactl` launched that way looks for a PipeWire socket in a directory that does not belong to the target user, finds nothing, and reports **no cards and no error**. From the caller's side that is indistinguishable from a badge with no audio card.

   Observed on PAN, 2026-08-08: a badge that `bluetoothctl info` showed as `Connected: yes`, `Bonded: yes`, `Trusted: yes`, `UUID: Handsfree`, battery 50% — a perfectly healthy badge — was being written off as unusable, repeatedly, because `pactl` was querying the wrong session. Every `pactl` and `systemctl --user` call now goes through `run_as_user()`, which attaches `XDG_RUNTIME_DIR=/run/user/<uid>`. If `pactl` works for you in a terminal but returns nothing from a root helper, this is why. `ensure_audio_ready()` now also says so explicitly when it sees *no cards at all*, rather than blaming the badge.

5. **"Connected" and "usable" are not the same thing — and confusing them livelocks the split.** BlueZ can hold an ACL link open to a badge whose audio profile never came up (`br-connection-profile-unavailable`): it reports `Connected: yes` while no `bluez_card.<MAC>` ever appears. The detect loop then keeps selecting that badge and waiting out its 15 s card poll, while the page loop — correctly told "a badge is connected" — stands down and never pages any other badge.

   **Note the shape of this failure: the split makes the two loops depend on one shared boolean, so any state that is neither cleanly connected nor cleanly absent can wedge both.** That is the structural cost of splitting the loops, and it is worth paying, but it has to be handled. The fix is `quarantine()`: a badge that connects but yields no audio card is disconnected, stood down for 60 s, and the connected flag cleared so paging resumes on the others — after the audio stack has been restarted and the badge retried once, because a dead session (bug 4) looks exactly like a dead badge from here, and the badge is the more expensive thing to discard wrongly.

### Two badges, one at a time

Pair as many badges as you like. The transceiver discovers **all** of them and connects to **whichever one is switched on** — so you can swap badges freely without restarting anything:

```
[transceiver] badge online: 1B:B8:82:88:2F:60
   …switch that badge off, switch the other on…
[transceiver] 1B:B8:82:88:2F:60 disconnected, stopping listener.py
[transceiver] paging 2C:F2:DF:45:EC:28...
[transceiver] badge changed: 1B:B8:82:88:2F:60 -> 2C:F2:DF:45:EC:28
```

`listener.py` is restarted with the new `BADGE_MAC`, so audio, taps and the server handshake all follow the badge you are actually holding.

The **expected pattern is one badge on at a time.** Two badges connected to a single transceiver simultaneously is out of scope: there is one `listener.py`, so the first-connected badge wins and the second is ignored until the first goes away. (Two badges *do* work in TOS proper — but as two separate relay hosts, which is what the intercom needs; see `INTERCOM.md`.)

Swap speed: a sweep pages each badge in turn, and an absent badge costs ~5 s of page timeout. The sweep **rotates which badge it tries first** — without that, the badge you just switched off would always be paged first and always burn its full timeout before the badge you just switched on was even tried, making the most common operation the slowest one.

### Pinning one badge (`SDK_BADGE_MAC`)

Optional. Leave it unset to accept any paired TNG COMBADGE — that is the right default, and it is what makes swapping work. Set it only when you want to *exclude* a badge (a faulty one, or one you have moved to another host):

```bash
sudo SDK_BADGE_MAC=1B:B8:82:88:2F:60 python3 transceiver.py
```

Comma-separate for several (`SDK_BADGE_MAC=1B:B8:82:88:2F:60,2C:F2:DF:45:EC:28`). Matching is case-insensitive. Find your badge's MAC with `bluetoothctl paired-devices`. The transceiver prints its active filter on startup, and says so explicitly if the pinned badge is not paired — rather than silently waiting forever.

### Battery — which battery

A natural worry is that a faster loop drains the badge. It does not, and the reason is worth internalising before tuning anything:

- **Paging costs the host, not the badge.** `bluetoothctl connect` transmits page trains from the *host* radio. The badge sits in page scan at a duty cycle fixed by its own firmware and cannot tell how often you page it.
- **What costs the badge is SCO** — bringing the audio link up, playing through its speaker, tearing it down. That is its highest-power activity by a wide margin. To save badge battery, look at how often you play audio to it, not at how often you poll.
- **What costs the host is `SDK_DETECT_INTERVAL`.** Each pass forks a subprocess and does a D-Bus round trip. At 2 s that is negligible; at 0 it is a busy loop that keeps a core warm permanently. On a battery-powered relay host (Pi, laptop) keep it ≥ 0.5. For genuinely instant detection at zero idle cost the answer is not a tighter poll but a D-Bus signal subscription on `org.bluez.Device1`'s `Connected` property.

**Why root?** `runuser` (drop privileges into the user's session for `pactl`/`pw-play`) requires root. `sg input -c …` (so `listener.py` can open `/dev/input/eventX`) likewise requires root unless the caller is already in `input`. `bluetoothctl` itself does **not** require root — it talks to BlueZ over D-Bus.

**Why a full `env=` dict for the child?** `runuser`/`sg` strip the environment. Without `HOME`, `USER`, `LOGNAME`, `PATH`, `XDG_RUNTIME_DIR`, and `DBUS_SESSION_BUS_ADDRESS` (`unix:path=$XDG_RUNTIME_DIR/bus`), `pw-play` exits 1 silently and you'll spend an evening wondering why the chirp never plays from the launcher even though it works from a terminal. Build the env explicitly from `SUDO_USER` and pass it to `Popen`.

**Startup sounds are handled by `listener.py`, not `transceiver.py`.** Playing audio from `transceiver.py` (which runs as root) is unreliable: `runuser`/`pw-play` from the root process lacks the user's PipeWire session, and `pw-play` alone cannot reliably establish a cold SCO output link anyway (see §7). All badge startup tones — badge-online and main-computer-online — are played by `listener.py` on startup via `play_wav_cold()`, which uses ffmpeg to trigger SCO negotiation from the capture side before playing audio.

Run it as:
```bash
sudo SDK_SERVER_HOST=192.168.50.5 SDK_SERVER_PORT=1701 python3 transceiver.py
```

Tuning knobs (all optional; intervals shown at their defaults):
```bash
sudo SDK_DETECT_INTERVAL=2 SDK_PAGE_GAP=5 SDK_BADGE_MAC=1B:B8:82:88:2F:60 python3 transceiver.py
```
`SDK_DETECT_INTERVAL` is the reconnect latency you feel. `SDK_PAGE_GAP` is the wait *between* connect attempts — each attempt against an absent badge costs ~5 s of page timeout regardless, so the effective retry period is roughly `5 + SDK_PAGE_GAP`. `SDK_BADGE_MAC` pins a specific badge; see *Choosing a badge* above.

See `transceiver.py` in this folder for the runnable minimal version.

## <a name="listener-py"></a>5. Minimal `listener.py` — Tap and Audio Handler

`listener.py` runs as the **logged-in user** (in the `input` group, courtesy of `transceiver.py`'s `sg input`). It does five things:

**Find the badge input node.** Iterate `evdev.list_devices()` and pick the one whose `name` contains `TNG COMBADGE`, or whose `EV_KEY` capability includes `KEY_PAUSECD` (201). Single-tap surfaces as that key, sometimes as code 200 — accept both. The node path can change if you re-pair, so look it up at startup and re-look-up after disconnect.

**Detect a single tap.** A clean `select()` loop on `device.fd`, reading events; trigger when `event.type == EV_KEY and event.code in (200, 201) and event.value == 1`. Debounce ~2 s — after SCO teardown the badge can re-fire spuriously.

**Start capture first, THEN play the chirp.** `ffmpeg -f pulse -i bluez_input.<MAC> -ar 16000 -ac 1 -f wav -loglevel quiet pipe:1`. Opening `bluez_input.<MAC>` triggers HFP SCO negotiation from the capture side — the only reliable way to bring the link up. ffmpeg writes a standard 44-byte WAV header as soon as it opens the source; receipt of those 44 bytes is the signal that the SCO link is live. Only then does `pw-play --target bluez_output.<MAC>.1 --media-role=communication listening.wav` play the chirp, which reliably routes to the badge speaker because the SCO link is already established. Playing the chirp before ffmpeg starts causes it to fall through to default output (laptop speakers). The `--media-role=communication` hint nudges PipeWire toward the HFP sink rather than treating it as music.

ffmpeg is not a stylistic choice — `parec`, `parecord --file-format=raw`, and `pw-record` all produce 0 bytes or hang on at least one of the supported adapter chips. ffmpeg is the only capture path that works everywhere we've tested.

**Stream and read back.** Open a TCP socket to the server, send the 18-byte handshake (`b'1'` + the 17-byte ASCII badge MAC — so the server can tell badges apart when more than one is on the air), then push the WAV stream straight from ffmpeg's stdout into the socket. The first 44 bytes are the standard WAV header — the server discards them and feeds raw PCM to Vosk. Use `select()` to multiplex the ffmpeg→socket forward with reading the response signal byte; the server closes the connection right after sending its byte, so always check the read side **before** writing more audio (otherwise you'll get `BrokenPipe` and miss the byte).

**Branch on the signal byte:**

| Byte   | Meaning              | Action                                                       |
|--------|----------------------|--------------------------------------------------------------|
| `b'c'` | command executed     | play `commandexecuted.wav` through the badge                 |
| `b'f'` | no match             | play `commandfailure.wav`                                    |
| `b'v'` | voice response       | read 4-byte big-endian size, then exactly N bytes of WAV; play |
| `b'k'` | keepalive            | reset the recv timeout (and slide the recording deadline), keep waiting |
| `b'W'` | prewarm (downlink)   | bring SCO up now and hold it — a hail is about to arrive     |
| `b'O'` | channel open         | this tap socket is now a live intercom (see `INTERCOM.md`)   |
| `b'A'`+len+PCM | channel audio | peer audio frame (2-byte BE len); pipe into the stdin player |
| `b'X'` | channel closed       | play close chirp, tear down                                  |

The badge-to-badge hail/channel system built on these (aliases, prewarm, hysteretic noise gate, half-duplex mute, close gestures) is documented in `INTERCOM.md`.

The SDK uses `c`, `f`, `v`, `k`, and the four channel bytes above. Other letters are free for your own extensions — a signal byte can trigger any relay-side behavior you like (the author's fuller system uses `l` for dictation-recorded and `p` for prompt-dispatched, for example).

`listener.py` expects these asset files in `assets/` (next to the scripts): `listening.wav` (chirp on tap), `commandexecuted.wav`, `commandfailure.wav`, `badge-to-comms-relay-online.wav` (played on startup once the badge connects), `maincomputeronline.wav` (played once the server is reachable). Any short WAV/MP3 clips work — record or synthesize your own and drop them in under these names.

**Generating the spoken assets — `tts.sh`.** For the spoken (as opposed to purely sound-effect) assets, `tts.sh` writes a WAV from a phrase using the *same* TTS engine `computer.py` speaks with — Windows SAPI (`System.Speech`, female voice, Rate 2) or `espeak-ng -v en-us -s 165` on Linux/macOS — so your startup and acknowledgement tones match the voice that answers commands. Unlike `speak`-style tools it only writes the file; it never plays audio. A bare filename lands next to the script; a path containing a slash is used as given:

```bash
./tts.sh "Access granted."               assets/access_granted.wav
./tts.sh "Main computer online."         assets/maincomputeronline.wav
./tts.sh "Badge to comms relay online."  assets/badge-to-comms-relay-online.wav
./tts.sh "Command executed."             assets/commandexecuted.wav
./tts.sh "Command failure."              assets/commandfailure.wav
```

**Persistent downlink.** On startup `listener.py` opens a second, long-lived connection to the server — handshake `b'h'` + MAC — and holds it open for the life of the session on a background thread. The server sends `b'k'` keepalives every ~5 s; 15 s of silence means the server is gone, and the relay reconnects every 5 s, playing `maincomputeronline.wav` on each successful (re)connect — so a server restart is audible on the badge with no tap. The downlink is also the server's **push path**: a `b'v'` frame arriving on it plays through the badge immediately via the cold-SCO sequence (`ensure_hfp_profile()` → `play_wav_cold()` → teardown), which is how badge-to-badge hails are delivered (see `INTERCOM.md`). An `audio_lock` serializes pushed audio against active tap cycles so a hail can never play over a live command. Taps are not accepted until the downlink's first connect.

**Chirp ordering matters: ffmpeg before pw-play.** The chirp plays *inside* `stream_and_handle_response()`, after ffmpeg has already started and the 44-byte WAV header has arrived. Opening `bluez_input.<MAC>` via ffmpeg triggers SCO negotiation from the capture side — the WAV header is confirmation that the HFP link is live. Playing the chirp before ffmpeg starts means it arrives before SCO is established and falls through to the default output device. The correct sequence:

```
1. Start ffmpeg → wait for 44-byte WAV header (SCO live)
2. play_silence(PRIME_MS_LISTENING)       ← prime output side
3. play_wav(LISTENING_WAV, prime=False)  ← chirp through badge
4. TCP connect, send header, stream PCM
```

`PRIME_MS_LISTENING` controls how long to prime the output side after the input side is confirmed live. Start at 0 ms (the event-driven teardown + ensure_hfp_profile() cycle leaves the link in a consistent state on each tap); bump to 200–500 ms only if the chirp still splits to speakers.

**Event-driven SCO teardown and re-establishment.** `subprocess.run(["pw-play", ...])` is synchronous — it blocks until the last audio frame has left the pipeline. Its return is the definitive "nothing is playing" event. Call `force_sco_teardown()` exactly at that moment:

```python
play_wav(ACK_WAV)         # blocks until playback complete
force_sco_teardown()      # instant, zero-timer — cannot be premature
```

```python
def force_sco_teardown():
    subprocess.run(["pactl", "set-card-profile", CARD, "off"], ...)
```

On the next tap, `ensure_hfp_profile()` re-establishes HFP by setting the profile back to `headset-head-unit` and polling `pactl list sinks short` every 0.5 s until the sink reappears — event-driven, not a fixed sleep:

```python
def ensure_hfp_profile():
    subprocess.run(["pactl", "set-card-profile", CARD, "headset-head-unit"], ...)
    for _ in range(15):
        r = subprocess.run(["pactl", "list", "sinks", "short"], ...)
        if SINK in r.stdout:
            return True
        time.sleep(0.5)
    return False
```

**Hot-path response playback: no silence prime.** When ffmpeg is still running (SCO is live), play the voice response with `prime=False`. Two separate pw-play calls (silence + audio) create a brief gap during which PipeWire re-releases the audio path; re-acquiring it eats the first ~200 ms of the second clip. A single pw-play with no gap avoids this entirely.

**Debounce and evdev queue flush.** `listener.py` closes and reopens the evdev device after each tap cycle. This gives a fresh file descriptor with an empty event queue, discarding any spurious badge re-fires that SCO teardown can trigger. Because of this flush, `last_tap` does *not* need to be reset to `time.time()` after `stream_and_handle_response()` returns — resetting it there would impose an unnecessary 2 s blackout after audio ends. The debounce only fires while the device is held open (within a tap cycle), not across cycles.

See `listener.py` in this folder for the runnable minimal version.

## <a name="computer-py"></a>6. Minimal `computer.py` — Receive, Recognize, Respond

`computer.py` listens on a TCP port (1701 by default; pick anything) and serves each connection on its own thread — multiple badges never block each other. The Vosk `Model` is shared across threads; each connection builds its own `KaldiRecognizer`. A `b'h'`+MAC connection registers as that badge's **persistent downlink** (held open with `b'k'` keepalives; the server can push `b'v'` voice frames down it at any time). A background console thread reads stdin: `badges` lists every badge seen; `hail <mac> [text]` pushes synthesized speech to a badge's downlink — unsolicited playback on that badge, `<mac>` accepting any unique substring. For each `b'1'` tap connection:

1. **Read the 18-byte handshake** (`b'1'` + 17-byte ASCII MAC). The MAC keys the session; MAC-less legacy clients fall back to the sentinel `00:00:00:00:00:00`.
2. **Discard the 44-byte WAV header.** Vosk wants raw PCM, not WAV.
3. **Recognize.** Build a `KaldiRecognizer(model, 16000)`. Loop on `conn.recv(4096)`; for each chunk, call `AcceptWaveform(chunk)` then poll `PartialResult()` and `Result()`. Match against your command vocabulary on every poll — fire as soon as a match appears, don't wait for end-of-utterance. Cap the loop at ~10 s of wall time.
4. **On match: act.** A "command" can be anything — play a sound locally, run a shell command, hit an API. The minimal server prints the recognized text and picks a canned response.
5. **Acknowledge.** Send `b'c'` (matched), `b'f'` (no match within timeout), or `b'v'` followed by `<4-byte big-endian size><WAV bytes>` to deliver a voice response.

`computer.py` uses the unconstrained small model and substring matches against a tiny phrase list — easiest to start with, fine for a few commands. If you grow to dozens of phrases, Vosk also accepts a constrained grammar (a JSON list of exact phrases passed to `KaldiRecognizer`), which keeps recognition dramatically tighter at the cost of only hearing what's in the list.

**Windows TTS via SAPI.** `computer.py` branches on `sys.platform == "win32"` and falls back to PowerShell `System.Speech.SpeechSynthesizer` — no extra dependencies on Windows:

```python
def _synth_sapi(text, path):
    safe = text.replace("'", "''")   # escape for PS single-quoted string
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate = 2; "
        f"$s.SetOutputToWaveFile('{path}'); "
        f"$s.Speak('{safe}'); "
        "$s.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   check=True, capture_output=True, timeout=15)
```

On Linux/macOS it falls back to `espeak-ng`. Neither path requires Piper or network TTS.

**Windows Ctrl-C.** Winsock's `accept()` is not interruptible by Python's signal handler on Windows — Ctrl-C has no effect while the server is blocked in accept. Fix: set a 1 s timeout on the listening socket and catch `socket.timeout` in the accept loop:

```python
srv.settimeout(1.0)
while True:
    try:
        conn, addr = srv.accept()
    except socket.timeout:
        continue   # allows KeyboardInterrupt to be delivered between attempts
```

A simple way to generate the response WAV (espeak-ng on Linux):

```python
subprocess.run(["espeak-ng", "-w", "/tmp/resp.wav", "Acknowledged."], check=True)
```

Then, framed for the relay:

```python
with open("/tmp/resp.wav", "rb") as f:
    data = f.read()
conn.sendall(b'v')
conn.sendall(len(data).to_bytes(4, 'big'))
conn.sendall(data)
```

Run it as:
```bash
python3 computer.py /path/to/vosk-model-small-en-us-0.15
```

See `computer.py` in this folder for the runnable minimal version.

## <a name="badge-playback"></a>7. Badge Playback

When the relay receives `b'v'`, it reads `<4-byte size><WAV>` off the socket, writes the WAV to a temp file, and plays it through the badge speaker:

```bash
pw-play --target bluez_output.<MAC_underscored>.1 \
        --media-role=communication \
        --volume 0.5 /tmp/resp.wav
```

### Cold SCO start: `play_wav_cold()`

**`pw-play` alone cannot reliably bring up a cold HFP output link.** Targeting the badge sink from `pw-play` on a cold link often falls through to the default output device — HFP input-side and output-side SCO are negotiated independently, and `pw-play` triggers only the output side, which is less reliable.

The reliable pattern uses ffmpeg to trigger SCO negotiation from the **capture side** first:

```python
def play_wav_cold(path):
    ffmpeg = subprocess.Popen(
        ["ffmpeg", "-f", "pulse", "-i", SOURCE,
         "-ar", "16000", "-ac", "1", "-f", "wav", "-loglevel", "quiet", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        header = b""
        while len(header) < 44:
            r, _, _ = select.select([ffmpeg.stdout], [], [], 10)
            if not r:
                return   # SCO link timeout
            chunk = ffmpeg.stdout.read(44 - len(header))
            if not chunk:
                return
            header += chunk
        # WAV header received = SCO link confirmed live
        play_silence(PRIME_MS_COLD)        # wake output path (1000 ms)
        play_wav(path, prime=False)
    finally:
        if ffmpeg.poll() is None:
            ffmpeg.terminate(); ffmpeg.wait(timeout=0.5) or (ffmpeg.kill(), ffmpeg.wait())
```

ffmpeg writes the 44-byte WAV header as soon as `bluez_input.<MAC>` opens successfully — that header arrival is the SCO confirmation signal. No fixed sleep; the wait is only as long as the link actually takes (typically < 1 s on a warm adapter).

### Silence priming

On the hot path (SCO already established, ffmpeg running), **do not use a silence prime**. Two separate `pw-play` calls (silence + audio) create a brief gap in which PipeWire re-releases the audio path; re-acquiring it eats the first ~200 ms of the second clip. Play audio directly with `prime=False` when SCO is already live.

On a cold link, silence priming after `play_wav_cold()`'s WAV-header confirmation is still needed to wake the output side. `PRIME_MS_COLD = 1000` works reliably; values below ~500 ms can still clip on some adapters.

```python
import tempfile, wave, subprocess, os
def play_silence(ms, sink):
    samples = int(16000 * ms / 1000)
    fd, path = tempfile.mkstemp(suffix='.wav'); os.close(fd)
    with wave.open(path, 'w') as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
        wf.writeframes(b'\x00\x00' * samples)
    subprocess.run(["pw-play", "--target", sink, "--media-role=communication", path],
                   check=False, capture_output=True, timeout=5)
    os.unlink(path)
```

### Event-driven SCO teardown

`pactl set-card-profile CARD off` collapses the SCO link. Call it exactly when the last `pw-play` subprocess returns — `subprocess.run()` is synchronous, so its return is the definitive "nothing is playing" event:

```python
subprocess.run(["pw-play", ...])   # blocks until last audio frame leaves pipeline
subprocess.run(["pactl", "set-card-profile", CARD, "off"], ...)   # instant, non-premature
```

On the next tap, re-establish with `set-card-profile … headset-head-unit` and poll `pactl list sinks short` for the sink to appear — event-driven, not a fixed sleep. See §5 for the full `ensure_hfp_profile()` pattern.

**Sink and source name format, restated for emphasis:**

```
source:  bluez_input.AA:BB:CC:DD:EE:FF        (colons, no suffix)
sink:    bluez_output.AA_BB_CC_DD_EE_FF.1     (underscores, .1 suffix)
card:    bluez_card.AA_BB_CC_DD_EE_FF         (underscores, no suffix)
```

Yes, the source uses colons even though the sink and card use underscores. Yes, this is real and not a typo. PipeWire inherits the inconsistency from BlueZ.

## <a name="complete-loop"></a>8. The Voice Command Pipeline at a Glance

```
   BADGE             listener.py                         computer.py
     |                   |                                    |
     | tap (KEY_PAUSECD) |                                    |
     |─────────────────▶ |                                    |
     |                   | ensure_hfp_profile()               |
     |                   | ffmpeg -f pulse -i bluez_input...  |
     |◀── SCO link up ───|  (opening SOURCE triggers SCO)     |
     |                   | (wait for 44-byte WAV header)      |
     |                   | pw-play listening.wav              |
     | ◀──── chirp ──────|                                    |
     |                   | TCP connect, send b'1'+MAC + header|
     |                   |───────────────────────────────────▶|
     |     mic audio     |   raw PCM stream                   |
     |─────────────────▶ |───────────────────────────────────▶| feed Vosk
     |                   |                                    | AcceptWaveform/Partial
     |                   |                                    | match → action
     |                   |                                    | synth_wav (TTS)
     |                   | b'v' + 4-byte size + WAV bytes     |
     |                   | ◀──────────────────────────────────|
     |                   | pw-play (no prime; SCO already hot)|
     | ◀── response ─────|                                    |
     |                   | force_sco_teardown()               |
     |                   |                                    |
```

End-to-end latency on a healthy adapter: ~1.5–2 s tap-to-chirp, ~300–800 ms recognition after end-of-speech, ~400 ms playback start. Most of the tap-to-chirp time is SCO link setup, not your code.

## <a name="known-gotchas"></a>9. Known Gotchas

**WirePlumber seat-monitoring on headless systems.** Covered in §3. If `bluetoothctl show` doesn't list `Handsfree Audio Gateway`, this is almost certainly the problem.

**`pw-play` exits 1 silently from a launcher.** The child needs a real session env: `HOME`, `USER`, `LOGNAME`, `PATH`, `XDG_RUNTIME_DIR`, and `DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus`. From a terminal these are all set; from `runuser`/`sg`/`sudo` they're stripped. Build the env dict explicitly.

**ffmpeg is the only reliable capture path.** `parec`, `parecord --file-format=raw`, and `pw-record` produce 0 bytes or hang on some adapters (notably anything Realtek). `ffmpeg -f pulse -i bluez_input.<MAC>` works everywhere.

**Source colons vs. sink underscores.** `bluez_input.AA:BB:CC:DD:EE:FF` (colons) but `bluez_output.AA_BB_CC_DD_EE_FF.1` (underscores + `.1`). Bake helpers for both.

**Read response byte before writing more audio.** The server closes the socket immediately after sending the signal byte. If your loop writes another chunk first, you'll hit `BrokenPipe` and lose the byte. Always check `select()`'s read side first; on `BrokenPipeError` make one last short-timeout `recv(1)` attempt before giving up.

**Single tap fires code 200 *or* 201.** `KEY_PAUSECD` is 201, but the badge alternates after audio activity — accept both.

**Double-tap is not a keypress.** It's emitted by the badge as `AT+BVRA=1` over the HFP control channel. Watch with `btmon` (running under a pty for line-buffered output) and pattern-match the line. The minimal SDK skips double-tap; add it when you need cancel/finalize semantics.

**`pw-play` alone cannot establish a cold SCO output link.** On a cold HFP link, targeting the badge sink via `pw-play` often falls back to the default output. The only reliable cold-start path opens `ffmpeg -f pulse -i bluez_input.<MAC>` first — this triggers SCO negotiation from the capture side. The 44-byte WAV header emitted by ffmpeg when the source opens is the signal that the link is live; only then is `pw-play` reliable. See `play_wav_cold()` in §7.

**Two `pw-play` calls on a hot SCO link eat the start of the second clip.** If you play silence then audio as two separate `subprocess.run(["pw-play", ...])` calls while SCO is already active, PipeWire briefly re-releases the audio path between them. Re-acquiring it eats ~200 ms of the second clip. Use a single `pw-play` call (no prime) when SCO is already live.

**SCO teardown is event-driven, not timer-based.** `subprocess.run(["pw-play", ...])` blocks until the last audio frame leaves the pipeline — its return is the definitive "nothing is playing" event. Call `pactl set-card-profile CARD off` immediately after for instant, non-premature teardown. Timer-based teardown (sleep N seconds then tear down) is always either too early or too late. Re-establishment likewise should poll for sink state change, not sleep a fixed time.

**SCO teardown saves ~5 s of dead time but adds a wake-up cost.** After confirmation, `pactl set-card-profile bluez_card.<MAC> off` collapses the lingering SCO link. The next tap then has to re-establish HFP via `set-card-profile … headset-head-unit` and poll for the sink to reappear (up to ~5 s). Net win on most adapters; worth making a config switch if your hardware disagrees.

**CRLF in shell scripts.** If you edit `.sh` files on Windows and run them on Linux, the kernel will look for `/bin/bash\r` and tell you "required file not found". Fix: `sed 's/\r//' f.sh > f.sh.tmp && mv f.sh.tmp f.sh`. Note: `sed -i` is unsafe on Windows bash (MSYS2) — use the temp-file form.

**TCP framing is positional, not length-prefixed (mostly).** The 18-byte handshake (`b'1'` + 17-byte MAC) and the WAV stream are framed by position and the WAV header. The voice response is the only length-prefixed frame: `b'v'` then 4 BE bytes then exactly that many bytes. Don't `recv(4096)` for the size — read exactly 4, then loop on the body until you've received the full count.

## <a name="vibe-control"></a>10. Vibe Control — driving a terminal by voice

**Optional, Windows-only.** Full design in <VIBE.md>.

Vibe Control turns the badge into a remote control for a terminal session running on the **server** machine. Tap, say *"computer proceed"*, and an Enter lands in the window it is latched to. The reference target is a Claude Code session, so you can supervise a long-running interactive program from across the room — screen cast to a television, no keyboard in reach. It does not know or care what is running there; it sends Enter, Escape, Right Arrow, digits, and pasted text at whatever window the target rule matches.

```
computer activate vibe control     → "Vibe control latched to sunshine. Ready."
computer proceed                   → Enter
computer continue / carry on       → Right Arrow, then Enter (accept the suggestion, submit)
computer cancel                    → Escape, twice
computer option one … nine         → 1 … 9
computer wake up                   → recover a blanked display
computer deactivate vibe control   → "Vibe control released."
```

Three things about it are worth knowing before you read <VIBE.md>:

- **It extends the vocabulary rather than replacing it.** While active, the phrases above are added to `COMMANDS` and everything already there keeps working — you can still ask the time without dropping the latch. (The full system does the opposite and suspends its normal vocabulary outright. That gate earns its keep there, where the vocabulary is large: narrowing it sharpens recognition and stops a stray command firing while you concentrate on a terminal. With a handful of phrases there is little to sharpen and little to fire by accident, so here it was only a restriction.) Hails are never suppressed either: an incoming call reaches the badge in any mode.
- **It refuses rather than guesses.** Exactly one target session may be running at activation; zero or several and it declines out loud, with no state left behind. Sessions are **counted** by process and **targeted** by window class + title, and the two counts must agree — a title rule that has silently stopped matching fails loudly instead of latching onto the wrong window.
- **Commands that act, chirp — they don't talk.** `computer.py` grew an `ACK` sentinel for this: return it from any command to acknowledge with the badge's short chirp instead of a synthesized sentence. A spoken reply after every keystroke would make the mode unusable, and it's the right answer for any command whose effect you can already see.

On a non-Windows server the import fails, the phrases are absent, and everything else is unchanged.

## <a name="large-vocab"></a>11. Large-Vocabulary Dictation — `captain's log` and `computer transcribe`

**Optional.** Enabled by passing a second model path; absent otherwise.

Everything up to this point matches a phrase you defined in advance. Dictation is the opposite problem: capturing a sentence nobody can enumerate ahead of time. That needs a different recognizer, and switching to it mid-utterance is the entire mechanism behind both features here.

```
computer.py <small-model> <large-model>

  captain's log <anything>       → timestamped line appended to captainslog.txt
  computer transcribe <anything> → pasted into the Vibe Control window, unsubmitted
  computer replay last log entry → speaks the most recent entry back
```

The first two are *triggers*; the third is an ordinary command (see below).

### Two models, one switch

The **small** model (~40 MB) runs on every tap. It loads in seconds and is accurate enough to pick a known phrase out of a handful of candidates, but it is poor at open dictation — a small vocabulary has to guess at words it does not really know. The **large** model (`vosk-model-en-us-0.22`, ~1.8 GB unpacked) transcribes arbitrary English well and is far too heavy to run on every tap.

So the server keeps both. `detect_trigger()` watches the small model's running hypothesis for a trigger phrase; when one appears, `handle_large_vocab_phase()` builds a fresh recognizer on the large model and **replays every buffered audio chunk into it**.

That replay is the part worth understanding. The trigger is only recognized *partway through* the sentence — by the time "captain's log" has been decoded, you are already several words into the entry, and that audio has been consumed by the small recognizer. `computer.py` was already buffering the raw PCM of each tap (the intercom needs it, to deliver a hail in the caller's actual voice), so the fix is to feed the buffer back: the large model transcribes the utterance from the tap, and nothing spoken before the switch is lost.

Capture then continues on the live socket until you stop talking.

### The large model is loaded at startup, never on demand

This is the one decision in the feature that is not a preference. Loading `vosk-model-en-us-0.22` takes tens of seconds and gigabytes of RAM, and a dictation begins **inside a live badge transaction** — the relay is already recording, and `listener.py` gives up `RECORD_MAX_S` (13 s) after the last byte from the server. Loading on first use would therefore blow the recording window every time, and the badge would fail the very command that triggered the load.

Paying it once at startup is what makes the switch feel instantaneous. The cost is a slower launch and a resident ~2 GB; if you do not want either, omit the argument and the feature is simply not there.

### Availability

| Trigger | Vibe Control inactive | Vibe Control latched | Where the text goes |
|---|---|---|---|
| `captain's log …` | ✅ | ✅ | appended to `captainslog.txt` |
| `computer transcribe …` | — | ✅ | pasted into the latched window |

`computer transcribe` needs a latch because it pastes into the window Vibe Control has latched onto — with no latch there is nowhere for the text to go. That is a real dependency, not a policy.

`captain's log` has no such dependency: it writes a file and touches no window, so it stays available in both states. (The full system suspends it during Vibe Control, as part of suspending its whole normal vocabulary — see §10 for why that trade doesn't carry over to a vocabulary this small.)

`computer transcribe` **does not press Enter.** The prompt lands in the composer and sits there until you read it and say `computer proceed`. That separation is the whole safety argument for letting a voice pipeline type into a terminal — recognition *will* occasionally mishear, and the review step is what makes that a nuisance rather than an incident. It is also why this works with no display: the terminal you are dictating into is the display.

### Playing a log back

`computer replay last log entry` speaks the most recent entry, re-rendering the
stored timestamp as spoken English rather than reading digits and brackets aloud:

```
[2026-08-01 14:30:00] captain's log, the away team has returned
   ↓
"Captain's log, August 1, 2026, 14 30. the away team has returned."
```

This one is an ordinary `COMMANDS` entry, not a trigger — a fixed phrase with a
spoken answer, which is what `COMMANDS` is for. Only the *recording* half needs
the large model. It is registered alongside the dictation feature anyway, so the
log commands appear and disappear together rather than offering playback of
entries this server has no way to record.

A stored entry keeps its `captain's log` prefix, and the announcement above
already opens with it, so the prefix is stripped before speaking — otherwise
every replay would say it twice.

### Watching it work

Both features stream the running hypothesis to the server console, word by word, as you speak:

```
[computer] [AA:BB:CC:DD:EE:FF] large-vocab trigger: captains_log
[computer] [AA:BB:CC:DD:EE:FF] > captain's log stardate forty seven six three four
[computer] [AA:BB:CC:DD:EE:FF] silence 1.5s — finalizing
[computer] [AA:BB:CC:DD:EE:FF] captain's log recorded (9 words)
```

Vosk *revises* its hypothesis as it goes, so a partial does not always extend the previous one. When it does not, the line breaks and the corrected hypothesis is reprinted whole rather than splicing a fragment into the middle of a word.

Note the privacy consequence: the full system routes log content to a separate display and keeps its server console discreet. This SDK has no second display, so the log appears on the server terminal. If that terminal is visible to other people, so is your log.

### Tuning

| Variable | Default | Meaning |
|---|---|---|
| `SDK_DICTATION_SILENCE_S` | `1.5` | end of a log entry — seconds without a new word |
| `SDK_PROMPT_SILENCE_S` | `6` | end of a transcribed prompt |
| `SDK_DICTATION_MAX_S` | `110` | hard cap on one dictation |
| `SDK_CAPTAINSLOG_FILE` | `captainslog.txt` | where log entries are appended |

The two silence gates differ because the two features are *spoken* differently, and both numbers are inherited from measured use rather than guessed. A log entry is composed before you start talking and delivered in one go, so a short gap means you are done. A prompt is thought out while speaking — you stop to consider the next clause, and 1.5 s would paste half a sentence into a terminal. The hard cap is the backstop for a room noisy enough to keep resetting the gate.

The silence timer does not start until the first word is recognized, so a pause between the tap and your first syllable never ends the capture — only a gap *between* words does.

### Adding your own

A trigger is a prefix with open speech behind it, which is why these cannot live in `COMMANDS` (a fixed phrase mapping to a fixed outcome). To add one: return a new type from `detect_trigger()`, and handle it at the bottom of `handle_large_vocab_phase()`. The capture loop itself is generic.

Two things to keep:

- **Every word of your trigger phrase must exist in the small model's vocabulary**, because the small model is what recognizes it. A phrase the small model cannot decode is unreachable by voice no matter how good the large model is.
- **Strip the trigger with `find()`, not a fixed slice.** The large model re-transcribes from the beginning and may render the trigger differently than the small model matched it. `strip_trigger()` does this, and falls back to returning the whole transcript rather than nothing — a stray leading word beats a silently truncated first sentence.

### Known gotchas

**The apostrophe.** Vosk renders the possessive inconsistently, and unlike the full system this server runs the small model *unconstrained* (no grammar), so the spelling cannot be forced. Both `captain's log` and `captains log` are accepted; a trigger of your own with a possessive in it should do the same.

**Keepalives are mandatory, not an optimization.** `listener.py` stops recording 13 s after the last byte from the server, and a dictation routinely runs longer. The capture loop sends `b'k'` every 4 s to slide that deadline. Remove it and every dictation truncates at 13 seconds.

**Triggers are matched as substrings, not anchored to the start.** The full system anchors them, because its recognizer is grammar-constrained and its text is therefore clean. This server runs the small model open, where a stray decoded syllable ahead of the trigger is ordinary — anchoring would drop real commands.

## 12.  More Info

This SDK is the distilled foundation of a much larger system — the Terran Operating System (TOS), the author's full starship-computer environment built on this same voice command pipeline (voice-print identity, command vocabularies, dictation, an AI main computer, and more). To see where this foundation can lead, visit https://tos.md.
