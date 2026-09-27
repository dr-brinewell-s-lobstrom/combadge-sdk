# License

See <LICENSE.md>.  This SDK is essentially a component of TOS and subject to its licensing terms.

# Combadge SDK

Turn a Bluetooth combadge into a working communicator and control system with a "main computer" behind it.

(While this SDK was primarily designed with the Fametek TNG Combadge in mind, it's built on Linux & Windows and is wired into the Bluetooth stack, and therefore can work with any bluetooth headset with a standard HFP button - essentially compatible with any two-way communication device (mic+speakers) with a standard Play/Pause button, such as the Sony WH1000XM3, as one of countless examples of devices with this capability.)

The FameTek Star Trek: TNG combadge pairs as an ordinary Bluetooth Hands-Free headset - out of the box it can answer your phone (bi-directional comms), play audio (music, notification sounds), and it has a single- and double-tap button for interaction.  What I see in this device is the 24th century modality of human-computer interaction brought to life.  Thank you, Fametek:  As far as building the rest of the ship:  I'll take it from here.  :)

This SDK gives the badge what it always implied: a computer on the other end. Three small Python scripts turn a badge tap into a complete voice command pipeline - tap the badge, hear a chirp, speak a phrase, and a synthesized voice answers back through the badge speaker. What a phrase does is yours to define in a plain Python dictionary: launch a program, report the time, run a shell command, switch on the lights, control whatever your computer can reach.

With two badges it becomes an **intercom**: tap and say *"captain to engineering"* and your actual voice plays from the other badge; a tap there answers, opening a live two-way channel between the badges until either wearer closes it. (See <INTERCOM.md> for the full design.)  Define your own user names, locations, hail and communicate with them *canonically* - from anywhere in the world that TCP/IP can reach, including space.

There is no AI during use.  No cloud dependency - EVERYTHING can run locally. Speech recognition is Vosk (offline), responses are local text-to-speech, and the transport is your own LAN - no cloud, no accounts, no LLM. The badge side needs a Linux machine with Bluetooth (a spare laptop or a Pi is plenty); the server side runs anywhere Python runs - Linux, macOS, or Windows, on the same machine or another on the network, or self-hosted on the internet if your crew is dispersed.

From this, I built TOS - an AI harness & guidance system, a home & system automation controller, even a theatrical co-performer with a famous starship computer voice - see it in action at https://tos.md and use this SDK to build your own.

While AI was obviously used for generating code - EVERY prompt was human-written or verified, EVERY file edit and every code change was human-supervised and audited.  Never auto-mode (see <LICENSE.md> for philosophy on that.)  It took nearly 6 months to produce the mere ~44K tokens that make up this SDK, because I started with the bigger TOS system, which itself is a mere ~300K tokens due to careful architecture and slow building.  This is no slop, it is a human-driven labor of love - there isn't a single line of code that I don't personally understand and had thought about carefully when it was being written - though I admit I am trusting a *bit* more to Claude now without line-by-line auditing, and working faster now than when I started.

### Table of Contents

0. [Quick Start](#quick-start)
1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Pairing and Connecting the Badge](#pairing-and-connecting)
4. [Minimal `transceiver.py` - Connection Manager](#transceiver-py)
5. [Minimal `listener.py` - Tap and Audio Handler](#listener-py)
6. [Minimal `computer.py` - Receive, Recognize, Respond](#computer-py)
7. [Badge Playback](#badge-playback)
8. [The Voice Command Pipeline at a Glance](#complete-loop)
9. [Known Gotchas](#known-gotchas)
10. [Vibe Control - driving a terminal by voice](#vibe-control)
11. [Large-Vocabulary Dictation - captain's log and computer transcribe](#large-vocab)
12. [Game Mode - the tap as a mouse click](#game-mode)
13. [Naming a New Badge by Voice](#onboarding)

## <a name="ai-assisted-quick-start"></a>-1.  AI Assisted Quick Start

Point your AI at this repo and `make it so`.  Note that this approach is technically a 'permissible license violation' with remediation steps described within <LICENSE.md> (in short: If you scan this with AI, you have to admit you did so, to avoid being in violation of the license, since this was made for humans. :)

## <a name="quick-start"></a>0. Quick Start

Two terminal windows are all you need. The **relay host** is the Linux machine the badge is paired to. The **server** (running `computer.py`) can be the same machine or any other machine on the same network - Linux, macOS, or Windows.

**Step 0 - Install the dependencies** (once per machine):
```bash
./install-dependencies.sh            # Linux: transceiver + server; Windows (Git Bash / MSYS2): server
./install-dependencies.sh --check    # report only
```
It checks first, lists what is missing, and asks before installing: the system packages (Debian/Ubuntu `apt`), the Python packages (`vosk`, `piper-tts`), both Vosk models into `../.vosk/` and the Piper voice Lessac low into `../.piper/` (about 2 GB of downloads, most of it the large Vosk model). `--transceiver` or `--server` limits it to one role. It installs dependencies only: there is no setup step, because everything else configures itself on first use - badges are paired by the transceiver and named by voice. The full list is in §2.

**Step 1 - Start the server** (any OS with Python):
```bash
./computer.sh
```
That is `computer.py` with both Vosk models: the small one for commands, the large one for dictation - captain's log, a core feature ([§11](#large-vocab)). Both are required; `computer.sh` stops with a pointer to the installer if either is missing. Run by hand: `python3 computer.py ../.vosk/vosk-model-small-en-us-0.15 ../.vosk/vosk-model-en-us-0.22`.

**Step 2 - Start the transceiver** (Linux, the host the badges pair to; asks for sudo):
```bash
./transceiver.sh                                  # computer.py on this machine
SDK_SERVER_HOST=192.168.x.x ./transceiver.sh      # computer.py elsewhere
```
Power a badge on nearby: the transceiver finds it, pairs it on a free Bluetooth adapter, and the server asks its wearer to name it (§4, §13).

`transceiver.sh` expands `SDK_SERVER_HOST` (default `localhost`) and `SDK_SERVER_PORT` (default 1701) in your shell and puts them on the `sudo` command line. By hand it is `sudo SDK_SERVER_HOST=<host> python3 transceiver.py`, and the variable **must** be inline there: `sudo` resets the environment, so a prior `export SDK_SERVER_HOST=...` is silently stripped and the listeners fall back to `localhost` (symptom: endless "waiting for server localhost:1701" while the server runs fine elsewhere). Hostnames work if the transceiver host resolves them.

**Server and relay on one machine** (`multiuser/PI.md` phase 4, verified on PAN
2026-09-27): run `computer.sh` on the relay host itself and start the
transceiver with no `SDK_SERVER_HOST`; it defaults to `localhost`. Run the
server as the ordinary user, the transceiver as root. Keep the Vosk and Piper
models on the host's own disk, not a network mount. On PAN (Core 2 Duo P8400,
2008, 7.7 GB) with both Vosk models and Piper Lessac low: listening 13 s after
launch; greetings, commands, onboarding, hails both ways, captain's log and
replay all as with a separate server; *"computer time"* spoken in 0.5 s. The
cost is memory: **about 5 GB resident with the large model** (dictation),
under 1 GB without it.

**Pointing a relay at a different server, by hand.** There is no server
discovery yet (`multiuser/PI.md` phase 5, on hold), so moving the badges to
another server (say a Windows box running `computer.sh`) is manual:

1. **On the new server:** `./install-dependencies.sh --server` (Python
   packages, both Vosk models, the Lessac low voice); start `computer.sh`. On Windows,
   allow inbound TCP 1701 through the firewall - Python's first listen
   usually prompts for it, and a dismissed prompt means the relay never
   connects.
2. **Copy `aliases.conf` from the old server.** Badge names live on the
   server, beside `computer.py`, not on the relay host: a server without them
   treats every badge as new and starts the naming dialogue again. The
   captain's log (`captainslog.txt`) likewise stays with the server that
   wrote it.
3. **On the relay host** (SSH): stop the transceiver; optionally stop a local
   `computer.py` (harmless if left, but ~5 GB with the large model); restart
   the transceiver pointed at it: `SDK_SERVER_HOST=192.168.x.x ./transceiver.sh`
   (by hand, inline on `sudo`: `sudo SDK_SERVER_HOST=192.168.x.x python3
   transceiver.py`). The transceiver
   hands the address to each listener as it launches them, so changing server
   always means restarting the transceiver.

Onboarding on/off and health stay with the relay host and are unaffected. To
go back, restart the transceiver without `SDK_SERVER_HOST` (`localhost`). If
the other server goes away, the badges say *"Main computer offline."* and
retry its address every 5 s; nothing falls back to the local server on its
own.

**Step 3 - Test it:**  
Tap the badge once. You hear a chirp from the badge speaker. Speak one of these phrases:

| Say this…             | Badge plays back…      |
|-----------------------|------------------------|
| "computer hello"      | "Hello."               |
| "computer status"     | "All systems nominal." |
| "computer time"       | current time readout   |
| "computer goodbye"    | "Acknowledged."        |

Edit the `COMMANDS` dict in `computer.py` to add your own phrases and responses.

> **If you outgrow the dict** - say you move commands to an external file (CSV,
> JSON, whatever) so you can edit them without restarting the server - resist
> the obvious shortcut of re-reading the file on every tap. It feels free with
> 20 commands, but the cost grows with the file and is invisible until it
> isn't: at a few thousand phrases, the per-tap reparse costs ~90 ms of pure
> waste (measured on the full TOS at 5,488 phrases). The right pattern costs
> ~10 lines: on each tap, `os.stat` the file and reparse **only when its
> mtime/size changed**, otherwise serve the previously parsed result from
> memory. You keep live editing (a save changes the mtime, so the next tap
> picks it up) and taps stay stat-cheap forever. No polling, no watcher
> threads - just a lazy check inside the tap handler, exactly where the
> file read used to be. (The in-memory `COMMANDS` dict itself scales fine -
> matching is a substring scan - this note is only about file re-reading.)

**Before your first run - checklist:**
- [ ] `./install-dependencies.sh --check` reports everything installed, on each machine
- [ ] WirePlumber seat-monitoring fix applied if the transceiver host runs headless/SSH (see §3)
- [ ] Badge powered on near the transceiver host (it pairs itself - §4)

---

The three scripts in this folder (`transceiver.py`, `listener.py`, `computer.py`) are deliberately minimal and heavily commented. They carry only the voice command pipeline (badge tap > command execution > voice response to badge) and the intercom - no user identity, no command-file dispatch, no dictation modes - so every piece that remains is essential and understandable. Read a section here for context, then read the corresponding file for the code. They are a foundation to build on, not a framework to configure.

(`vibewin.py` and `vibekeys.py` are a fourth, **optional** piece - Vibe Control, §10 - and `clicker.py` is a fifth, Game Mode, §12. All three are Windows-only, `computer.py` imports them defensively, and nothing else depends on them. On any other platform the imports fail, the phrases are simply absent, and the rest is unchanged.)

---

## <a name="overview"></a>1. Overview

The TNG combadge presents itself to a Linux Bluetooth host as **two devices in one**:

1. A **Bluetooth Hands-Free (HFP)** audio device - bidirectional 16 kHz mono audio over an SCO link. Visible in PipeWire as a `bluez_card.<MAC>` with profile `headset-head-unit`, exposing a `bluez_input.<MAC>` source and `bluez_output.<MAC>.1` sink.
2. An **HID input device** - the physical badge tap surfaces as keypress events on `/dev/input/eventX`. A single tap fires `KEY_PAUSECD` (key code 201; some firmware revisions use 200). A double tap is *not* a separate keycode - it's an `AT+BVRA=1` command that the badge issues over the HFP control channel; you watch for it with `btmon`. And while the SCO audio link is up, a *single* tap is not a keycode either: the button becomes call control and sends a hang-up, `AT+CHUP`, also visible only on `btmon`.

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
- Linux host with a Bluetooth adapter that supports HFP Audio Gateway. Verified working: built-in Intel adapters, generic CSR dongles, TP-Link UB500 (Realtek RTL8761B). *(Historical note: early testing attributed an SCO packet fragmentation bug to the RTL8761B chipset; that attribution proved stale - the same adapter later passed full round-trips, and a second host ran the identical chipset without issue throughout.)*

**OS:** Debian/Ubuntu-flavored Linux is the tested baseline. Anything with PipeWire ≥ 0.3.50 and BlueZ ≥ 5.60 should work. The Main Computer side runs anywhere Python and Vosk run (Linux, macOS, Windows).

`./install-dependencies.sh` installs all of the following (§0); this is what it does, for other distributions or for doing it by hand.

**Transceiver host (Linux):**
```bash
sudo apt install bluez pipewire pipewire-pulse wireplumber libspa-0.2-bluetooth \
                 pulseaudio-utils ffmpeg python3 python3-evdev
```
`libspa-0.2-bluetooth` is PipeWire's Bluetooth plugin: without it there is no HFP audio at all. `evdev` comes from apt as `python3-evdev`: on Debian 12+ / Ubuntu 23.04+ Python is "externally managed" and a plain `pip install evdev` is refused.

**Server host:**
```bash
sudo apt install python3 python3-pip espeak-ng curl                   # Linux; espeak-ng is the fallback voice
sudo pip install --break-system-packages vosk piper-tts               # Linux, externally managed Python
pip install vosk piper-tts                                            # Windows
```
vosk and piper-tts are not packaged by Debian, so on an externally managed Python they go into `/usr/local` with `--break-system-packages` (omit the flag where pip does not ask for it).

**Vosk models** from https://alphacephei.com/vosk/models, unpacked into `../.vosk/` (where `computer.sh` looks): `vosk-model-small-en-us-0.15` (~40 MB, commands) and `vosk-model-en-us-0.22` (1.8 GB, dictation; ~5 GB of RAM in use). Both are required.

**TTS for responses:** `computer.py` speaks with **Piper**, Piper's standard US English female voice **Lessac, low quality**, wherever Piper and that voice are installed; otherwise it falls back to the built-in PowerShell `System.Speech` synthesizer on Windows (a female voice, Zira on stock Windows; no install required) or `espeak-ng` on Linux/macOS. The server log's startup line says which it is using.

To use Piper (recommended, on any OS):
```bash
pip install piper-tts
mkdir -p ../.piper        # beside ../.vosk/; ~/.piper/ also works
curl -L -o ../.piper/en_US-lessac-low.onnx      https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/low/en_US-lessac-low.onnx
curl -L -o ../.piper/en_US-lessac-low.onnx.json https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/low/en_US-lessac-low.onnx.json
```
`SDK_PIPER_MODEL=/path/to/voice.onnx` names a different voice outright. The model loads once at startup and each sentence is cached, so a repeated one (greetings, *"Channel open."*) is synthesized once.

Why **low**: a badge's audio is 16 kHz, low's native rate, so low and medium sound the same on a badge (Captain, 2026-09-27), and low is the fastest. Seconds to render *"Captain, online."* / a 6 s answer:

| host | lessac-low | lessac-medium | lessac-high |
|---|---|---|---|
| PAN (Core 2 Duo P8400, 2008) | 0.47 / 1.8 | 0.52 / 2.3 | 4.3 / 17.5 |
| CUBE (Windows desktop) | 0.10 / 0.13 | - | - |

The Lessac dataset's licence is CSTR's Blizzard 2013 licence (see the voice's `MODEL_CARD`); the SDK does not ship the voice.

For the fallback on Linux, install `espeak-ng`:
```bash
sudo apt install espeak-ng
```
You can verify it works independently:
```bash
espeak-ng -v en-us+f3 -w /tmp/test.wav "Acknowledged."
```
The SDK asks for a female voice on both platforms: `en-us+f3` on espeak-ng, and a female SAPI voice (Zira on stock Windows).

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

`AlreadyExists` on `pair` is fine - it means the badge was paired previously; just `trust` and `connect`.

**Verify HFP is registered:**
```bash
bluetoothctl show | grep -i handsfree
# expect: UUID: Handsfree Audio Gateway   (0000111f-0000-1000-8000-00805f9b34fb)
```

If that line is missing, the host has BlueZ but no HFP profile registered - almost always WirePlumber's bluetooth monitor isn't running (see Gotchas: WirePlumber Seat Monitoring). Apply the seat-monitoring fix below before continuing, or HFP will never come up on a headless/SSH-only system.

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

Note the asymmetry: **sinks** use underscores in the MAC and append `.1`. **Sources** use colons and have no suffix. This trips everyone - keep it visible.

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

## <a name="transceiver-py"></a>4. Minimal `transceiver.py` - Connection Manager

`transceiver.py` runs as **root** and supervises **every combadge paired to the host: one badge per Bluetooth adapter, one `listener.py` per badge.** With one adapter and one badge it is exactly the single-badge transceiver it always was; add a second USB adapter and pair a second badge to it, and both run side by side on one host - two badges, one relay box, the intercom between them (validated on PAN, 2026-09-26: two TP-Link UB500s, two badges, server on another machine).

It talks to BlueZ over **D-Bus** (`busctl`), addressing every object by its adapter - `/org/bluez/hci0/dev_2C_F2_DF_45_EC_28` - rather than through `bluetoothctl`. `bluetoothctl list` shows every adapter, but each command-line invocation (`devices`, `info`, `connect`) acts on the **default** one; `select` switches adapters only for the rest of one interactive session. Scripted one command at a time, it cannot see a badge paired to the second adapter. Adapters are identified by **address**; `hciN` numbering is not stable across boots with identical dongles and is looked up fresh each time it is needed.

**COORDINATOR** (main thread, every `SDK_DETECT_INTERVAL`, default 2 s) - cheap, and never blocks:

1. **Pre-flight survey** (see below) whenever anything in BlueZ's object tree changes - a pairing, a removal, an adapter plugged in - and every 30 s regardless.
2. Ask which adapter each paired badge is connected on: a D-Bus property read per pairing, nothing on the radio.
3. **Assign** badges to adapters, one per adapter, never two on one.
4. **Enforce** the assignment (remove a second pairing, evict a badge from an adapter another badge owns).
5. Keep one **supervisor** thread per assigned badge.

**SUPERVISOR** (one thread per badge): on a **new link, however it was established**, poll `pactl list cards short` until `bluez_card.<MAC>` appears, set the card to `headset-head-unit` so the HFP source and sink exist, wait for the sink, then spawn `listener.py` for that badge - as the invoking user, in the `input` group, with the user's session environment plus `BADGE_MAC`, `BADGE_ADAPTER` and `BADGE_HCI`, the adapter taken from the **live link** at that moment rather than from any record of where the badge was paired. Restart the listener if it exits; stop it when its badge goes away. Its own thread, so one badge's slow path (a 30 s card timeout) never holds up another's.

**PAGE loop** (one background thread): `Device1.Connect` on each assigned badge that is absent, **through its own adapter only**, one at a time.

### Pre-flight

Runs before any paging, and assumes nothing about who set things up - the user may have paired, trusted or connected by hand, correctly or not:

| found | action |
|---|---|
| adapter powered off | power it on; if it will not (rfkill), log it as an adapter fault |
| badge paired, not trusted | `trust` it - without trust BlueZ refuses a badge that reconnects by itself, about a quarter of all reconnects |
| badge already connected at startup | adopt it; no page |
| badge paired on **two adapters** | once it connects, remove its pairing on the other adapter. Not before: until then there is no telling which one is the extra |
| **two badges paired on one adapter** (the likeliest mistake when pairing by hand - `bluetoothctl` pairs on the default adapter) | the connected one keeps it, otherwise the lower MAC; the other is left unconnected and logged |
| more paired badges than adapters | one per adapter; the rest are left unconnected and logged |
| a combadge seen but not paired | claimed, if an adapter is free and onboarding is enabled - see *Claiming* below |

**Two badges never share an adapter.** It is not a preference: two badges on one adapter produced audio chaos and Bluetooth stack crashes (TOS, 2026-09-13). A badge that comes up on an adapter another badge owns - it is paired there, so it may connect by itself - is disconnected, every time.

To keep a badge out, unpair it from this host. The pairings *are* the set; there is no allow-list.

### Claiming: a new badge pairs itself

**Nobody pairs a badge by hand.** Switch on a badge that has never been paired here and, if an adapter has no badge of its own, the transceiver finds it, pairs it, trusts it and connects it on that adapter; its listener starts; and the server, finding it unnamed, asks its wearer to name it (§13). From the box to talking, the only steps are *power on* and *say a name*.

- **How:** a claim thread scans each free adapter for 20 s, then waits 30 s. A TNG COMBADGE paired to **no** adapter here is claimed through **one interactive `bluetoothctl` session** driven under a pty: `select` the adapter, `scan on`, `pair`, `trust`, `connect` - `select` only lasts for the session it was typed in, which is why one-off commands cannot do this. A `NoInputNoOutput` agent makes pairing silent; if a badge ever did raise a prompt, the transceiver would answer it and log it loudly. The rest of the transceiver keeps using `busctl`.
- **Measured** (PAN, 2026-09-26): pair 5.4 s, trust 0.2 s, connect 0.5 s, no prompt; both badges claimed from a full wipe, one per adapter. A badge its host has unpaired goes **straight back to discoverable** - no button, no power cycle. A pairing can fail on the first try (it did once); the next window tries again, and the log gives `bluetoothctl`'s own reason.
- **Only while an adapter is free.** With every adapter holding its badge, nothing is scanned.

### Disabling onboarding

Every badge found is claimed by default. To stop that - taking the transceiver somewhere other people's badges are about - say:

- *"computer, disable on boarding"* → *"Onboarding disabled."*
- *"computer, enable on boarding"* → *"Onboarding enabled."*

"On boarding" is two words in the phrase because "onboarding" is not in the vosk model's vocabulary.

The badges already claimed are unaffected. The setting is a file, `onboarding_disabled.flag` beside `transceiver.py`, so it **survives a power cut**: a transceiver that reboots mid-convention does not start claiming again. The server sends `b'D'` / `b'Y'` (onboarding disabled / enabled) down the speaking badge's downlink; that badge's listener writes or removes the file; the transceiver checks it before every scan. The server console has `onboarding on|off` too (every connected transceiver). It is not a security control - it only decides whether new badges are looked for. Verified 2026-09-26: with onboarding disabled, a free adapter and a discoverable badge were left alone for 75 s; enabled, it was claimed in the next window.

### Health: a failing adapter is announced on the badges that work

An appliance has no screen, so faults are spoken. The transceiver keeps one sentence in `health.txt` - empty when all is well - and every listener sends a new sentence up its downlink (`b'R'` + 2-byte length + text, the only thing a relay sends upstream there); the server speaks it on that badge. Reported: an adapter that will not power on, an adapter that has disappeared since startup, a badge with no adapter of its own:

> *"Warning. One Bluetooth adapter is not responding. Check the transceiver log over SSH."*

The other badges keep running. Recovery clears the file and says nothing. Verified by `rfkill`-blocking one adapter of two: the fault was spoken on the other badge within a second, and unblocking brought its badge back on its own.

### Why two loops

The obvious design is one loop that checks, then connects, then sleeps. **Do not write that.** It is what this file used to be, and it is slow for a reason that is invisible until you measure it.

A connect against a badge that is switched off or out of range does **not** fail fast. It blocks for the controller's **page timeout** - BlueZ's default is `0x2000` slots × 0.625 ms = **5.12 s** - before reporting failure. Checking whether a badge is connected, by contrast, is a D-Bus property read costing milliseconds.

Put both in one loop and the cheap operation is held hostage by the expensive one. That matters more than it sounds, because **a badge often connects itself**: powering it on makes it page the host it was last paired with. Measured in the reference TOS deployment over 8,951 retry cycles, **24% of all links were badge-initiated** - and for every one of those, the connect attempt was pointless while the badge sat unnoticed for a mean of 8 s behind a loop busy paging a badge that was already on the line.

Split them and detection costs whatever you set `SDK_DETECT_INTERVAL` to, while paging carries on in the background at its own pace.

| | Before (one loop) | After (split) |
|---|---|---|
| Badge connects itself | mean 8 s, worst 16 s | **≤ ~2 s** |
| Badge must be paged | mean 8 s | mean ~7.5 s |
| Wasted card-poll per failed retry | **15 s** | 0 |

With several badges the page loop stays **one thread, paging one badge at a time**: each absent badge costs ~5 s, and paging on one adapter while another badge's audio is live on the next is radio contention that has not been measured yet. The sweep **rotates which badge it tries first**, so a badge left switched off does not always burn its timeout before the one you just switched on is tried.

### Bugs worth learning from

All of these were live in this file. They are stated plainly because every one of them is easy to write again, and because most of them fail *silently* - the system does the wrong thing without ever reporting an error:

1. **Never poll for the side effect of an operation you did not confirm succeeded.** The old `connect_badge()` issued `bluetoothctl connect`, *ignored the result*, then polled up to 15 s for an audio card that cannot appear when the connect just failed. Every failed retry cost ~20 s of dead time on top of the 5 s page timeout. `page_badge()` now returns a checked boolean.
2. **Post-connection setup must not live inside the connect path.** A badge that connects itself never calls your connect function, so any HFP setup hidden in there is skipped - leaving roughly a quarter of links on A2DP, where the badge microphone does not exist. `ensure_audio_ready()` therefore runs for every link, however it was established.
3. **Identity is the MAC, not the name.** Every TNG COMBADGE reports the same device *name*, so the first name match is whichever badge the tool happened to print first, not the one that is switched on. Collect every badge and key everything by MAC.
4. **A privileged helper must be given the target user's session, and omitting it fails as silence.** This one cost the most time, so it is worth the detail. `transceiver.py` runs under `sudo`, which **strips `XDG_RUNTIME_DIR`**; `runuser` without `-l` does not create a login session, so it does not restore it. A `pactl` launched that way looks for a PipeWire socket in a directory that does not belong to the target user, finds nothing, and reports **no cards and no error**. From the caller's side that is indistinguishable from a badge with no audio card.

   Observed on PAN, 2026-08-08: a badge that `bluetoothctl info` showed as `Connected: yes`, `Bonded: yes`, `Trusted: yes`, `UUID: Handsfree`, battery 50% - a perfectly healthy badge - was being written off as unusable, repeatedly, because `pactl` was querying the wrong session. Every `pactl` and `systemctl --user` call now goes through `run_as_user()`, which attaches `XDG_RUNTIME_DIR=/run/user/<uid>`. If `pactl` works for you in a terminal but returns nothing from a root helper, this is why. `ensure_audio_ready()` now also says so explicitly when it sees *no cards at all*, rather than blaming the badge.

5. **"Connected" and "usable" are not the same thing.** BlueZ can hold an ACL link open to a badge whose audio profile never came up (`br-connection-profile-unavailable`): it reports `Connected: yes` while no `bluez_card.<MAC>` ever appears. Retrying that badge forever is a livelock. The fix is `quarantine()`: a badge that connects but yields no audio card is disconnected and stood down for 60 s - after the audio stack has been started and the badge retried once, because a dead session (bug 4) looks exactly like a dead badge from here, and the badge is the more expensive thing to discard wrongly.
6. **`bluetoothctl` acts on the default adapter.** `bluetoothctl list` shows them all, but `devices`, `info` and `connect` run as one-off commands all act on the default one; `select` lasts only for the session it was typed in. On PAN, with adapter B the default, `bluetoothctl devices Paired` listed only B's badge and `bluetoothctl info` on A's badge answered `Device … not available` (2026-09-26). The old transceiver would never have seen A's badge. Hence D-Bus, which names the adapter in every object path.
7. **With no speakers of its own, the host makes a badge the default audio device.** A stream whose target disappears is moved by PipeWire to the *default* sink or source. On a relay box with no sound card (PAN, a Pi), PipeWire elects the badges themselves: found on PAN with the default sink set to one badge's speaker and the default source to the *other* badge's mic. One badge's answer could then play out of the other, and one badge's recording could hear the other. `ensure_no_badge_default()` points the defaults at a null sink (`sdk_no_badge`) whenever a badge holds them, so a fallback lands nowhere. A device you chose yourself - laptop speakers, a USB mic - is left alone.

   The tempting fix is `node.dont-fallback` on every stream, which forbids the fallback outright. **Do not use it.** The HFP sink node is briefly re-created as the SCO link comes up; without the flag PipeWire re-links the stream and nobody notices, with it the stream dies. It truncated the first badge's greeting on both of the first two-badge runs, and in a direct test a 4 s tone survived 1 time in 6 with the flag and 6 in 6 without (`multiuser/xtalk_probe.py`).

### Battery - which battery

A natural worry is that a faster loop drains the badge. It does not, and the reason is worth internalising before tuning anything:

- **Paging costs the host, not the badge.** A connect transmits page trains from the *host* radio. The badge sits in page scan at a duty cycle fixed by its own firmware and cannot tell how often you page it.
- **What costs the badge is SCO** - bringing the audio link up, playing through its speaker, tearing it down. That is its highest-power activity by a wide margin. To save badge battery, look at how often you play audio to it, not at how often you poll.
- **What costs the host is `SDK_DETECT_INTERVAL`.** Each pass forks a few `busctl` subprocesses, one per pairing. At 2 s that is negligible; at 0 it is a busy loop that keeps a core warm permanently. On a battery-powered relay host (Pi, laptop) keep it ≥ 0.5. For genuinely instant detection at zero idle cost the answer is not a tighter poll but a D-Bus signal subscription on `org.bluez.Device1`'s `Connected` property.

**Why root?** `runuser` (drop privileges into the user's session for `pactl`/`pw-play`) requires root. `sg input -c …` (so `listener.py` can open `/dev/input/eventX`) likewise requires root unless the caller is already in `input`. Reading BlueZ over D-Bus does not; powering adapters, trusting badges and removing pairings does.

**Why a full `env=` dict for the child?** `runuser`/`sg` strip the environment. Without `HOME`, `USER`, `LOGNAME`, `PATH`, `XDG_RUNTIME_DIR`, and `DBUS_SESSION_BUS_ADDRESS` (`unix:path=$XDG_RUNTIME_DIR/bus`), `pw-play` exits 1 silently and you'll spend an evening wondering why the chirp never plays from the launcher even though it works from a terminal. Build the env explicitly from `SUDO_USER` and pass it to `Popen`.

**Startup sounds are handled by `listener.py`, not `transceiver.py`.** Playing audio from `transceiver.py` (which runs as root) is unreliable: `runuser`/`pw-play` from the root process lacks the user's PipeWire session, and `pw-play` alone cannot reliably establish a cold SCO output link anyway (see §7). All badge startup tones - badge-online and main-computer-online - are played by `listener.py` on startup via `play_wav_cold()`, which uses ffmpeg to trigger SCO negotiation from the capture side before playing audio.

**Stopping it.** Ctrl-C, or `SIGTERM` (systemd, `pkill`): every listener is stopped with it.

Run it as:
```bash
sudo SDK_SERVER_HOST=192.168.50.5 SDK_SERVER_PORT=1701 python3 transceiver.py
```

Tuning knobs (all optional; intervals shown at their defaults):
```bash
sudo SDK_DETECT_INTERVAL=2 SDK_PAGE_GAP=5 python3 transceiver.py
```
`SDK_DETECT_INTERVAL` is the reconnect latency you feel. `SDK_PAGE_GAP` is the wait *between* page sweeps - each attempt against an absent badge costs ~5 s of page timeout regardless, so the effective retry period is roughly `5 s × absent badges + SDK_PAGE_GAP`. `SDK_BADGE_MAC` was removed on 2026-09-26; if it is set, the transceiver says it is ignored.

See `transceiver.py` in this folder for the runnable minimal version.

## <a name="listener-py"></a>5. Minimal `listener.py` - Tap and Audio Handler

`listener.py` runs as the **logged-in user** (in the `input` group, courtesy of `transceiver.py`'s `sg input`). It does five things:

**Find the badge input node.** Iterate `evdev.list_devices()` and pick the one whose `name` contains `TNG COMBADGE`, or whose `EV_KEY` capability includes `KEY_PAUSECD` (201). Single-tap surfaces as that key, sometimes as code 200 - accept both. The node path can change if you re-pair, so look it up at startup and re-look-up after disconnect. **With several badges on one host, match `phys` too.** Every badge's node has the same name and an empty `uniq` (where the badge's own MAC would go); `phys` is the address of the *adapter* it came in on, which is the one thing that differs - and one badge per adapter is what makes that enough. Event numbers are no guide: the two badges on PAN swapped `event12` and `event13` across one power cycle. `BADGE_ADAPTER` carries the address; `btmon` is scoped the same way, `btmon -i <BADGE_HCI>`, or one badge's hang-up tap would end the other's recording.

**Detect a single tap.** A clean `select()` loop on `device.fd`, reading events; trigger when `event.type == EV_KEY and event.code in (200, 201) and event.value == 1`. Debounce ~2 s - after SCO teardown the badge can re-fire spuriously.

**End a recording with a second tap.** One tap starts a recording and the next tap ends it, the same rule as closing a channel. During SCO that second tap arrives as `AT+CHUP` on `btmon` (a double tap, `AT+BVRA=1`, does the same), so the listener starts `btmon` under a pty as soon as the capture is live - the same moment the link comes up, and therefore the moment taps stop being key events - and folds its fd into the streaming `select()`. A 0.3 s guard (`TAP_CANCEL_GUARD_S`) keeps the tap that started the cycle from ending it; starting `btmon` later instead, as this did until 2026-09-13, leaves a gap in which a tap is seen by nobody at all. On the tap it stops ffmpeg, half-closes the socket, and waits up to `TAP_FINALIZE_WAIT_S` (1.5 s) for the server's verdict. A verdict plays as usual, so a dictation ended by tap is still kept and confirmed; `b'f'` or no verdict plays the spoken `cancelled.wav`. The server finalizes on whatever audio it already has, so a command it recognised **still runs** - a tap abandons a recording, it does not recall a command. `btmon` needs the privileges to open the HCI monitor; without them the listener says so and taps simply do not end recordings.

**Hold the link between taps.** Most of the ~1.2 s between a tap and its chirp is the SCO link being rebuilt after the previous cycle tore it down. With `SCO_HOLD_MS` (5000) the listener keeps the cycle's capture — which is what holds the link — running for that long after the confirmation, ends the cycle silently — the answer it just played is the signal that it is over — and a tap inside the window (again `AT+CHUP`, since a held link emits no key events) reuses the live capture and skips the rebuild. Expiry tears down as before, so the badge's own chirp sounds at the end of the linger. The held capture is drained and watched: if it ends, the profile is switched off and the next tap takes the full path. Costs badge battery for the linger; needs a working `btmon`, without which no hold is taken. `0` restores the old behaviour. `INTERCOM.md` Phase 11.

**Start capture first, THEN play the chirp.** `ffmpeg -f pulse -i bluez_input.<MAC> -ar 16000 -ac 1 -f wav -loglevel quiet pipe:1`. Opening `bluez_input.<MAC>` triggers HFP SCO negotiation from the capture side - the only reliable way to bring the link up. ffmpeg writes a standard 44-byte WAV header as soon as it opens the source; receipt of those 44 bytes is the signal that the SCO link is live. Only then does `pw-play --target bluez_output.<MAC>.1 --media-role=communication listening.wav` play the chirp, which reliably routes to the badge speaker because the SCO link is already established. Playing the chirp before ffmpeg starts causes it to fall through to default output (laptop speakers). The `--media-role=communication` hint nudges PipeWire toward the HFP sink rather than treating it as music.

ffmpeg is not a stylistic choice - `parec`, `parecord --file-format=raw`, and `pw-record` all produce 0 bytes or hang on at least one of the supported adapter chips. ffmpeg is the only capture path that works everywhere we've tested.

**Stream and read back.** Open a TCP socket to the server, send the 18-byte handshake (`b'1'` + the 17-byte ASCII badge MAC - so the server can tell badges apart when more than one is on the air), then push the WAV stream straight from ffmpeg's stdout into the socket. The first 44 bytes are the standard WAV header - the server discards them and feeds raw PCM to Vosk. Use `select()` to multiplex the ffmpeg→socket forward with reading the response signal byte; the server closes the connection right after sending its byte, so always check the read side **before** writing more audio (otherwise you'll get `BrokenPipe` and miss the byte).

**Branch on the signal byte:**

| Byte   | Meaning              | Action                                                       |
|--------|----------------------|--------------------------------------------------------------|
| `b'c'` | command executed     | play `commandexecuted.wav` through the badge                 |
| `b'f'` | no match             | play `commandfailure.wav`                                    |
| `b'v'` | voice response       | read 4-byte big-endian size, then exactly N bytes of WAV; play |
| `b'V'` | voice, keep listening | as `b'v'`, but NOT terminal: play it, discard the mic while it plays (and 0.35 s after), then keep streaming - see §13 |
| `b'k'` | keepalive            | reset the recv timeout (and slide the recording deadline), keep waiting |
| `b'W'` | prewarm (downlink)   | bring SCO up now and hold it - a hail is about to arrive     |
| `b'G'` | named-badge greeting (downlink, first byte after the handshake) | skip `maincomputeronline.wav`; the `b'v'` that follows is *"&lt;first name&gt;, online."*, played at announcement level |
| `b'H'` | hail pending (downlink) | the next tap answers a hail - skip the `listening.wav` chirp for it |
| `b'E'` | hail ended (downlink) | the answer window closed unanswered - taps are commands again |
| `b'D'` / `b'Y'` | onboarding disabled / enabled (downlink) | write / remove `onboarding_disabled.flag` for the transceiver (§4) |
| `b'O'` | channel open         | this tap socket is now a live intercom (see `INTERCOM.md`); play `channelopen.wav` |
| `b'A'`+len+PCM | channel audio | peer audio frame (2-byte BE len); pipe into the stdin player |
| `b'X'` | channel closed       | play `channelclosed.wav`, tear down                          |

The badge-to-badge hail/channel system built on these (aliases, prewarm, hysteretic noise gate, half-duplex mute, close gestures) is documented in `INTERCOM.md`.

**Turn-taking in a live channel.** The channel carries one talker at a time. Pause for less than **0.5 s** (`CHANNEL_GATE_HOLD`) and you still have the floor: your next words go straight through. After a pause of 0.5 s the floor is released, and for the next **0.4 s** (`CHANNEL_FLOOR_RELEASE_MS`) neither badge transmits. That window lets the tail of your voice die away on the other badge, so it can't open that badge's gate. From 0.9 s on, whoever speaks first has the floor. Picking it up takes a little more voice than keeping it (gate open at 50, close below 16), so a whispered first syllable may be lost. In practice this is hard to notice: counting aloud at 0.5 s, 1 s and 1.5 s intervals comes through without drops on badges an arm's length apart (PAN, 2026-09-26). Both values are server environment variables (`SDK_CHANNEL_GATE_HOLD`, `SDK_CHANNEL_FLOOR_RELEASE_MS`); their tuning history and the same-room trade-offs are in `INTERCOM.md`.

The SDK uses `c`, `f`, `v`, `V`, `k`, the four channel bytes above, and the downlink's `H`/`E`/`D`/`Y`. One byte goes the other way on the downlink: `b'R'` + 2-byte length + UTF-8 text, a health report from the relay host, which the server speaks on that badge (§4). Other letters are free for your own extensions - a signal byte can trigger any relay-side behavior you like (the author's fuller system uses `l` for dictation-recorded and `p` for prompt-dispatched, for example).

`listener.py` expects these asset files in `assets/` (next to the scripts): `listening.wav` (chirp on tap), `commandexecuted.wav`, `commandfailure.wav`, `badge-to-transceiver-online.wav` (played on startup once the badge connects), `maincomputeronline.wav` (played once the server is reachable), `channelopen.wav` and `channelclosed.wav` (spoken when an intercom channel opens and closes). Any short WAV/MP3 clips work - record or synthesize your own and drop them in under these names.

**Generating the spoken assets - `tts.sh`.** For the spoken (as opposed to purely sound-effect) assets, `tts.sh` writes a WAV from a phrase using the *same* TTS engine `computer.py` speaks with - Windows SAPI (`System.Speech`, female voice, Rate 2) or `espeak-ng -v en-us -s 165` on Linux/macOS - so your startup and acknowledgement tones match the voice that answers commands. Unlike `speak`-style tools it only writes the file; it never plays audio. A bare filename lands next to the script; a path containing a slash is used as given:

```bash
./tts.sh "Access granted."               assets/access_granted.wav
./tts.sh "Main computer online."         assets/maincomputeronline.wav
./tts.sh "Badge to transceiver, online." assets/badge-to-transceiver-online.wav
./tts.sh "Command executed."             assets/commandexecuted.wav
./tts.sh "Command failure."              assets/commandfailure.wav
./tts.sh "Channel open."                 assets/channelopen.wav
./tts.sh "Channel closed."               assets/channelclosed.wav
```

**Persistent downlink.** On startup `listener.py` opens a second, long-lived connection to the server - handshake `b'h'` + MAC - and holds it open for the life of the session on a background thread. The server sends `b'k'` keepalives every ~5 s; 15 s of silence means the server is gone, and the relay reconnects every 5 s, playing `maincomputeronline.wav` on each successful (re)connect - so a server restart is audible on the badge with no tap. The downlink is also the server's **push path**: a `b'v'` frame arriving on it plays through the badge immediately via the cold-SCO sequence (`ensure_hfp_profile()` → `play_wav_cold()` → teardown), which is how badge-to-badge hails are delivered (see `INTERCOM.md`). An `audio_lock` serializes pushed audio against active tap cycles so a hail can never play over a live command. Taps are not accepted until the downlink's first connect.

**Chirp ordering matters: ffmpeg before pw-play.** The chirp plays *inside* `stream_and_handle_response()`, after ffmpeg has already started and the 44-byte WAV header has arrived. Opening `bluez_input.<MAC>` via ffmpeg triggers SCO negotiation from the capture side - the WAV header is confirmation that the HFP link is live. Playing the chirp before ffmpeg starts means it arrives before SCO is established and falls through to the default output device. The correct sequence:

```
1. Start ffmpeg → wait for 44-byte WAV header (SCO live)
2. play_silence(PRIME_MS_LISTENING)       ← prime output side
3. play_wav(LISTENING_WAV, prime=False)  ← chirp through badge
4. TCP connect, send header, stream PCM
```

`PRIME_MS_LISTENING` controls how long to prime the output side after the input side is confirmed live. It is **160 ms**, established by blind A/B on hardware — at 0 ms the chirp was heard as clipped in 4 of 4 trials.

⚠ **If you swap the chirp asset, re-derive this number.** What actually protects the chirp is the prime PLUS the silence your WAV begins with. The shipped `assets/listening.wav` opens with 90 ms of its own silence, so 160 + 90 gives 250 ms of protection. A chirp that starts at full level needs the whole 250 ms from the prime; one with a longer lead-in needs less. Measure your file's lead-in before copying anyone's value — including this one.

**Event-driven SCO teardown and re-establishment.** `subprocess.run(["pw-play", ...])` is synchronous - it blocks until the last audio frame has left the pipeline. Its return is the definitive "nothing is playing" event. Call `force_sco_teardown()` exactly at that moment:

```python
play_wav(ACK_WAV)         # blocks until playback complete
force_sco_teardown()      # instant, zero-timer - cannot be premature
```

```python
def force_sco_teardown():
    subprocess.run(["pactl", "set-card-profile", CARD, "off"], ...)
```

On the next tap, `ensure_hfp_profile()` re-establishes HFP by setting the profile back to `headset-head-unit` and polling `pactl list sinks short` every 0.5 s until the sink reappears - event-driven, not a fixed sleep:

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

**Debounce and evdev queue flush.** `listener.py` closes and reopens the evdev device after each tap cycle. This gives a fresh file descriptor with an empty event queue, discarding any spurious badge re-fires that SCO teardown can trigger. Because of this flush, `last_tap` does *not* need to be reset to `time.time()` after `stream_and_handle_response()` returns - resetting it there would impose an unnecessary 2 s blackout after audio ends. The debounce only fires while the device is held open (within a tap cycle), not across cycles.

See `listener.py` in this folder for the runnable minimal version.

## <a name="computer-py"></a>6. Minimal `computer.py` - Receive, Recognize, Respond

`computer.py` listens on a TCP port (1701 by default; pick anything) and serves each connection on its own thread - multiple badges never block each other. The Vosk `Model` is shared across threads; each connection builds its own `KaldiRecognizer`. A `b'h'`+MAC connection registers as that badge's **persistent downlink** (held open with `b'k'` keepalives; the server can push `b'v'` voice frames down it at any time). A background console thread reads stdin: `badges` lists every badge seen; `hail <mac> [text]` pushes synthesized speech to a badge's downlink - unsolicited playback on that badge, `<mac>` accepting any unique substring. For each `b'1'` tap connection:

1. **Read the 18-byte handshake** (`b'1'` + 17-byte ASCII MAC). The MAC keys the session; MAC-less legacy clients fall back to the sentinel `00:00:00:00:00:00`.
2. **Discard the 44-byte WAV header.** Vosk wants raw PCM, not WAV.
3. **Recognize.** Build a `KaldiRecognizer(model, 16000)`. Loop on `conn.recv(4096)`; for each chunk, call `AcceptWaveform(chunk)` then poll `PartialResult()` and `Result()`. Match against your command vocabulary on every poll - fire as soon as a match appears, don't wait for end-of-utterance. Cap the loop at ~10 s of wall time.
4. **On match: act.** A "command" can be anything - play a sound locally, run a shell command, hit an API. The minimal server prints the recognized text and picks a canned response.
5. **Acknowledge.** Send `b'c'` (matched), `b'f'` (no match within timeout), or `b'v'` followed by `<4-byte big-endian size><WAV bytes>` to deliver a voice response. (`b'V'`, same framing, speaks without ending the cycle; §13 uses it.)

`computer.py` uses the unconstrained small model and substring matches against a tiny phrase list - easiest to start with, fine for a few commands. If you grow to dozens of phrases, Vosk also accepts a constrained grammar (a JSON list of exact phrases passed to `KaldiRecognizer`), which keeps recognition dramatically tighter at the cost of only hearing what's in the list.

**Windows TTS via SAPI.** `computer.py` branches on `sys.platform == "win32"` and falls back to PowerShell `System.Speech.SpeechSynthesizer` - no extra dependencies on Windows:

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

On Linux/macOS it falls back to `espeak-ng`. Piper, when installed (§2, *TTS for responses*), takes precedence on every OS; no path needs network TTS.

**Windows Ctrl-C.** Winsock's `accept()` is not interruptible by Python's signal handler on Windows - Ctrl-C has no effect while the server is blocked in accept. Fix: set a 1 s timeout on the listening socket and catch `socket.timeout` in the accept loop:

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

**`pw-play` alone cannot reliably bring up a cold HFP output link.** Targeting the badge sink from `pw-play` on a cold link often falls through to the default output device - HFP input-side and output-side SCO are negotiated independently, and `pw-play` triggers only the output side, which is less reliable.

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

ffmpeg writes the 44-byte WAV header as soon as `bluez_input.<MAC>` opens successfully - that header arrival is the SCO confirmation signal. No fixed sleep; the wait is only as long as the link actually takes (typically < 1 s on a warm adapter).

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

`pactl set-card-profile CARD off` collapses the SCO link. Call it exactly when the last `pw-play` subprocess returns - `subprocess.run()` is synchronous, so its return is the definitive "nothing is playing" event:

```python
subprocess.run(["pw-play", ...])   # blocks until last audio frame leaves pipeline
subprocess.run(["pactl", "set-card-profile", CARD, "off"], ...)   # instant, non-premature
```

On the next tap, re-establish with `set-card-profile … headset-head-unit` and poll `pactl list sinks short` for the sink to appear - event-driven, not a fixed sleep. See §5 for the full `ensure_hfp_profile()` pattern.

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

**Single tap fires code 200 *or* 201.** `KEY_PAUSECD` is 201, but the badge alternates after audio activity - accept both.

**Double-tap is not a keypress.** It's emitted by the badge as `AT+BVRA=1` over the HFP control channel. Watch with `btmon` (running under a pty for line-buffered output) and pattern-match the line.

**During SCO, neither is a single tap.** Once the audio link is up the badge button is call control, and a single tap sends `AT+CHUP` (a hang-up) instead of a key event - `btmon` shows `21 ef 11 41 54 2b 43 48 55 50 0d 80  !..AT+CHUP..`. BlueZ has no call to hang up, so if nothing watches for it the badge just chirps and nothing happens. The listener watches for both commands and treats either as "end this": it ends a recording or closes a channel. Drain the pty fully on each pass - SCO traffic floods `btmon`, and a single read per loop lets the tap line drown in backlog.

**`pw-play` alone cannot establish a cold SCO output link.** On a cold HFP link, targeting the badge sink via `pw-play` often falls back to the default output. The only reliable cold-start path opens `ffmpeg -f pulse -i bluez_input.<MAC>` first - this triggers SCO negotiation from the capture side. The 44-byte WAV header emitted by ffmpeg when the source opens is the signal that the link is live; only then is `pw-play` reliable. See `play_wav_cold()` in §7.

**Two `pw-play` calls on a hot SCO link eat the start of the second clip.** If you play silence then audio as two separate `subprocess.run(["pw-play", ...])` calls while SCO is already active, PipeWire briefly re-releases the audio path between them. Re-acquiring it eats ~200 ms of the second clip. Use a single `pw-play` call (no prime) when SCO is already live.

**SCO teardown is event-driven, not timer-based.** `subprocess.run(["pw-play", ...])` blocks until the last audio frame leaves the pipeline - its return is the definitive "nothing is playing" event. Call `pactl set-card-profile CARD off` immediately after for instant, non-premature teardown. Timer-based teardown (sleep N seconds then tear down) is always either too early or too late. Re-establishment likewise should poll for sink state change, not sleep a fixed time.

**SCO teardown saves ~5 s of dead time but adds a wake-up cost.** After confirmation, `pactl set-card-profile bluez_card.<MAC> off` collapses the lingering SCO link. The next tap then has to re-establish HFP via `set-card-profile … headset-head-unit` and poll for the sink to reappear (up to ~5 s). Net win on most adapters; worth making a config switch if your hardware disagrees.

**CRLF in shell scripts.** If you edit `.sh` files on Windows and run them on Linux, the kernel will look for `/bin/bash\r` and tell you "required file not found". Fix: `sed 's/\r//' f.sh > f.sh.tmp && mv f.sh.tmp f.sh`. Note: `sed -i` is unsafe on Windows bash (MSYS2) - use the temp-file form.

**The SDK and the full TOS build share port 1701 AND the wire protocol - so a listener will happily talk to the wrong server.** `SDK_SERVER_PORT` defaults to 1701, which is also what TOS's `maincomputer.py` binds, and the two dialects are deliberately compatible (this SDK was distilled from that code). Point a listener at a host where the *other* server is running and nothing fails: the handshake succeeds, the command matches, a voice response comes back. The only symptom is that the answer arrives in the other system's voice, from the other system's vocabulary - which is easy to mistake for a TTS bug in the SDK. Observed 2026-08-23. If you run both, give one of them its own port (`SDK_SERVER_PORT=1702` on both `computer.sh` and `transceiver.sh`), or check the *server's* log to see which one actually answered.

**`Bluetooth: hciN: corrupted SCO packet` - reboot the relay host before you change anything.** A few a day is background noise on cheap adapters. **Hundreds a second is a fault**, and on the one occasion it was chased to ground (2026-08-23) the cause was **stale driver/stack state**, not configuration: a reboot cleared it with no config change surviving, and the same stale state later produced total silence - SCO reporting "established" while zero packets flowed. It survived a badge power-cycle, a relay restart, a config revert and disabling USB autosuspend. Only the reboot fixed it. Investigate *after* a reboot has failed to clear it, not before.

**Don't blame the codec, and check the live alt setting rather than the descriptors.** mSBC/wideband (`Air mode: Transparent (0x03)` in `btmon`) was verified working with zero corruption on a dongle whose `lsusb -v` shows **no alt setting 6** - its largest isoc packet is 49 bytes against a 60-byte mSBC frame. That looks impossible and isn't: `btusb_work()` falls back to alt 1 and reassembles across packets, and its own comment says so - *"Alt 1 appears to work for all adapters that do not have alt 6."* What the descriptors tell you is what the hardware *could* do; what you need is what it *is* doing:

```bash
cat /sys/bus/usb/devices/<dev>:1.1/bAlternateSetting   # read DURING a live SCO
```

A value the driver would never select for the negotiated codec means stale state.

**Two different SCO messages, one benign.** `corrupted SCO packet` comes from `btusb_isoc_complete()` in the USB driver - an isoc URB that couldn't be parsed. `SCO packet for unknown connection handle N` comes from `hci_scodata_packet()` in the core stack - a packet arriving for a handle already removed, i.e. packets in flight at teardown. A handful of the second around a disconnect is normal. Don't conflate them.

**Never restart PipeWire/WirePlumber with the badge connected.** Doing so left a badge silent in both directions while every layer above looked healthy: profile set, source present, `Mute: no`, volume 100%, SCO negotiating successfully - and pure digital silence in the captured stream (`max_volume: -91.0 dB`). Removing the config change that prompted the restart did not undo it. If you must change bluez5 settings, drop the file in and **reboot**.

**Reap your ffmpeg, don't just signal it.** `terminate()` returns immediately and the process can hold `bluez_input` for tens of milliseconds after SIGTERM. If the next tap opens the same source, or a playback starts on the same SCO link, you have two consumers on one link. Always SIGTERM, wait with a timeout, escalate to SIGKILL, and reap - `terminate_ffmpeg()` in `listener.py`. Watch the *early return* paths especially; the happy path is easy to get right and the error paths are where a bare `terminate()` hides.

**TCP framing is positional, not length-prefixed (mostly).** The 18-byte handshake (`b'1'` + 17-byte MAC) and the WAV stream are framed by position and the WAV header. The voice response is the only length-prefixed frame: `b'v'` then 4 BE bytes then exactly that many bytes. Don't `recv(4096)` for the size - read exactly 4, then loop on the body until you've received the full count.

## <a name="vibe-control"></a>10. Vibe Control - driving a terminal by voice

**Optional, Windows-only.** Full design in <VIBE.md>.

Vibe Control turns the badge into a remote control for a terminal session running on the **server** machine. Tap, say *"computer proceed"*, and an Enter lands in the window it is latched to. The reference target is a Claude Code session, so you can supervise a long-running interactive program from across the room - screen cast to a television, no keyboard in reach. It does not know or care what is running there; it sends Enter, Escape, Right Arrow, digits, and pasted text at whatever window the target rule matches.

```
computer activate vibe control     → "Vibe control latched to sunshine. Ready."
computer proceed                   → Enter
computer continue                  → paste the word "continue", then Enter (advance regardless)
computer carry on                  → Right Arrow, then Enter (accept the suggestion, submit)
computer cancel                    → Escape, twice
computer option one … nine         → 1 … 9
computer wake up                   → recover a blanked display
computer deactivate vibe control   → "Vibe control released."
(the latched window closes)        → "Vibe control target closed. Vibe control released."
```

Four things about it are worth knowing before you read <VIBE.md>:

- **It extends the vocabulary rather than replacing it.** While active, the phrases above are added to `COMMANDS` and everything already there keeps working - you can still ask the time without dropping the latch. (The full system does the opposite and suspends its normal vocabulary outright. That gate earns its keep there, where the vocabulary is large: narrowing it sharpens recognition and stops a stray command firing while you concentrate on a terminal. With a handful of phrases there is little to sharpen and little to fire by accident, so here it was only a restriction.) Hails are never suppressed either: an incoming call reaches the badge in any mode.
- **It refuses rather than guesses.** Exactly one target session may be running at activation; zero or several and it declines out loud, with no state left behind. Sessions are **counted** by process and **targeted** by window class + title, and the two counts must agree - a title rule that has silently stopped matching fails loudly instead of latching onto the wrong window.
- **The target's death ends the mode, and it never re-acquires one.** Close the latched window and Vibe Control releases: the vocabulary comes back and the badge says so out loud rather than chirping as though your keystroke landed. To drive a different session, activate again at it. This is worth stating because the obvious convenience - the bound window died, so rebind to whatever single session exists now - is a mistarget wearing a helpful face. A latch is consent to drive *one* session; a session that appears later is a different one you never pointed at. The SDK used to rebind lazily at send time and no longer does (2026-08-23); the full system had the same idea in a background loop, and it eventually armed the injector at a terminal opened twenty hours later for something else entirely.
- **Commands that act, chirp - they don't talk.** `computer.py` grew an `ACK` sentinel for this: return it from any command to acknowledge with the badge's short chirp instead of a synthesized sentence. A spoken reply after every keystroke would make the mode unusable, and it's the right answer for any command whose effect you can already see.

### Tuning

Vibe Control's settings are listed in full in <VIBE.md>; three of them are gaps
*inside* a fixed key pair, and they are the ones to know about here because they
are the first things to reach for if you retarget this at a TUI other than the
reference one:

| Variable | Default | Gap between |
|---|---|---|
| `SDK_VIBE_ESCAPE_GAP_MS` | `80` | the two Escapes of `computer cancel` |
| `SDK_VIBE_CONTINUE_GAP_MS` | `250` | Right Arrow and Enter (`computer carry on`) |
| `SDK_VIBE_PASTE_GAP_MS` | `250` | Ctrl+V and the Enter that submits it (`computer continue`) |

`python vibekeys.py` prints the values in force, which is quicker than reasoning
about whether the environment reached the process.

**Never set the paste gap to 0**, and be generous with the other two. It is
tempting to argue the gap away - `SendInput` delivers in input-queue order, so
an Enter cannot overtake a paste. Delivery order really is guaranteed and really
is not what decides this: what matters is whether the receiving TUI *processes*
the `\r` as a submit keypress. Ctrl+V arrives as a bracketed-paste burst
(`ESC[200~` … `ESC[201~`) through the terminal and, on Windows, ConPTY, and TUI
input layers coalesce stdin arriving within one tick into a single chunk - a
`\r` caught inside that chunk is absorbed into the pasted *text* as a literal
newline instead of being dispatched as a key. The text lands in the composer and
nothing is submitted. Because it turns on terminal and renderer scheduling it
fails **intermittently**, which is the expensive shape for a voice command: from
across the room a silent no-op is indistinguishable from the program still
thinking.

Lengthening these costs nothing but the milliseconds. A sleep inside a send is
normally a window for focus to move - trading a dropped keystroke for a
misdirected one, which is worse - but the injector re-reads the foreground
window after every gap and **aborts the rest of the sequence** if it moved,
rather than finishing the send somewhere it was never aimed.

On a non-Windows server the import fails, the phrases are absent, and everything else is unchanged.

## <a name="large-vocab"></a>11. Large-Vocabulary Dictation - `captain's log` and `computer transcribe`

**A core feature: the large model is required** (Captain, 2026-09-27). `computer.sh` passes it and will not start without it. `computer.py` still runs with the small model alone if started by hand, and dictation is then simply absent.

Everything up to this point matches a phrase you defined in advance. Dictation is the opposite problem: capturing a sentence nobody can enumerate ahead of time. That needs a different recognizer, and switching to it mid-utterance is the entire mechanism behind both features here.

```
computer.py <small-model> <large-model>

  captain's log <anything>       → timestamped line appended to captainslog.txt
  computer transcribe <anything> → pasted into the Vibe Control window, unsubmitted
  computer replay last log entry → speaks the most recent entry back
```

The first two are *triggers*; the third is an ordinary command (see below).

### Two models, one switch

The **small** model (~40 MB) runs on every tap. It loads in seconds and is accurate enough to pick a known phrase out of a handful of candidates, but it is poor at open dictation - a small vocabulary has to guess at words it does not really know. The **large** model (`vosk-model-en-us-0.22`, ~1.8 GB unpacked) transcribes arbitrary English well and is far too heavy to run on every tap.

So the server keeps both. `detect_trigger()` watches the small model's running hypothesis for a trigger phrase; when one appears, `handle_large_vocab_phase()` builds a fresh recognizer on the large model and **replays every buffered audio chunk into it**.

That replay is the part worth understanding. The trigger is only recognized *partway through* the sentence - by the time "captain's log" has been decoded, you are already several words into the entry, and that audio has been consumed by the small recognizer. `computer.py` was already buffering the raw PCM of each tap (the intercom needs it, to deliver a hail in the caller's actual voice), so the fix is to feed the buffer back: the large model transcribes the utterance from the tap, and nothing spoken before the switch is lost.

Capture then continues on the live socket until you stop talking.

### The large model is loaded at startup, never on demand

This is the one decision in the feature that is not a preference. Loading `vosk-model-en-us-0.22` takes tens of seconds and gigabytes of RAM, and a dictation begins **inside a live badge transaction** - the relay is already recording, and `listener.py` gives up `RECORD_MAX_S` (13 s) after the last byte from the server. Loading on first use would therefore blow the recording window every time, and the badge would fail the very command that triggered the load.

Paying it once at startup is what makes the switch feel instantaneous. The cost is a slower launch and the memory: `computer.py` measured **about 5 GB resident** with both models loaded (PAN, 2026-09-27), under 1 GB with the small model alone. That cost is accepted: captain's log is a core feature, and the saving is not worth the complexity of making it optional.

### Availability

| Trigger | Vibe Control inactive | Vibe Control latched | Where the text goes |
|---|---|---|---|
| `captain's log …` | ✅ | ✅ | appended to `captainslog.txt` |
| `computer transcribe …` | - | ✅ | pasted into the latched window |

`computer transcribe` needs a latch because it pastes into the window Vibe Control has latched onto - with no latch there is nowhere for the text to go. That is a real dependency, not a policy.

`captain's log` has no such dependency: it writes a file and touches no window, so it stays available in both states. (The full system suspends it during Vibe Control, as part of suspending its whole normal vocabulary - see §10 for why that trade doesn't carry over to a vocabulary this small.)

`computer transcribe` **does not press Enter.** The prompt lands in the composer and sits there until you read it and say `computer proceed`. That separation is the whole safety argument for letting a voice pipeline type into a terminal - recognition *will* occasionally mishear, and the review step is what makes that a nuisance rather than an incident. It is also why this works with no display: the terminal you are dictating into is the display.

### Playing a log back

`computer replay last log entry` speaks the most recent entry, re-rendering the
stored timestamp as spoken English rather than reading digits and brackets aloud:

```
[2026-08-01 14:30:00] captain's log, the away team has returned
   ↓
"Captain's log, August 1, 2026, 14 30. the away team has returned."
```

This one is an ordinary `COMMANDS` entry, not a trigger - a fixed phrase with a
spoken answer, which is what `COMMANDS` is for. Only the *recording* half needs
the large model. It is registered alongside the dictation feature anyway, so the
log commands appear and disappear together rather than offering playback of
entries this server has no way to record.

A stored entry keeps its `captain's log` prefix, and the announcement above
already opens with it, so the prefix is stripped before speaking - otherwise
every replay would say it twice.

### Watching it work

Both features stream the running hypothesis to the server console, word by word, as you speak:

```
[computer] [AA:BB:CC:DD:EE:FF] large-vocab trigger: captains_log
[computer] [AA:BB:CC:DD:EE:FF] > captain's log stardate forty seven six three four
[computer] [AA:BB:CC:DD:EE:FF] silence 1.5s - finalizing
[computer] [AA:BB:CC:DD:EE:FF] captain's log recorded (9 words)
```

Vosk *revises* its hypothesis as it goes, so a partial does not always extend the previous one. When it does not, the line breaks and the corrected hypothesis is reprinted whole rather than splicing a fragment into the middle of a word.

Note the privacy consequence: the full system routes log content to a separate display and keeps its server console discreet. This SDK has no second display, so the log appears on the server terminal. If that terminal is visible to other people, so is your log.

### Tuning

| Variable | Default | Meaning |
|---|---|---|
| `SDK_DICTATION_SILENCE_S` | `1.5` | end of a log entry - seconds without a new word |
| `SDK_PROMPT_SILENCE_S` | `6` | end of a transcribed prompt |
| `SDK_DICTATION_MAX_S` | `110` | hard cap on one dictation |
| `SDK_CAPTAINSLOG_FILE` | `captainslog.txt` | where log entries are appended |

The two silence gates differ because the two features are *spoken* differently, and both numbers are inherited from measured use rather than guessed. A log entry is composed before you start talking and delivered in one go, so a short gap means you are done. A prompt is thought out while speaking - you stop to consider the next clause, and 1.5 s would paste half a sentence into a terminal. The hard cap is the backstop for a room noisy enough to keep resetting the gate.

The silence timer does not start until the first word is recognized, so a pause between the tap and your first syllable never ends the capture - only a gap *between* words does.

### Adding your own

A trigger is a prefix with open speech behind it, which is why these cannot live in `COMMANDS` (a fixed phrase mapping to a fixed outcome). To add one: return a new type from `detect_trigger()`, and handle it at the bottom of `handle_large_vocab_phase()`. The capture loop itself is generic.

Two things to keep:

- **Every word of your trigger phrase must exist in the small model's vocabulary**, because the small model is what recognizes it. A phrase the small model cannot decode is unreachable by voice no matter how good the large model is.
- **Strip the trigger with `find()`, not a fixed slice.** The large model re-transcribes from the beginning and may render the trigger differently than the small model matched it. `strip_trigger()` does this, and falls back to returning the whole transcript rather than nothing - a stray leading word beats a silently truncated first sentence.

### Known gotchas

**The apostrophe.** Vosk renders the possessive inconsistently, and unlike the full system this server runs the small model *unconstrained* (no grammar), so the spelling cannot be forced. Both `captain's log` and `captains log` are accepted; a trigger of your own with a possessive in it should do the same.

**Keepalives are mandatory, not an optimization.** `listener.py` stops recording 13 s after the last byte from the server, and a dictation routinely runs longer. The capture loop sends `b'k'` every 4 s to slide that deadline. Remove it and every dictation truncates at 13 seconds.

**Triggers are matched as substrings, not anchored to the start.** The full system anchors them, because its recognizer is grammar-constrained and its text is therefore clean. This server runs the small model open, where a stray decoded syllable ahead of the trigger is ordinary - anchoring would drop real commands.

## <a name="game-mode"></a>12. Game Mode - the tap as a mouse click

**Optional, Windows-only.** `clicker.py`; imported defensively, absent everywhere else.

Every other command in this SDK treats the tap as *"I am about to say something."* Game Mode treats the tap as the thing itself: while it is on, a single tap is dispatched as a **mouse click at the pointer** and no speech is recognized at all. It was built for a game whose teleport commits on right-click-down - aim with the mouse, tap the badge, go.

```
computer activate game mode   → "Game mode engaged. Badge taps send right-click
                                 until game mode is stopped from the server console."
(tap)                         → right-click at the pointer. No chirps at all.
game off        (console)     → "Game mode released."
```

### It is a tap takeover, not a vocabulary takeover

Vibe Control changes *which phrases* are in force. Game Mode never gets that far: `handle_connection()` checks it immediately after the answer-tap check, sends `b'c'` and clicks, and returns - before a recognizer is built. So "which commands are active during Game Mode" is a question that is never asked, because nothing is listened to.

Answering with the signal byte at once is deliberate. `listener.py` streams audio until it reads one, so replying immediately collapses the tap cycle instead of holding the microphone open for the full timeout. A trigger that goes deaf for ten seconds after each use is not a trigger.

The check sits **after** the pending-hail answer, on purpose: a hail has someone waiting at the other end, and playing a game does not make an unanswered hail the right outcome.

### Both chirps are removed, and that is a latency decision

Two sounds normally bracket a tap. Game Mode drops both, because **both block the audio pipeline** - `play_wav()` runs `pw-play` to completion before anything else happens:

| Chirp | Normally | In Game Mode |
|---|---|---|
| `listening.wav`, on tap | plays at step 4 of the tap cycle | **suppressed** - "listening" is a promise the mode does not keep, since nothing is listened to |
| `commandexecuted.wav`, on `b'c'` | plays **to completion before** `force_sco_teardown()` | **suppressed** - the server sends `b'g'` instead, which falls straight through to the teardown |

The badge's **own hardware chirp as the SCO link drops** becomes the feedback, which is why the teardown wants to be as early as possible rather than queued behind a WAV.

The listening chirp is the interesting half. It plays *before the socket to the server exists*, so no reply could ever suppress it - by the time one arrived the sound would already have been made. So the mode is **pushed** down the persistent downlink instead:

| Byte | Channel | Meaning |
|---|---|---|
| `b'M'` / `b'N'` | downlink | Game Mode on / off. Sent on every change, and again whenever a downlink registers, so a relay that restarted re-syncs at once |
| `b'g'` | tap socket | the click is done - terminal and silent |

`b'M'`/`b'N'` rather than `b'G'`, because the full TOS relay dialect already spends `b'G'` on its authorized-greeting marker and the two are kept in parity. An older relay logs one "unknown byte" line and carries on chirping: degrades to the old behaviour, never to silence.

### The way out is the console, and it has to be

While Game Mode is on, a tap never becomes speech - so *no spoken phrase can end it*, including the obvious one. There is deliberately no `computer deactivate game mode` command; a phrase that could never reach the dispatcher would be a lie in the vocabulary listing.

`activate()` therefore prints a banner to the server console saying exactly how to stop it, and `game off` there does. This is the same problem the full TOS build solves with a separate visible holder window, and the SDK gets a cheaper answer because its badge dispatcher and its console are **the same process** - the server is already a window you can see and type into.

### The mode cannot be stranded

State is in-process, as it is for Vibe Control, and here that buys something extra: kill the server by any means - Ctrl-C, a crash, a closed window - and the armed flag dies with it. There is no such thing as a Game Mode that outlived its server, so there is nothing to detect and nothing to clean up. Running `python clicker.py` on its own is inert for the same reason: a fresh interpreter starts disarmed.

(The full build parks the mode in a flag file, because *its* badge dispatcher and console are different processes, and pays the price in a liveness check. `clicker/CLICKER.md` has that side of the story.)

### The click is not aimed at a window

`clicker.py` does no window targeting at all. Windows routes a button-down to the window under the **cursor**, so "click the game" and "click where the pointer is" are the same instruction - and re-pointing the cursor to satisfy a window check would move the very thing being aimed. This is the deliberate opposite of `vibekeys.py`, which verifies focus and aborts rather than redirect; that guardrail exists because a stray Enter *submits* something, and it does not transfer to a click.

**So state the cost plainly:** while Game Mode is on, every tap right-clicks whatever the pointer is over. Stop the mode before leaving the pointer somewhere a right-click would matter. What contains it is the mode itself - explicit arming, a loud banner, and death with the server.

### Tuning

| Variable | Default | Meaning |
|---|---|---|
| `SDK_GAME_BUTTON` | `right` | `right`, `left` or `middle` |
| `SDK_GAME_HOLD_MS` | `20` | how long the button is held down |

`SDK_GAME_HOLD_MS` is not cosmetic. A game that polls button state once per frame can miss a press that goes down and up inside the same frame; 20 ms clears 60fps with room to spare and is invisible next to the ~1 s badge dispatch in front of it.

**Rate:** roughly one click per tap cycle - about a second, governed by the badge audio link coming up and down, not by anything in `clicker.py`. Right for a transporter, wrong for a fire button.

## <a name="onboarding"></a>13. Naming a New Badge by Voice

A badge with no line in `aliases.conf` is **new**, and the server asks its wearer to name it, on the badge, the moment it connects. No file editing, no console. A fresh SDK has no `aliases.conf` at all (it is not shipped, and git ignores it): every badge starts out new, and naming the first one creates the file, with a header explaining its format:

```
badge (pushed, no tap)  "New badge detected. Tap, then state this badge's name."
you   (one tap)         "chief engineer"
badge                   "Chief engineer. Confirm?"
you                     "yes"
badge                   "Additional identity?"
you                     "engineering"
badge                   "Engineering. Confirm?"
you                     "yes"
badge                   "Additional identity?"
you                     "done"
badge                   "Identity established: chief engineer, engineering, on-line."
```

`aliases.conf` gains `MAC = chief engineer, engineering` (with a dated comment above it), and the badge can hail and be hailed at once: the file is re-read on every tap, so there is no restart. The first name is the badge's spoken name; the rest are further aliases (a role, a location), in any order. The closing line reads **every** name back, so you hear that they were all saved. That is the only time the whole list is spoken: a badge that reconnects later is not re-introduced by name at all in the SDK, and in TOS, whose reconnect greeting names the badge, it says one name (`{callsign}, on-line.`).

**One tap for the whole dialogue.** After the first tap you just answer. Each question goes out as `b'V'`, the non-terminal voice frame: the listener plays it, throws away what its own microphone hears while the question plays and for 0.35 s after (the badge would otherwise hear itself), and keeps streaming. Only the closing line is an ordinary `b'v'`, which ends the cycle.

**Pausing.** Say nothing for 12 s at any question, or tap to end the recording, and the badge says *"Onboarding paused. Tap to continue."* Progress is kept: the next tap picks up at the same question. An answer spoken just before such a tap still counts.

**What it refuses**, and asks again for:

- a name another badge already has;
- a name containing **"to"** (it would break the hail grammar, *"\<self\> to \<target\>"*) or **"computer"** (which starts every command);
- more than three words;
- nothing heard.

"no" at a *Confirm?* discards that name and asks again. At *Additional identity?*, "done" (or "no", "none", "finished") ends it.

**Names are heard by the small model**, the same unconstrained recognizer that later matches hails. So any name it transcribes is by definition one it can hear again: a name outside its vocabulary comes back as some other word, and the readback lets you reject it before it is saved. (Large-vocabulary dictation would transcribe names the small model then cannot match.)

**To rename a badge**, delete its line from `aliases.conf` and reconnect it (switch it off and on): it is new again. While a badge is being named, its taps do nothing else. A server restart forgets an unfinished dialogue; the badge is asked again when it next connects.

Tuning, all in `computer.py`: `ONBOARD_ANSWER_WAIT_S` (12 s, how long a question waits for an answer), `ONBOARD_SILENCE_S` (1.2 s of silence ends an answer), `ONBOARD_MAX_WORDS` (3). The dialogue's logic is `_onboard_step()`, a pure function; `multiuser/test_onboarding.py` in the TOS tree drives it offline.

## 14.  More Info

This SDK is the distilled foundation of a much larger system - the Terran Operating System (TOS), the author's full starship-computer environment built on this same voice command pipeline (voice-print identity, command vocabularies, dictation, an AI main computer, and more). To see where this foundation can lead, visit https://tos.md.


## "Main computer offline." — announced

The server going away was printed to the console and otherwise silent, so a
badge that had stopped working sounded exactly like a badge nobody had tapped.
`listener.py` now plays `assets/maincomputeroffline.wav` on the **up->down
edge** of the downlink -- the mirror of the `maincomputeronline.wav` announce
it already made on connect.

Once per transition, gated on `downlink_up` having actually been set, so it
never fires on the startup retries before the first successful connect and
never repeats every `DOWNLINK_RETRY_S` while the server stays down.

A bundled asset rather than TTS for a structural reason: synthesis needs the
server, which is by definition what just disappeared.

⚠ **The SDK's copy is NOT the one the full TOS tree ships.** `audio/` and
relay-mobile carry the Majel voice; the SDK is meant to be voice-neutral, so
`assets/maincomputeroffline.wav` was regenerated 2026-09-06 with `tts.sh`, the
same platform TTS `computer.py::synth_wav()` uses at runtime. Same filename,
deliberately different audio. Regenerate it with:

```bash
./tts.sh "Main computer offline." assets/maincomputeroffline.wav
```
