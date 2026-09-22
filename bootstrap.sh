#!/bin/bash
# MCAP viewer - macOS bootstrapper (self-healing).
#
# Creates an isolated runtime under ./runtime (or ~/Library/Application Support/
# MCAPViewer/runtime when the project folder is not writable), installs the
# pinned dependencies, validates the runtime, then launches the viewer.
#
# Usage:  ./bootstrap.sh [--check-only]
# Notes:  This file intentionally keeps ASCII-only comments so that older
#         bash 3.2 (macOS default) parses it identically everywhere.

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1

CHECK_ONLY=0
[ "${1:-}" = "--check-only" ] && CHECK_ONLY=1

LOG="$HERE/startup-error.log"
PIP_PIN="26.2.1"

say()  { printf '%s\n' "$*"; }
log()  { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG" 2>/dev/null || true; }

# ---------------------------------------------------------------- runtime root
probe_writable() {
    local d="$1"
    mkdir -p "$d" 2>/dev/null || return 1
    local f="$d/.write-probe-$$"
    ( : > "$f" ) 2>/dev/null || return 1
    rm -f "$f" 2>/dev/null
    return 0
}

if probe_writable "$HERE"; then
    RUNTIME_PARENT="$HERE"
else
    RUNTIME_PARENT="$HOME/Library/Application Support/MCAPViewer"
    mkdir -p "$RUNTIME_PARENT" || { say "cannot create $RUNTIME_PARENT"; exit 1; }
fi
RUNTIME="$RUNTIME_PARENT/runtime"
RT_PY="$RUNTIME/bin/python"

# ---------------------------------------------------------------- pick manifest
OSVER="$(sw_vers -productVersion 2>/dev/null || echo 0)"
MAJOR="${OSVER%%.*}"
if [ "${MAJOR:-0}" -ge 12 ] 2>/dev/null; then
    REQ="$HERE/requirements-macos.txt"
else
    REQ="$HERE/requirements-macos-legacy.txt"
fi
say "macOS $OSVER -> $(basename "$REQ")"

# ---------------------------------------------------------------- find python3
find_python() {
    local c
    for c in /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
             /usr/local/bin/python3.12 /usr/local/bin/python3.11 \
             "$HOME/.workbuddy/binaries/python/versions"/*/bin/python3.13 \
             "$(command -v python3.12 2>/dev/null)" \
             "$(command -v python3.11 2>/dev/null)" \
             "$(command -v python3 2>/dev/null)"; do
        [ -n "$c" ] && [ -x "$c" ] || continue
        if "$c" -c 'import sys; v=sys.version_info
ok = (3,10) <= v[:2] <= (3,13) and sys.maxsize > 2**32
print("OK" if ok else "NG", v[0], v[1])' 2>/dev/null | grep -q '^OK'; then
            printf '%s\n' "$c"
            return 0
        fi
    done
    return 1
}

# ---------------------------------------------------------------- reuse runtime
runtime_ok() {
    [ -x "$RT_PY" ] || return 1
    "$RT_PY" "$HERE/pycheck.py" --runtime --requirements "$REQ" --log >/dev/null 2>&1 || return 1
    "$RT_PY" "$HERE/desktop.py" --self-check >/dev/null 2>&1 || return 1
    return 0
}

cleanup_build_dir() {
    local d="$1"
    case "$d" in
        "$RUNTIME_PARENT"/runtime.building-*) rm -rf "$d" ;;
        *) say "refusing to remove $d" ;;
    esac
}

# stale build dirs from dead runs (only ours by name pattern)
for d in "$RUNTIME_PARENT"/runtime.building-*; do
    [ -d "$d" ] || continue
    pid="${d##*/runtime.building-}"
    if ! kill -0 "$pid" 2>/dev/null; then
        say "cleaning stale build dir: $(basename "$d")"
        cleanup_build_dir "$d"
    fi
done

if runtime_ok; then
    say "Existing runtime validation: OK"
else
    BASE_PY="$(find_python || true)"
    if [ -z "$BASE_PY" ]; then
        say ""
        say "[STARTUP FAILED] No supported 64-bit Python 3.10-3.13 found."
        say "Install Python from https://www.python.org/downloads/macos/ (3.12 recommended)"
        say "(or: brew install python@3.12)"
        log "no supported base python found"
        exit 1
    fi
    say "Base interpreter: $BASE_PY"
    BUILD="$RUNTIME_PARENT/runtime.building-$$"
    cleanup_build_dir "$BUILD"
    say "Creating an isolated runtime..."
    if ! "$BASE_PY" -m venv "$BUILD"; then
        say "[STARTUP FAILED] could not create the runtime."
        cleanup_build_dir "$BUILD"
        log "venv creation failed"
        exit 1
    fi
    B_PY="$BUILD/bin/python"
    say "Installing pip==$PIP_PIN ..."
    if ! "$B_PY" -m pip install --disable-pip-version-check --only-binary=:all: "pip==$PIP_PIN"; then
        say "[STARTUP FAILED] pip install failed."
        cleanup_build_dir "$BUILD"
        log "pip pin install failed"
        exit 1
    fi
    say "Installing runtime dependencies (about 150 MB on first run)..."
    if ! "$B_PY" -m pip install --disable-pip-version-check --only-binary=:all: -r "$REQ"; then
        say ""
        say "[STARTUP FAILED] dependency install failed."
        say "Paste the lines above back to the developer; a pinned version may not"
        say "provide a wheel for this macOS/Python combination."
        cleanup_build_dir "$BUILD"
        log "dependency install failed for $REQ"
        exit 1
    fi
    if ! "$B_PY" "$HERE/pycheck.py" --runtime --requirements "$REQ" --log >/dev/null 2>&1; then
        say "[STARTUP FAILED] runtime validation failed."
        "$B_PY" "$HERE/pycheck.py" --runtime --requirements "$REQ" --log || true
        cleanup_build_dir "$BUILD"
        exit 1
    fi
    OLD="$RUNTIME_PARENT/runtime.broken-$(date '+%Y%m%d-%H%M%S')-$$"
    [ -d "$RUNTIME" ] && mv "$RUNTIME" "$OLD"
    if ! mv "$BUILD" "$RUNTIME"; then
        say "[STARTUP FAILED] could not publish the runtime."
        [ -d "$OLD" ] && mv "$OLD" "$RUNTIME"
        cleanup_build_dir "$BUILD"
        exit 1
    fi
    [ -d "$OLD" ] && rm -rf "$OLD"
    say "Runtime is ready."
fi

if [ "$CHECK_ONLY" = "1" ]; then
    say "Runtime check passed."
    exit 0
fi

# ---------------------------------------------------------------- launch
say "Starting the viewer..."
exec "$RT_PY" "$HERE/desktop.py" "$@"
