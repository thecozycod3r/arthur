# Setup

## 1. Python and dependencies
```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt
```
Optional but recommended: copy a standalone Python into `runtime/python` and point `.venv` at it, so macOS
permissions are granted to Arthur's interpreter only (not one shared with other tools).

## 2. Models (all local, free)
| Model | Where | Used for |
|---|---|---|
| Kokoro v1.0 ONNX + voices | `models/kokoro-v1.0.onnx`, `models/voices-v1.0.bin` (github.com/thewh1teagle/kokoro-onnx releases) | Arthur's voice |
| Vosk small en-us 0.15 | `models/vosk-model-small-en-us-0.15/` (alphacephei.com/vosk/models) | wake word |
| whisper-large-v3-turbo (MLX) | `models/whisper-turbo/` (huggingface.co/mlx-community/whisper-large-v3-turbo) | commands |
| faster-whisper base.en | downloaded automatically into `models/whisper/` | fallback |
| ERes2Net speaker model | `models/speaker.onnx` (k2-fsa/sherpa-onnx `speaker-recongition-models` release) | voiceprint |

## 3. Accounts
- **Google** — create an OAuth desktop client, save it as `secrets/google_client.json`, enable the Gmail, Calendar
  and Drive APIs, then `python -m arthur auth google <label>`.
- **Slack** — a Slack app with user scopes `search:read users:read chat:write im:history im:read mpim:history
  mpim:read channels:history channels:read groups:history groups:read`; put the `xoxp-` token in `config.json`.
- **WhatsApp / iMessage** — read from the desktop apps' local databases (Full Disk Access); WhatsApp sends via
  Accessibility.
- **Claude** — the Claude Code CLI, logged in.

## 4. macOS permissions (for Arthur's python)
Full Disk Access · Accessibility · Input Monitoring · Microphone. Automation prompts (Reminders, Music, Messages)
appear on first use.

## 5. Run
`python -m arthur install` — server, listener, menu bar, morning (08:30) and evening (22:00) jobs.
`python -m arthur doctor` shows every channel's status. `python -m arthur train` tunes the wake word and names
to your voice.

## Config reference
See `config.example.json`. Notable keys: `voiceprint_threshold`, `active_minutes` (sleep timer), `always_listen`,
`aliases` (contacts saved under nicknames), `people` (pronouns / roles), `mute` (senders to ignore), `vocabulary`
(names the speech model should expect).
