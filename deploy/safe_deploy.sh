#!/usr/bin/env bash
# safe_deploy.sh -- install staged files only if the live files are exactly the version
# you expect; back up, install, compile, test, and restore automatically on any failure.
#
#   usage: safe_deploy.sh path/to/deploy.conf
#
# Exit codes: 0 deployed | 1 aborted before touching anything | 2 failed and restored
#             3 failed and the restore could not be verified (look at it by hand)
#
# Steps
#   1. wait (bounded) until the service is not running -- for services that stop on a schedule
#   2. check every live file's hash against EXPECT_OLD  (someone changed it since you planned?)
#   3. check every staged file's hash against EXPECT_NEW (did the right files get uploaded?)
#   4. back up the live files and verify the backup
#   5. install each file atomically (copy to a temp name in the same dir, then rename)
#   6. byte-compile the Python files
#   7. run the test command (with a timeout)
#   8. any failure in 5-7 -> put every file back from the backup and verify the hashes again
#
# Hashes are SHA-256 of the file with CR bytes removed (first 12 hex chars), so a CRLF copy
# from a Windows checkout hashes the same as its LF twin. Print one with:
#   tr -d '\r' < FILE | sha256sum | cut -c1-12
# A file that should not exist yet is written as name=none in EXPECT_OLD.
# File names must not contain spaces.
set -uo pipefail

CONF=${1:?usage: safe_deploy.sh deploy.conf}
# shellcheck source=/dev/null
source "$CONF" || { echo "cannot read $CONF" >&2; exit 1; }

: "${APP_DIR:?APP_DIR not set}" "${STAGE_DIR:?STAGE_DIR not set}" "${FILES:?FILES not set}"
: "${EXPECT_OLD:?EXPECT_OLD not set}" "${EXPECT_NEW:?EXPECT_NEW not set}"
SERVICE=${SERVICE:-}
SERVICE_STATE_CMD=${SERVICE_STATE_CMD:-"systemctl is-active"}
WAIT_MAX_S=${WAIT_MAX_S:-1800}
WAIT_STEP_S=${WAIT_STEP_S:-30}
PYTHON=${PYTHON:-python3}
TEST_CMD=${TEST_CMD:-}
TEST_TIMEOUT_S=${TEST_TIMEOUT_S:-600}
BACKUP_ROOT=${BACKUP_ROOT:-"$APP_DIR/.deploy_backups"}
LOG=${LOG:-"$APP_DIR/deploy.log"}
BACKUP_DIR="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)-$$"

log()   { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }
abort() { log "ABORTED (nothing changed): $*"; exit 1; }
hash_of() { tr -d '\r' < "$1" | sha256sum | cut -c1-12; }
expected() {            # expected "a.py=hash b.py=hash" a.py  ->  hash
    local pair
    for pair in $1; do
        if [ "${pair%%=*}" = "$2" ]; then echo "${pair#*=}"; return 0; fi
    done
    return 1
}
matches() {             # matches FILE EXPECTED_HASH_OR_none
    if [ "$2" = none ]; then [ ! -e "$1" ]; else [ -f "$1" ] && [ "$(hash_of "$1")" = "$2" ]; fi
}

# One deploy at a time (flock is on every systemd distro; skipped where missing).
if command -v flock >/dev/null 2>&1; then
    exec 9>"$APP_DIR/.deploy.lock" || abort "cannot open lock file"
    flock -n 9 || abort "another deploy is running"
fi

log "deploy start: $FILES"

# 1. wait for the service to be stopped
if [ -n "$SERVICE" ]; then
    waited=0
    while :; do
        state=""; read -r state _ < <($SERVICE_STATE_CMD "$SERVICE" 2>/dev/null)   # first word only
        case "$state" in
            active|activating|reloading|deactivating) ;;
            *) break ;;
        esac
        [ "$waited" -ge "$WAIT_MAX_S" ] && abort "$SERVICE still $state after ${WAIT_MAX_S}s"
        sleep "$WAIT_STEP_S"; waited=$((waited + WAIT_STEP_S))
    done
fi

# 2-3. live files are the version we planned against; staged files are the version we mean to install
for f in $FILES; do
    old=$(expected "$EXPECT_OLD" "$f") || abort "no EXPECT_OLD entry for $f"
    new=$(expected "$EXPECT_NEW" "$f") || abort "no EXPECT_NEW entry for $f"
    if ! matches "$APP_DIR/$f" "$old"; then
        have=none; [ -f "$APP_DIR/$f" ] && have=$(hash_of "$APP_DIR/$f")
        abort "live $f is $have, expected $old"
    fi
    [ -f "$STAGE_DIR/$f" ] || abort "staged $f is missing"
    have=$(hash_of "$STAGE_DIR/$f")
    [ "$have" = "$new" ] || abort "staged $f is $have, expected $new"
done

# 4. back up and verify the backup
mkdir -p "$BACKUP_DIR" || abort "cannot create $BACKUP_DIR"
for f in $FILES; do
    [ -e "$APP_DIR/$f" ] || continue
    mkdir -p "$BACKUP_DIR/$(dirname "$f")" && cp -p "$APP_DIR/$f" "$BACKUP_DIR/$f" \
        || abort "backup of $f failed"
    matches "$BACKUP_DIR/$f" "$(expected "$EXPECT_OLD" "$f")" || abort "backup of $f does not match"
done
log "backed up to $BACKUP_DIR"

restore() {
    log "FAILED: $1 -- restoring from $BACKUP_DIR"
    local f bad=0
    for f in $FILES; do
        if [ -e "$BACKUP_DIR/$f" ]; then
            cp -p "$BACKUP_DIR/$f" "$APP_DIR/$f.restore.$$" && mv -f "$APP_DIR/$f.restore.$$" "$APP_DIR/$f"
        else
            rm -f "$APP_DIR/$f"
        fi
        matches "$APP_DIR/$f" "$(expected "$EXPECT_OLD" "$f")" || { bad=1; log "  $f does NOT match its old hash"; }
    done
    if [ "$bad" -eq 0 ]; then
        log "RESTORED: every file matches its pre-deploy hash"; exit 2
    fi
    log "RESTORE INCOMPLETE: check $APP_DIR by hand (backup kept in $BACKUP_DIR)"; exit 3
}

# 5. install, one atomic rename per file
for f in $FILES; do
    mkdir -p "$APP_DIR/$(dirname "$f")" \
        && cp "$STAGE_DIR/$f" "$APP_DIR/$f.new.$$" \
        && mv -f "$APP_DIR/$f.new.$$" "$APP_DIR/$f" \
        || { rm -f "$APP_DIR/$f.new.$$"; restore "install of $f failed"; }
done

# 6. compile
py=()
for f in $FILES; do case "$f" in *.py) py+=("$APP_DIR/$f") ;; esac; done
if [ "${#py[@]}" -gt 0 ]; then
    "$PYTHON" -m py_compile "${py[@]}" >>"$LOG" 2>&1 || restore "byte-compile failed"
fi

# 7. tests
if [ -n "$TEST_CMD" ]; then
    runner=(bash -c "$TEST_CMD")
    command -v timeout >/dev/null 2>&1 && runner=(timeout "$TEST_TIMEOUT_S" "${runner[@]}")
    (cd "$APP_DIR" && "${runner[@]}") >>"$LOG" 2>&1 || restore "tests failed (see $LOG)"
fi

summary=""
for f in $FILES; do summary="$summary $f=$(hash_of "$APP_DIR/$f")"; done
log "DEPLOYED:$summary (backup in $BACKUP_DIR)"
exit 0
