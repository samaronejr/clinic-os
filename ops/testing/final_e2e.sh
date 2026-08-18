#!/bin/bash
set -euo pipefail
export LANG=C.UTF-8 LC_ALL=C.UTF-8

fail() {
  printf '%s\n' 'final-e2e: frozen launcher contract rejected' >&2
  exit 2
}

lexical_absolute() {
  local value="$1"
  [[ "$value" == /* && "$value" != *//* && "$value" != */./* \
    && "$value" != */../* && "$value" != */. && "$value" != */.. \
    && "$value" != *$'\n'* && "$value" != *$'\r'* ]]
}

mode_0400() {
  [[ ! -L "$1" && -f "$1" && "$(/usr/bin/stat --format='%a %h' -- "$1")" == '400 1' ]]
}

verify_input_manifest() {
  local manifest="$1"; shift
  mode_0400 "$manifest" || fail
  local descriptor line digest suffix expected
  exec {descriptor}<"$manifest"
  for expected in "$@"; do
    IFS= read -r line <&"$descriptor" || fail
    digest="${line%% *}"
    suffix="${line#"$digest"}"
    [[ "$digest" =~ ^[0-9a-f]{64}$ && "$suffix" == "  $expected" ]] || fail
  done
  if IFS= read -r line <&"$descriptor"; then fail; fi
  exec {descriptor}<&-
  /usr/bin/sha256sum --check --strict --status "$manifest" || fail
}

verify_launcher_manifest() {
  local manifest="$1"; shift
  mode_0400 "$manifest" || fail
  local descriptor line digest suffix path previous='' candidate seen=0 matched
  exec {descriptor}<"$manifest"
  while IFS= read -r line <&"$descriptor"; do
    digest="${line%% *}"
    suffix="${line#"$digest"}"
    path="${suffix#  }"
    [[ "$digest" =~ ^[0-9a-f]{64}$ && "$suffix" == "  $path" ]] || fail
    [[ -z "$previous" || "$previous" < "$path" ]] || fail
    matched=0
    for candidate in "$@"; do
      if [[ "$path" == "$candidate" ]]; then matched=$((matched + 1)); fi
    done
    [[ "$matched" -eq 1 ]] || fail
    previous="$path"; seen=$((seen + 1))
  done
  exec {descriptor}<&-
  [[ "$seen" -eq "$#" ]] || fail
  /usr/bin/sha256sum --check --strict --status "$manifest" || fail
}

[[ "$#" -eq 6 && "$1" == '--sha' && "$3" == '--inputs' \
  && "$5" == '--terminal-evidence-dir' ]] || fail
readonly sha="$2" inputs="$4" terminal_evidence_dir="$6"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || fail
lexical_absolute "$inputs" || fail
lexical_absolute "$terminal_evidence_dir" || fail
[[ "$inputs" == */terminal/inputs.json \
  && "$terminal_evidence_dir" == "${inputs%/inputs.json}/F3" ]] || fail
mode_0400 "$inputs" || fail

readonly terminal_root="${inputs%/inputs.json}"
readonly launcher_path="$terminal_root/F3-launcher.path"
readonly launcher_lstat="$terminal_root/F3-launcher.lstat"
readonly launcher_sha256="$terminal_root/F3-launcher.sha256"
readonly inputs_sha256="$terminal_root/inputs.sha256"
verify_input_manifest "$inputs_sha256" \
  "$launcher_lstat" "$launcher_path" "$launcher_sha256" "$inputs"
mode_0400 "$launcher_path" || fail

exec {launcher_descriptor}<"$launcher_path"
IFS= read -r launcher <&"$launcher_descriptor" || fail
if IFS= read -r extra <&"$launcher_descriptor"; then fail; fi
exec {launcher_descriptor}<&-
lexical_absolute "$launcher" || fail
[[ "$launcher" == */.venv/bin/python && -L "$launcher" && -x "$launcher" ]] || fail

readonly link_target="$(/usr/bin/readlink -- "$launcher")"
[[ -n "$link_target" && "$link_target" != *$'\n'* && "$link_target" != *$'\r'* ]] || fail
readonly target_hash_line="$(printf '%s' "$link_target" | /usr/bin/sha256sum)"
readonly target_hash="${target_hash_line%% *}"
readonly lstat_actual="$(/usr/bin/stat --format='%d %i %f %u %g %h' -- "$launcher") $target_hash"
mode_0400 "$launcher_lstat" || fail
exec {lstat_descriptor}<"$launcher_lstat"
IFS= read -r lstat_expected <&"$lstat_descriptor" || fail
if IFS= read -r extra <&"$lstat_descriptor"; then fail; fi
exec {lstat_descriptor}<&-
[[ "$lstat_actual" == "$lstat_expected" ]] || fail

readonly resolved_target="$(/usr/bin/readlink --canonicalize-existing -- "$launcher")"
lexical_absolute "$resolved_target" || fail
[[ -x "$resolved_target" && ! -L "$resolved_target" ]] || fail
readonly script="$(/usr/bin/readlink --canonicalize-existing -- "$0")"
readonly script_root="${script%/*}"
readonly supervisor="$script_root/final_e2e_supervisor.py"
readonly controller="$script_root/final_e2e_controller.py"
verify_launcher_manifest "$launcher_sha256" \
  "$resolved_target" "$script" "$supervisor" "$controller"

exec /usr/bin/env -i \
  HOME=/nonexistent \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PATH=/usr/bin:/bin \
  TZ=UTC \
  "$launcher" -I -P -B "$supervisor" \
  --sha "$sha" --inputs "$inputs" --terminal-evidence-dir "$terminal_evidence_dir"
