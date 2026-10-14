# A.R.T.H.U.R. — setup

> **Deletion rule:** Arthur cannot delete anything, and voice can never approve a deletion. Any deletion must be
> approved by the owner at the laptop.

HUD: http://localhost:8730 · morning run: daily 08:30 (fires on wake if the Mac was asleep)

```
cd ~/arthur
.venv/bin/python -m arthur doctor        # status of every channel
.venv/bin/python -m arthur run           # scan + analyse now
.venv/bin/python -m arthur morning       # full 08:30 routine (open HUD, scan, speak)
.venv/bin/python -m arthur open          # just open the HUD
.venv/bin/python -m arthur uninstall     # remove the launchd jobs
```

HUD keys: `Space` = brief me / stop, `R` = rescan. Tick a priority to mark it done (resets daily).

## Linking channels

### 1. WhatsApp — allow the background job to read it (one-time)
Reads the WhatsApp Desktop app's local database, read-only. Works from Terminal already;
the 08:30 job runs in the background and macOS blocks it until allowed.

System Settings → Privacy & Security → **Full Disk Access** → `+` → press `⌘⇧G` and paste:

    ~/.local/share/uv/python/cpython-3.12.<x>-macos-aarch64-none/bin/python3.12   (the python your .venv points at)

Toggle it on, then: `launchctl kickstart -k gui/$(id -u)/com.arthur.server`

### 2. Gmail + Google Calendar
Create an OAuth client (Desktop app) in your own Google Cloud project and save it as `secrets/google_client.json`. Read-only scopes.
In Google Cloud console for that project, enable **Gmail API** and **Google Calendar API**, then:

    .venv/bin/python -m arthur auth google work        # your work Google account
    .venv/bin/python -m arthur auth google personal    # personal gmail

A browser window opens for consent. Add as many accounts as you like, one label each.

### 3. Slack
1. https://api.slack.com/apps → Create New App → From scratch → pick the workspace
2. OAuth & Permissions → **User Token Scopes**: `search:read`, `users:read`
3. Install to Workspace → copy the **User OAuth Token** (`xoxp-…`)
4. Add to `config.json`:
   `"slack": {"workspaces": [{"name": "the firm", "token": "xoxp-…"}]}`

Repeat per workspace.

## Tuning (config.json)
- `vips`: names/emails that always surface
- `about_me`: context the triage uses to judge importance
- `voice`: any `say -v '?'` voice (default Daniel, British)
- `lookback_hours` / `monday_lookback_hours`: 24 / 72 (Monday covers the weekend)
- `whatsapp.skip_muted_groups`: muted groups are ignored unless they have unread messages

## How it works
`collectors/*` → `pipeline.py` (parallel, 90s cap per run) → `triage.py` runs `claude -p` with
your settings/MCP/tools stripped (≈1k context, structured JSON output) → `data/briefing.json` →
HUD. If Claude fails, a rule-based briefing is used instead. Logs: `logs/arthur.log`.
Secrets live in `secrets/` (chmod 700), nothing leaves the machine except the triage call to Claude
and the API calls to each service.

## Voice: "Hey Arthur"
Always-on listener (`com.arthur.ears`), fully offline: Vosk spots "hey arthur", faster-whisper (base.en)
transcribes, Claude (haiku) picks the action. Try: "what's on tomorrow", "who's waiting on me", "check again",
"read me the briefing", "mark Happy done", "reply to <name> on Slack saying …" (read back; only sent after you say yes),
"stop". Logs: `logs/com.arthur.ears.err.log`. Mic + Full Disk Access granted to the uv python3.12.

## Filters (config.json)
- `people`: pronoun/context notes, e.g. {"Pepper": "woman (she/her)"}
- `mute`: words that drop matching senders/subjects before triage (e.g. "asana")
- Slack: only DMs and direct @tags; anything you've already replied to (DM or thread) is dropped.
- Ticking a priority on the HUD hides its messages from briefings for 21 days.

## Tests
`zsh tests/run_all.sh` — sends nothing. Brain (47 commands), server (21 endpoints/refusals), HUD (browser), ears
(wake word with 10 synthetic voices + look-alikes, transcription, yes/no), flow (wake → answer with injected audio,
Gmail sent/draft counts checked before/after). Results in tests/results_*.json.
Mic: prefers the built-in mic (config `mic`) — a Bluetooth headset mic held open drops headphones to call quality.

## Push-to-talk + wake-word training
- Hold **right ⌥** and speak; release to send. No wake word needed (good in noisy rooms). Needs Input Monitoring
  for the uv python3.12 (granted 11 Oct). pynput still logs "not trusted" — harmless, it works.
