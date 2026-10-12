#!/bin/sh
# Local-only Minis readiness check. Never pairs, retries UI actions or changes
# automation settings. A deliberate user stop must be respected by the caller.
set -eu
probe() { curl -fsS --connect-timeout 1 --max-time 2 http://127.0.0.1:8200/schema >/dev/null 2>&1; }
if probe; then
    printf '%s\n' '{"ready":true,"wake_requested":false}'
    exit 0
fi
if ! command -v apple-open >/dev/null 2>&1; then
    printf '%s\n' '{"ready":false,"error":"Runner unavailable; start iCTRL on the phone."}'
    exit 1
fi
# Keep the native opener's output: opened=true alone is not proof of readiness.
apple-open 'ictrl://start?callback=minis%3A%2F%2F' || exit 1
deadline=$(($(date +%s) + 15))
while [ "$(date +%s)" -lt "$deadline" ]; do
    if probe; then
        printf '%s\n' '{"ready":true,"wake_requested":true}'
        exit 0
    fi
    sleep 1
done
printf '%s\n' '{"ready":false,"error":"Runner did not become ready. Check unlock, UI automation consent, signing and developer disk image. Do not re-pair for this error."}'
exit 1
