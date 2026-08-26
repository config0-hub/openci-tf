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

phase_timing_run() {
  local phase="$1"
  shift
  local rc
  phase_timing_begin "$phase"
  set +e
  "$@"
  rc=$?
  set -e
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