- `.venv/bin/python -m arthur train` (in Terminal, ~3 min): records 20× "Hey Arthur" + 6 look-alikes, saves learned
  variants to config `wake_vosk_extra` / `wake_whisper_extra`. Listener pauses while it runs (data/ears_paused).
- Training part 2 (reading ~25 sentences of your names/commands): learns `corrections` (misheard non-English words →
  the right name, applied to every transcript) and compares whisper base vs small on your voice (`whisper_model`).

## Hearing upgrade (11 Oct)
- Commands transcribed by **whisper large-v3-turbo on the GPU** (models/whisper-turbo, MLX). Loads when you say
  "Hey Arthur" (overlaps with you talking), freed after 10 idle minutes (~1.6 GB while loaded). Falls back to the
  small CPU model if it fails. Config `stt_model`: "turbo" | "base". All MLX calls run on one thread (MLX requirement).
  Measured (synthetic voices + real recorded car noise): WER quiet 9%→5%, car 9%→4%, 2× car 13%→5%; ~1.2 s vs 0.5 s.
- **Silero VAD** decides when you've stopped talking (~1 s after you stop; never mid-sentence, works in car noise).
- **DeepFilterNet** (bin/deep-filter) is installed but **off** (`denoise`): it made whisper worse in every test.
- **Streaming**: Claude's answer is spoken sentence by sentence as it's written. ~4 s from end of command to first
  words (Claude's own start-up is ~3 s of that; "One moment, sir" covers it).
- Re-test speech models anywhere: `PYTHONPATH=.:tests .venv/bin/python tests/test_stt.py <noise.wav>`

## Training findings (11 Oct, the owner's voice)
- Wake rule is now **Vosk "hey arthur" or "hey author"** (live in his room Vosk hears his Arthur as "author"; 18/19 of his recordings; only false wake = literal "hey author") (`wake_verify: false`). On the owner's recordings: 15/19 caught, 0/5 of his
  look-alikes, 0/60 synthetic look-alikes. The old whisper double-check caught only 3/19 — whisper (small) hears his
  "Arthur" as "otto". `wake_verify: true` restores the two-stage mode.
- Silence-gate pre-roll: when speech starts, the last ~0.5 s is replayed into Vosk (the gate used to clip "hey").
- Commands: turbo 14% vs small 18% WER on his reading (with learned corrections) → turbo stays.
- Training records continuously and cuts phrases with Silero VAD (fixed-length windows clipped most samples).

## Is he on? — menu bar icon (com.arthur.menubar)
`A ◉` listening · `A ●))` hearing you · `A …` thinking · `A ♪` speaking · `A ⏸` paused · `A ✕` off.
Click it for: Open dashboard, Brief me, Stop talking, Rescan, Pause/Resume listening, Restart Arthur.

## Conversation mode
After every answer Arthur keeps listening ~7 s (10 s if he asked you something) — no wake word or hotkey — and
remembers the last 6 turns ("reply to the first one", "yes"). Ends on silence or "that's all / thanks / never mind".
Sending or running code still needs a read-back + your yes. Test: tests/test_conversation.py.
- Follow-ups: `ignore` (TV / not addressed to him → ends quietly), `hold` ("hold on" → waits 20 s), "say that again",
  soft Pop cue when the window opens, conversation resumes if you say "Hey Arthur" within 2 min.

## Claude usage in the menu bar
`A ◉ 47%` = 5-hour window used. Menu shows "Claude session: 47% used · resets <time>". Fed for free by Arthur's own Claude
calls (rate_limit_event in stream-json) and by ~/.claude/statusline-command.sh (writes data/usage.json; backup at
statusline-command.sh.bak). Same account as your Claude Code login, so coding sessions and Arthur share it.

## Nicknames (config `aliases`)
Saved-as-something-else contacts: "Natasha" → "Nat 🕷 (work)", "Wanda" → "Scarlet".
Matched by sound; a real contact that matches the spoken name more closely wins (e.g. "Nat" stays Nat).
Briefings use the real names via `people` notes.

## Sleep mode (power saving)
After 10 min without use the mic is closed (menu bar `A ○`). Wake: hold right ⌥ (and speak), or menu → "Listen for
10 minutes". Morning/evening briefings wake him too. Each use extends 10 min. `always_listen: true` disables sleep.

## New skills (12 Oct)
Memory ("remember that…", "forget…" = archived), reminders (Apple Reminders), timers (spoken when done), calls (via
iPhone), Apple Music control, new emails / iMessages to anyone, calendar create / move (never cancel), evening
wrap-up 19:30, voiceprint (ERes2Net, threshold 0.30) on wake / follow-ups / every approval; PTT bypasses it.
Done-means-done: ticked items return only if newer activity arrives; briefing told handled topics.
