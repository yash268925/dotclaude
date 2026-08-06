#!/usr/bin/env bash
# Claude Code statusline: rate limit usage meters
#   󱫥 (meter) 00% (hh:mm)  -> 5-hour window: usage % and time until reset
#   󰇡 (meter) 00% (m/dd)   -> 7-day window:  usage % and reset date
set -uo pipefail

METER_WIDTH="${METER_WIDTH:-10}"

input="$(cat)"

# --- helpers ---------------------------------------------------------------

# Draw a meter of METER_WIDTH cells using 1/8-width block glyphs.
# $1 = percentage (integer 0-100)
meter() {
  local pct=$1 width=$METER_WIDTH
  local eighths=$(( pct * width * 8 / 100 ))
  local full=$(( eighths / 8 ))
  local rem=$(( eighths % 8 ))
  local out="" i part=""

  # Filled cells, tinted by usage level.
  out+="$(color_for "$pct")"
  for (( i = 0; i < full; i++ )); do out+="█"; done

  if (( full < width )); then
    # Boundary cell: partial glyph in the usage color over a grey track,
    # so the unfilled remainder of the cell reads as track, not background.
    case $rem in
      1) part="▏" ;; 2) part="▎" ;; 3) part="▍" ;; 4) part="▌" ;;
      5) part="▋" ;; 6) part="▊" ;; 7) part="▉" ;;
    esac
    if [[ -n $part ]]; then
      out+="${TRACK_BG}${part}${BG_OFF}"
    else
      out+="${TRACK_FG}█"
    fi
    # Remaining empty cells: solid glyph in grey.
    out+="${TRACK_FG}"
    for (( i = full + 1; i < width; i++ )); do out+="█"; done
  fi
  printf '%s%s' "$out" "$RESET"
}

# Color by usage level: <60% green, <85% yellow, else red.
color_for() {
  local pct=$1
  if   (( pct < 60 )); then printf '\033[32m'
  elif (( pct < 85 )); then printf '\033[33m'
  else                      printf '\033[31m'
  fi
}

DIM=$'\033[2m'
RESET=$'\033[0m'
BG_OFF=$'\033[49m'
# Unfilled meter track. Defaults to the terminal's "bright black" so it follows
# the active theme; override with 256-color/truecolor codes if you prefer.
TRACK_FG="${TRACK_FG:-$'\033[90m'}"
TRACK_BG="${TRACK_BG:-$'\033[100m'}"

# --- read rate limits ------------------------------------------------------

read -r five_pct five_reset seven_pct seven_reset <<<"$(
  printf '%s' "$input" | jq -r '
    def n($v): if $v == null then -1 else ($v | floor) end;
    [ n(.rate_limits.five_hour.used_percentage)
    , n(.rate_limits.five_hour.resets_at)
    , n(.rate_limits.seven_day.used_percentage)
    , n(.rate_limits.seven_day.resets_at)
    ] | @tsv' 2>/dev/null
)"
: "${five_pct:=-1}" "${five_reset:=-1}" "${seven_pct:=-1}" "${seven_reset:=-1}"

# Current session id, truncated to 8 chars. Matches the session_id[:8] used
# as the display-key suffix in scripts/agent-usage-report.ts.
session_id="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null)"
sid8="${session_id:0:8}"
: "${sid8:=--------}"

# Format a unix timestamp in the local timezone. $1 = epoch, $2 = strftime fmt
fmt_time() {
  date -r "$1" "+$2" 2>/dev/null || date -d "@$1" "+$2" 2>/dev/null
}

# Segment 1: 5-hour window, reset clock time as HH:MM (local time)
if (( five_pct >= 0 )); then
  when=""
  (( five_reset > 0 )) && when=$(fmt_time "$five_reset" '%H:%M')
  : "${when:=--:--}"
  seg1="$(meter "$five_pct") $(printf '%2d' "$five_pct")% ${DIM}(${when})${RESET}"
else
  seg1="${DIM}$(meter 0)  --% (--:--)${RESET}"
fi

# Segment 2: 7-day window, reset date and clock time as m/d HH:MM (local time)
if (( seven_pct >= 0 )); then
  when=""
  (( seven_reset > 0 )) && when=$(fmt_time "$seven_reset" '%-m/%-d %H:%M')
  : "${when:=-/- --:--}"
  seg2="$(meter "$seven_pct") $(printf '%2d' "$seven_pct")% ${DIM}(${when})${RESET}"
else
  seg2="${DIM}$(meter 0)  --% (-/- --:--)${RESET}"
fi

printf '󱫥 %s %s|%s 󰇡 %s  %s%s%s\n' "$seg1" "$DIM" "$RESET" "$seg2" "$DIM" "$sid8" "$RESET"
