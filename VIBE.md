# VIBE.md — Vibe Control: driving a terminal session by voice

*Component of TOS, subject to [LICENSE.md](LICENSE.md).*

> **Status:** Windows only. Optional — the server runs identically without it.

Vibe Control turns the badge into a remote control for a terminal session
running on the **server** machine. Tap the badge, say *"computer proceed"*, and
an Enter lands in the window it is latched to. It exists so you can supervise a
long-running interactive program — the reference target is Claude Code — from
across the room, with the screen cast to a television and no keyboard in reach.

Nothing here knows what is running in that window. It sends Enter, Escape, Right
Arrow, digits, and pasted text at whatever the target rule matches. Point it at
something else and it drives that instead.

## Contents

- [What it does](#what-it-does)
- [Files](#files)
- [The vocabulary](#the-vocabulary)
- [Modal takeover](#modal-takeover)
- [The single-instance gate](#the-single-instance-gate)
- [Detecting the candidates](#detecting-the-candidates)
- [Keystroke injection](#keystroke-injection)
- [Guardrails](#guardrails)
- [Configuration](#configuration)
- [Known gotchas](#known-gotchas)
- [Extending it](#extending-it)
- [Philosophy](#philosophy)

## What it does {#what-it-does}

```
  you (couch)                 relay host                    server (Windows)
  ───────────                 ──────────                    ────────────────
  badge tap ──HFP/BT──►  listener.py ──TCP 1701──►  computer.py
       "computer proceed"                                │
                                                   vibekeys.py
                                                   (SendInput)
                                                         │
                                                         ▼
                                             the latched terminal window
```

Keystrokes are injected **locally on the server**. Whatever you use to see that
screen from a distance — a cast, a KVM, a second monitor — is a display path
only and plays no part in the control path.

## Files {#files}

| File | Role |
|---|---|
| `vibewin.py` | Session detection and latch resolution. Counts processes, enumerates candidate windows, cross-checks the two, derives the spoken name. **Read-only** — never injects, never arms anything. Has a CLI: `python vibewin.py` prints a diagnostic report. |
| `vibekeys.py` | Key and clipboard injection via `ctypes` → `user32`. Also owns the mode state (armed / latched). `python vibekeys.py` prints a diagnostic; it cannot inject from the CLI (see below). |
| `computer.py` | `VIBE_COMMANDS` — the vocabulary — plus the one-line vocabulary swap in `active_commands()`. |

Both modules are pure stdlib, consistent with the rest of the SDK: the server
asks only for `vosk`, and this adds nothing to that.

**On a non-Windows server, `import vibekeys` raises `ImportError` and
`computer.py` catches it.** The Vibe Control phrases are simply absent from the
vocabulary and everything else behaves exactly as before. This is not a
degraded mode; it is the feature being cleanly optional.

## The vocabulary {#the-vocabulary}

Spoken in the normal vocabulary, to enter the mode:

| Phrase | Effect |
|---|---|
| `computer activate vibe control` | Run the gate, latch, and **speak what it latched onto** |
| `computer wake up` | Shift + a net-zero mouse nudge — recover a blanked display |

Spoken while the mode is active — **these are the only commands recognized**:

| Phrase | Key(s) |
|---|---|
| `computer proceed` | Enter |
| `computer continue` / `computer carry on` | Right Arrow, then Enter |
| `computer cancel` | Escape, twice |
| `computer option one` … `computer option nine` | `1` … `9` |
| `computer wake up` | as above |
| `computer deactivate vibe control` | leave the mode, speak the release |

`computer proceed` presses Enter, which in a selection UI activates the
highlighted entry — the first by default. That covers "just take the default"
with no special-casing, and it submits a composed prompt too.

**`computer continue` is the two-key one.** When Claude Code settles it offers a
recommended next action as ghost text in the composer: Right Arrow autocompletes
it, Enter submits it. Both keys go through **one** focus acquisition, so the
Enter cannot race a focus change and land somewhere the Right Arrow did not.
`computer carry on` is an alias for the same action.

**Number words, not digits.** Vosk's small model has no token for `1`. A phrase
containing a bare digit becomes unmatchable by voice — hence `computer option
one` through `computer option nine`. Nine is a generous ceiling; most prompts
offer three or four.

**Hails are not suppressed.** They are matched before the command vocabulary, so
an incoming call still reaches the badge whatever mode the desk is in. A comms
system that goes deaf in a submode is a broken comms system. This is deliberate
— don't "fix" the inconsistency.

## Modal takeover {#modal-takeover}

While Vibe Control is active, `COMMANDS` is **replaced**, not extended:

```python
def active_commands():
    if vibekeys is not None and vibekeys.is_active():
        return VIBE_COMMANDS
    return COMMANDS
```

That single branch is the most important structural choice in the feature, and
it buys two things at once:

- **Accuracy.** A handful of live phrases instead of your whole vocabulary. In
  a larger deployment this is the difference between reliable and infuriating.
- **Safety.** A mode that types into a terminal should be able to do almost
  nothing *else*. The takeover is a containment boundary, not just tidiness.

`computer wake up` is the one phrase deliberately present in **both**
dictionaries, so a blanked screen can be recovered whether or not the mode is
up. Duplication is correct there rather than a smell: with the vocabulary fully
swapped, a single copy in either dict would be unreachable from the other mode.

## The single-instance gate {#the-single-instance-gate}

**Exactly one target session may be running when Vibe Control activates.** If
zero or several are found, it **refuses and says so**.

| Found | Behavior |
|---|---|
| 1 | Latch to it, and speak its name |
| 0 | *"No session detected. Vibe control not activated."* |
| ≥2 | *"Multiple sessions detected. Vibe control not activated."* Every candidate is logged with HWND, title, and PID |
| counts disagree | *"Session detection inconsistent. Vibe control not activated."* |

Refusing beats guessing. The failure is loud, immediate, has **no side effects**
— nothing is armed, nothing is latched, the normal vocabulary stays live — and
it is resolved by closing a window and saying the phrase again. Nothing needs
cleaning up in between.

**Activation is an audible latch, not a silent bind.** It names the session it
latched onto, because the target is chosen automatically and that is the one
thing you cannot verify by looking at a badge. If the title carries no useful
name it says *"latched to an unnamed session"* rather than inventing one.

**Re-latch happens lazily.** If the bound window dies and exactly one candidate
now exists, the next command rebinds to it and logs `RE-LATCHED`. Zero or
several, and it declines and keeps waiting — the same refusal as at activation.
Because every injection routes through `target_hwnd()`, this needs no background
thread and no polling.

## Detecting the candidates {#detecting-the-candidates}

**Counting sessions and targeting one are different jobs and need different
signals.** This is the central lesson here, and it is easy to get wrong by
trying to do both with one mechanism.

| Job | Signal | Why |
|---|---|---|
| **Count** the sessions | process enumeration (`claude.exe`) | One process per session. Immune to window titles and retitling. Ground truth. |
| **Target** one session | window enumeration by class + title | Only a window can be focused and typed into. Necessarily heuristic. |
| **Validate** | compare the two counts | Catches a title rule that has silently stopped matching |

**The cross-check is the important part.** If the window rule matches one but
two processes are running, a second session exists that the title rule failed to
see — and latching onto the one it did see would be exactly the mistarget this
gate exists to prevent. So it refuses, loudly, with both lists logged. A
heuristic that can fail silently is replaced by one that fails loudly. Set
`SDK_VIBE_REQUIRE_AGREEMENT=false` to disable that, at your own risk.

**Findings from measuring this on a real desktop, which shaped the rule:**

1. **The window title is a usable *hint* and an unusable *identity*.** It may be
   user-set, auto-generated, plain ASCII, or in motion — Claude Code retitles a
   session as the conversation proceeds, and even an explicit rename can vanish
   on its own. Hence: count on processes, store the **HWND**, and match the
   title by **pattern**, never by equality.
2. **The busy and idle status glyphs are different, and assuming otherwise
   nearly shipped a fatal bug.** The animated Braille frames (U+2800–U+28FF) are
   the *busy spinner*; `✳` (U+2733) is the *idle* marker. A rule keyed on
   Braille reports **zero sessions whenever the target is idle** — which is
   precisely when you want to drive it. The shipped rule accepts any leading
   non-ASCII glyph, present or future.
3. **The title is not the working directory.** Never infer the session from cwd.
4. **Window-to-session mapping by PID is impossible** when the terminal hosts
   all its windows in one process, as Windows Terminal does. Counting the
   *session* process is a different question and works.
5. **A naive `"Claude"` substring match false-positives on a browser tab.** The
   class filter excludes it independently of the title, which is why the rule
   requires **both**.

**One session per window, in the active tab.** A terminal window titles itself
from its active tab, so a session in a background tab is neither counted nor
targetable — keystrokes go to the active tab anyway. A real constraint, and it
happens to reinforce the single-instance rule.

**Unicode discipline is mandatory.** Every path that logs or compares a title
must be UTF-8 safe end to end; writing a Braille glyph to a cp1252 console
throws.

## Keystroke injection {#keystroke-injection}

`vibekeys.py` — **stdlib only, `ctypes` → `user32`.** Adding a dependency for
five keystrokes and a clipboard write is not worth it, and these APIs have been
stable since Windows 2000.

| Purpose | Win32 call |
|---|---|
| Send keys | `SendInput` with `INPUT_KEYBOARD`, `KEYEVENTF_SCANCODE` (+ `KEYEVENTF_KEYUP`) |
| Extended keys | `KEYEVENTF_EXTENDEDKEY` — **required** for the arrow cluster |
| Key codes | `VK_RETURN`, `VK_ESCAPE`, `VK_RIGHT`, `VK_1`…`VK_9`, `VK_CONTROL`, `VK_V`, `VK_SHIFT` → scancode via `MapVirtualKeyW` |
| Wake nudge | `SendInput` with `INPUT_MOUSE`, `MOUSEEVENTF_MOVE`, relative `+1` then `−1` |
| Find window | `EnumWindows` + `GetWindowTextW` + `GetClassNameW` + `IsWindowVisible` |
| Focus | `SetForegroundWindow`, **verified** with `GetForegroundWindow`; `AttachThreadInput` + `ShowWindow(SW_RESTORE)` fallback |
| Clipboard | `OpenClipboard` / `EmptyClipboard` / `GlobalAlloc` / `SetClipboardData(CF_UNICODETEXT)` / `CloseClipboard` |

**Scancodes, not virtual keys** — console hosts and terminal emulators read
scancodes far more reliably than synthesized VK-only events.

`sizeof(INPUT)` must be **40** on x64. A wrong size makes `SendInput` fail
*silently* by returning 0, so `python vibekeys.py` prints it rather than
assuming it.

### Waking a blanked display {#wake}

`computer wake up` is **the one action exempt from both the armed check and the
focus check**, and it lives on its own code path so the exemption is visible
where it is granted rather than hidden behind a parameter. Each bypass is
forced:

- **No focus check.** It cannot pass one. The absence of a foreground window
  *is* the condition being recovered from.
- **No armed check.** A blank screen is exactly when you cannot see enough to
  activate anything, so the phrase must work outside the mode too.

**What makes that acceptable is the payload, and nothing else.** A bare
`VK_SHIFT` press/release types no character and changes no application state;
the mouse events are a relative `+1`/`−1` pair with no button flags, so the
pointer ends where it started. Wherever it lands, the outcome is the same:
nothing happens. **No other key has that property — do not extend the
exemption.**

Both input kinds are sent because the failure modes differ: a screensaver exits
on any input, while a display blanked by a power profile responds more reliably
to mouse movement.

This cannot defeat a **locking** screensaver, and should not: the secure desktop
is a boundary a user-mode process on the default desktop cannot cross.

## Guardrails {#guardrails}

Every one of these is load-bearing:

- **Nothing injects unless `activate()` has succeeded.** Inert until armed.
- **A standalone run of `vibekeys.py` can inject nothing**, because the state
  is in-process and a fresh interpreter starts disarmed and unlatched. Designs
  that keep this state in a file have to build that property deliberately;
  here it is free.
- **Only the fixed key set is reachable:** Enter, Escape, digits 1–9, Ctrl+V,
  and Right Arrow — the last **only** as the first half of the fixed continue
  pair, never as a standalone verb. There is no "send arbitrary keys" entry
  point, by design.
- **The target is re-validated before every send** (`IsWindow` + a *pattern*
  re-check of class and title, because an HWND can be recycled).
- **Focus is verified, never assumed.** `SetForegroundWindow` is silently
  refused for processes without recent user input, so the return value is not
  trustworthy on its own. If focus cannot be confirmed the send is **aborted,
  never redirected** — that is what stops a stray Enter from landing in another
  application.
- **One injection at a time.** Connection handlers run on their own threads;
  overlapping sends would fight over the foreground window.
- **Every injection is logged** to the server console with the target HWND. The
  keystroke record is auditable after the fact.

## Configuration {#configuration}

All environment variables, read at import, all optional:

| Variable | Default | Meaning |
|---|---|---|
| `SDK_VIBE_TARGET_PROCESS` | `claude.exe` | Session **count** ground truth |
| `SDK_VIBE_TARGET_CLASS` | `CASCADIA_HOSTING_WINDOW_CLASS` | Window class (Windows Terminal's host window) |
| `SDK_VIBE_TARGET_TITLE_RE` | `^([^\x00-\x7F]\s\|claude( code)?$)` | Title pattern — **targeting only**, never identity |
| `SDK_VIBE_REQUIRE_AGREEMENT` | `true` | Refuse when the window and process counts disagree |
| `SDK_VIBE_NAME_MAX_WORDS` | `6` | Cap on the spoken session name |
| `SDK_VIBE_ESCAPE_GAP_MS` | `80` | Gap between the two ESC presses |
| `SDK_VIBE_CONTINUE_GAP_MS` | `80` | Gap between Right Arrow and Enter |

**To drive something other than Claude Code**, set the first three. Run `python
vibewin.py` to see every window of your chosen class and which ones the title
pattern matches — that report is the tuning instrument.

## Known gotchas {#known-gotchas}

**The Right Arrow is an extended key, and getting it wrong is silent.**
`MapVirtualKeyW` returns the *same* scancode `0x4D` for `VK_RIGHT` and for
numeric-keypad 6. With `KEYEVENTF_SCANCODE` alone, the injected key **is**
numpad 6 — so with NumLock on it types the character `6` into the composer and
the following Enter submits it. `KEYEVENTF_EXTENDEDKEY` is mandatory for the
whole arrow cluster, Insert/Delete/Home/End/PgUp/PgDn, and right Ctrl/Alt.

**`computer cancel` must send ESC twice.** A single ESC does not clear Claude
Code's input box; a quick double-press does. Found in use, not by inspection.
The gap matters in *both* directions: too short and the pair risks being parsed
as an escape *sequence* (TUI input layers typically wait ~25–50 ms after ESC for
a CSI/Alt continuation); too long and it falls outside the application's
double-press window. 80 ms clears both with margin.

**A NULL `HWND` arrives as `None`, not `0`.** `GetForegroundWindow` is declared
with a pointer restype, and ctypes maps NULL to `None` — so `int(...)` on it
raises the moment Windows has no foreground window, which is a *documented*
condition, not an error (during activation changes, or whenever the interactive
desktop is not yours). `foreground_hwnd()` normalizes to `0`, a value that can
never equal a real HWND, so callers fall through to their existing failure path
instead of crashing. **Any ctypes function with a pointer restype has this
hazard** — `FindWindow`, `GetParent`, `GetWindow`, `GlobalLock`. Treat
`int(...)` on such a return as a bug unless NULL is impossible.

**A ctypes `HWND` is not an `int`.** `int()` on a `wintypes.HWND` tries to parse
the object's raw bytes and raises. `_window_info()` normalizes, because it is
called both with the plain int `EnumWindows` hands its callback and with the
ctypes form from `window_matches()`. This one shipped once and broke *every*
re-validation.

**A guard verified through the wrong branch is not verified.** The above
survived testing because the test hit an earlier `return` and never reached the
validation. When testing a refusal, confirm *which* check did the refusing.

**Focus stealing is real.** `SendInput` goes to the foreground window, so
injection must focus the target first — which is why `_focus()` verifies rather
than hopes. Relatedly, if you are tempted to add an always-on-top overlay for
this feature: a Tk window created with `overrideredirect(True)` and
`WS_EX_NOACTIVATE | WS_EX_TOPMOST | WS_EX_TOOLWINDOW`, shown via
`SW_SHOWNOACTIVATE`, **still took foreground** on Windows 11 — on the initial
show and on every subsequent update. Do not assume that flag is sufficient.

**Dictated prose is not code.** If you extend this with dictation (see below),
expect the recognizer to mangle identifiers, paths, punctuation, and camelCase.
Voice is for prose instructions — *"try the other approach"*, *"explain what you
just changed"*. Anything requiring exact syntax still wants a keyboard.

**Latency.** Each command costs a tap plus relay latency plus recognition —
comfortably fine for approve/choose/cancel, and nobody will want to drive a text
editor this way. It is not meant for that.

## Extending it {#extending-it}

**Dictation** is the obvious next step and the pieces are already here.
`paste_text()` stages text on the clipboard and Ctrl+V's it into the latched
window without pressing Enter; no shipped phrase calls it. To use it: switch to
a large Vosk model for the utterance, capture free-form speech, and hand the
result to `paste_text()`. **Do not make it press Enter** — see below.

**More keys** belong in `vibekeys.py` as named actions, never as a generic
"send this key" function. Scroll keys (`computer page up`) are harmless. Ctrl+C
to interrupt a running task is genuinely useful and genuinely destructive —
worth its own thought before adding.

**Chirp instead of speech** for anything you add that *does* rather than
*answers*: return `ACK` from the command. A spoken sentence after every
keystroke would make the mode unusable.

## Philosophy {#philosophy}

This is a system where one program's terminal may be driven by voice through
another. It is worth being explicit about why that stays consistent with
[LICENSE.md](LICENSE.md) rather than drifting toward the thing it exists to
resist.

Every keystroke originates in a spoken command, initiated by a physical badge
tap, on a screen you are looking at. There is no loop, no polling, and no
unattended operation: Vibe Control adds **reach**, not independence. The human
is not removed from the loop — the loop is extended across the room.

The deliberate choices that keep it that way: the mode is entered and left only
by voice; the injector is inert until armed; every injected key is logged; the
vocabulary takeover means the desk can do almost nothing else while it is up;
and when the target is ambiguous the system **refuses and hands the decision
back** rather than picking one.

**Be precise about which of those is the guarantee.** A display is an aid, not a
safeguard — no software can force a human to read a screen, and claiming
otherwise would be self-deception. If you add dictation, the invariant that
matters is the **separation of dictation from submission**: speaking a prompt
stages it, and a second, independent, deliberate act commits it. That gap is
where the human lives. Everything else is ergonomics.
