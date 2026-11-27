#!/usr/bin/env bash

# Per-phase elapsed time logging for install/uninstall recipes.
# Source this file, then call phase_timing_run or phase_timing_begin/end.

_phase_timing_format_duration() {
  local secs="$1"
  local m=$((secs / 60))
  local s=$((secs % 60))
  printf '%dm %ds' "$m" "$s"
}

phase_timing_begin() {
  local phase="$1"
  echo ">> ${phase} start" >&2
  _PHASE_TIMING_CURRENT="$phase"
  _PHASE_TIMING_START=$(date +%s)
}

phase_timing_end() {
  local rc="${1:-$?}"
  local phase="${_PHASE_TIMING_CURRENT:?phase_timing_end without phase_timing_begin}"
  local end elapsed dur
  end=$(date +%s)
  elapsed=$((end - _PHASE_TIMING_START))
  dur="$(_phase_timing_format_duration "$elapsed")"
  if [ "$rc" -eq 0 ]; then
    echo "<< ${phase} done in ${dur}" >&2
  else
    echo "<< ${phase} FAILED after ${dur}" >&2
    return "$rc"
  fi
}

# The phase runs in a subshell with errexit on, so a phase that is a shell
# function stops at its first failed command and the subshell's exit code is
# the phase's exit code. errexit is off only around the subshell, so the
# FAILED line prints before that code is returned; the caller's errexit state
# is restored afterwards.
# Bash ignores errexit inside a command whose status is tested (`||`, `&&`,
# `if`, `!`), subshells included. So call this as a plain command when the
# phase is a function; to read its exit code, use
#   set +e; phase_timing_run <phase> <cmd...>; rc=$?; set -e
phase_timing_run() {
  local phase="$1"
  shift
  local rc errexit=off
  case "$-" in *e*) errexit=on ;; esac
  phase_timing_begin "$phase"
  set +e
  (
    set -e
    "$@"
  )
  rc=$?
  if [ "$errexit" = on ]; then set -e; fi
  phase_timing_end "$rc"
  return "$rc"
}

phase_timing_total_begin() {
  _PHASE_TIMING_TOTAL_START=$(date +%s)
}

phase_timing_total_end() {
  local label="$1"
  local rc="${2:-0}"
  local end elapsed dur
  end=$(date +%s)
  elapsed=$((end - _PHASE_TIMING_TOTAL_START))
  dur="$(_phase_timing_format_duration "$elapsed")"
  if [ "$rc" -eq 0 ]; then
    echo "<< ${label} total in ${dur}" >&2
  else
    echo "<< ${label} total FAILED after ${dur}" >&2
  fi
}
