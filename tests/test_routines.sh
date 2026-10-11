#!/bin/zsh
# Morning / evening routines with simulated lock events. Opens a dashboard window and speaks — run when that's OK.
cd "${0:A:h}/.."
T=$(cat secrets/api_token); ORIG=$([ -f data/screen_locked ] && echo locked || echo unlocked)
post(){ .venv/bin/python -c "from Foundation import NSDistributedNotificationCenter as C; C.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_('$1', None, None, True)"; sleep 1.5; }
speaking(){ curl -s -H "X-Arthur-Token: $T" localhost:8730/api/speaking | grep -q '"speaking":true'; }
for job in ${@:-morning evening}; do
  post com.apple.screenIsLocked
  M0=$(stat -f %m data/briefing.json); E0=$(stat -f %m data/evening.json 2>/dev/null || echo 0)
  (.venv/bin/python -m arthur $job > /tmp/arthur_$job.log 2>&1 &)
  for i in $(seq 1 100); do sleep 3; [ $(stat -f %m data/briefing.json) -gt $M0 ] && { [ $job = morning ] || [ $(stat -f %m data/evening.json 2>/dev/null || echo 0) -gt $E0 ]; } && break; done
  sleep 6; silent=$(speaking && echo no || echo yes)
  post com.apple.screenIsUnlocked
  spoke=no; for i in $(seq 1 40); do sleep 0.5; speaking && { spoke=yes; break; }; done
  curl -s -X POST -H "X-Arthur-Token: $T" localhost:8730/api/stop >/dev/null
  echo "$job: scanned while locked + stayed silent: $silent | spoke after unlock: $spoke"
done
[ "$ORIG" = locked ] && post com.apple.screenIsLocked
