#!/usr/bin/env bash
# Claude Code hook: forward the event JSON on stdin to agentd for the #activity feed.
# Sessions started by agentd are skipped (agentd streams those itself). Never fails the session.
[ -n "$AGENTD_THREAD_ID" ] && exit 0
curl -s -m 2 -X POST -H 'Content-Type: application/json' --data-binary @- \
  "http://127.0.0.1:${AGENTD_PORT:-8765}/hook" >/dev/null 2>&1
exit 0
