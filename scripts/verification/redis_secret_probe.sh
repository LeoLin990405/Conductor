#!/bin/sh
# Run inside the deployed Redis container. Print only stable booleans/check names.
set -eu

fail() {
    printf '{"status":"failed","check":"%s"}\n' "$1"
    exit 1
}

case "${REDIS_PASSWORD:-}" in
    ''|*[!0-9a-fA-F]*) fail secret_format ;;
esac

config=/run/redis-config/redis.conf
[ "$(stat -c '%a' "$config")" = 600 ] || fail config_mode
[ "$(stat -c '%u' "$config")" = "$(id -u redis)" ] || fail config_owner
[ "$(stat -c '%u:%a' /run/redis-config)" = 0:711 ] || fail config_directory
awk '$2 == "/run/redis-config" && $3 == "tmpfs" { found=1 } END { exit !found }' /proc/mounts || fail config_tmpfs
configured=false
while IFS= read -r line; do
    if [ "$line" = "requirepass $REDIS_PASSWORD" ]; then configured=true; fi
done < "$config"
[ "$configured" = true ] || fail requirepass_config

unauth=$(env -u REDISCLI_AUTH redis-cli --raw ping 2>&1 || true)
case "$unauth" in NOAUTH*) ;; *) fail unauthenticated_ping ;; esac
auth=$(REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --raw ping 2>&1) || fail authenticated_ping
[ "$auth" = PONG ] || fail authenticated_ping

for proc in /proc/[0-9]*/cmdline; do
    command=$(tr '\000' ' ' < "$proc" 2>/dev/null || true)
    case "$command" in *"$REDIS_PASSWORD"*) fail secret_in_process_argv ;; esac
done
uid=$(awk '/^Uid:/ { print $2 }' /proc/1/status)
[ "$uid" = "$(id -u redis)" ] || fail server_user

printf '%s\n' '{"status":"passed","config_mode_0600":true,"config_owned_by_redis":true,"config_on_tmpfs":true,"unauthenticated_ping_rejected":true,"authenticated_ping_succeeded":true,"password_absent_from_process_argv":true,"server_runs_as_redis_user":true}'
