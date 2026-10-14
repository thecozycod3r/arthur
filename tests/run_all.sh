#!/bin/zsh
# Arthur test suite — sends nothing. ~10 min. Run from ~/arthur:  zsh tests/run_all.sh
cd "${0:A:h}/.."
quiet='warn|HTTP Request|Processing audio|VAD filter|words count|wake rejected'
echo "== brain (47 spoken commands as text)"; .venv/bin/python tests/test_brain.py 2>&1 | grep -vE "$quiet" | tail -1
echo "== server (endpoints + refusal paths)"; .venv/bin/python tests/test_server.py 2>&1 | tail -1
echo "== HUD (browser)"; (cd ~ && node arthur/tests/test_hud.mjs /tmp/arthur_hud.png) | grep -E '"errors"|overflow' 
echo "== ears (wake word, transcription, yes/no)"; .venv/bin/python tests/test_ears.py 2>&1 | grep -vE "$quiet" | grep -E '"recall"|"false_alarms"|"mean_wer"|"passed"'
echo "== flow (after wake word, injected audio)"; .venv/bin/python tests/test_flow.py 2>&1 | grep -vE "$quiet" | tail -1
echo "== conversation (multi-turn)"; .venv/bin/python tests/test_conversation.py 2>&1 | grep -E "^PASS|^FAIL|^conversation"
echo "== skills (memory, reminders, timers, calls, music, email/iMessage, calendar, evening, voiceprint, done-rule)"; .venv/bin/python tests/test_skills.py 2>&1 | grep -E "^FAIL|^skills"
echo "== security (attacks, files, lock gate, stranger's yes, prompt injection)"; .venv/bin/python tests/test_security.py 2>&1 | grep -E "^FAIL|^security"
echo "== sleep mode"; .venv/bin/python tests/test_sleep.py 2>&1 | grep -E "^FAIL|^sleep"
echo "== resilience (broken sources, Claude down, concurrent rescans, timers across restart)"; .venv/bin/python tests/test_resilience.py 2>&1 | grep -E "^FAIL|^resilience"
echo "== dashboard edge cases (fresh install, server down, malicious text, huge day)"; (cd ~ && node arthur/tests/test_hud_edge.mjs) | python3 -c "import json,sys; d=json.load(sys.stdin); bad={k:v for k,v in d.items() if v['jsErrors'] or v['scriptRan']}; print('hud-edge:', f'{len(d)-len(bad)}/{len(d)} clean', bad or '')"
echo "== audit regressions"; .venv/bin/python tests/test_audit.py 2>&1 | grep -E "^FAIL|^audit"
echo "== typed commands (browser, approval cancelled on screen)"; (cd ~ && node arthur/tests/test_typed.mjs /tmp/arthur_typed.png) | python3 -c "import json,sys; d=json.load(sys.stdin); ok=d['questionAnswered'] and d['confirmCardShown'] and d['cancelledCardGone'] and not d['errors']; print('typed:', 'PASS' if ok else 'FAIL', '' if ok else d)"
