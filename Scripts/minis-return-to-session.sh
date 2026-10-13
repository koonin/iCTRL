#!/bin/sh
# One navigation RPC back to the task's originating chat, without sending a
# message, restarting automation or inspecting the chat's private content.
set -eu
return_url=${MINIS_RETURN_URL:-}
case "$return_url" in
    minis://sessions/*) session_id=${return_url#minis://sessions/} ;;
    *) printf '%s\n' 'iCTRL: originating Minis session link unavailable.' >&2; exit 2 ;;
esac
case "$session_id" in
    ''|*[!a-zA-Z0-9_-]*) printf '%s\n' 'iCTRL: invalid originating Minis session link.' >&2; exit 2 ;;
esac
exec sh "$HOME/.ictrl/bin/ictrl.sh" call open_url "{\"url\":\"$return_url\"}"
