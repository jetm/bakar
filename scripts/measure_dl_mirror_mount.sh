#!/usr/bin/env bash
# Measure how a builder behaves when the read-only download share (the NFS
# mirror mount) is idle, up, and unreachable. Run on the builder as the build
# user. The destructive step (a temporary firewall DROP rule on the hub's NFS
# port, installed through sudo) happens only with --inject.
#
# Output: one tab-separated line per row: name, seconds, result.
# Exit status is non-zero when any row printed FAIL or fewer rows ran than
# expected. The closing line is always "rows: N/M run".
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: measure_dl_mirror_mount.sh --mount DIR [options]

  --mount DIR        mirror mountpoint (required)
  --hub HOST         hub address the firewall rule matches (required with --inject)
  --bitbake-dir DIR  BitBake checkout (default: $HOME/repos/personal/yocto/bitbake)
  --smoke PATH       smoke script (default: smoke_dl_file_premirror.py beside this script)
  --sstate DIR       with --inject, also stat one cold file under this sstate mount
                     while the hub is blocked (informational comparison, no threshold)
  --inject           also run the unreachable-mount rows; installs a temporary
                     firewall DROP rule for TCP 2049 to HOST through sudo
  --help             show this help

Safe rows: mount-info, hit-stat, statfs-probe-up, read-throughput, live-fetch-up.
With --inject: dangling-stat, statfs-probe-down, live-fetch-down, recovery-stat,
and sstate-cold-stat when --sstate is given.
Thresholds: stat on a dangling link 20 s, fetch with the mount down 120 s,
statfs probe 25 s.
EOF
}

MOUNT=""
HUB=""
BITBAKE_DIR="$HOME/repos/personal/yocto/bitbake"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SMOKE="$SCRIPT_DIR/smoke_dl_file_premirror.py"
INJECT=0
SSTATE=""

die_usage() {
  echo "measure_dl_mirror_mount.sh: $1" >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --mount)
      [ $# -ge 2 ] || die_usage "--mount needs a value"
      MOUNT="$2"
      shift 2
      ;;
    --hub)
      [ $# -ge 2 ] || die_usage "--hub needs a value"
      HUB="$2"
      shift 2
      ;;
    --bitbake-dir)
      [ $# -ge 2 ] || die_usage "--bitbake-dir needs a value"
      BITBAKE_DIR="$2"
      shift 2
      ;;
    --smoke)
      [ $# -ge 2 ] || die_usage "--smoke needs a value"
      SMOKE="$2"
      shift 2
      ;;
    --sstate)
      [ $# -ge 2 ] || die_usage "--sstate needs a value"
      SSTATE="$2"
      shift 2
      ;;
    --inject)
      INJECT=1
      shift
      ;;
    --help | -h)
      usage
      exit 0
      ;;
    *)
      die_usage "unknown option: $1"
      ;;
  esac
done

[ -n "$MOUNT" ] || die_usage "--mount is required"
if [ "$INJECT" -eq 1 ] && [ -z "$HUB" ]; then
  die_usage "--hub is required with --inject"
fi

DL="$MOUNT/downloads"
RUN=0
FAILS=0
EXPECTED=5
[ "$INJECT" -eq 1 ] && EXPECTED=9
[ "$INJECT" -eq 1 ] && [ -n "$SSTATE" ] && EXPECTED=10
RULE_KIND=""
SCRATCH="$(mktemp -d)"

remove_rule() {
  case "$RULE_KIND" in
    iptables)
      # Removes the iptables DROP rule this script added for TCP 2049 to the hub.
      sudo iptables -D OUTPUT -d "$HUB" -p tcp --dport 2049 -j DROP || echo "WARNING: could not remove iptables rule; remove it by hand" >&2
      ;;
    nft)
      # Deletes the nft table this script created, with its DROP rule.
      sudo nft delete table inet bakar_dlmeasure || echo "WARNING: could not delete nft table bakar_dlmeasure; remove it by hand" >&2
      ;;
  esac
  RULE_KIND=""
}

on_exit() {
  local rc=$?
  trap - EXIT
  if [ -n "$RULE_KIND" ]; then
    remove_rule
  fi
  # Removes the scratch directory created by mktemp (holds only a symlink and logs).
  rm -rf "$SCRATCH"
  printf 'rows: %d/%d run\n' "$RUN" "$EXPECTED"
  if [ "$rc" -eq 0 ] && { [ "$FAILS" -gt 0 ] || [ "$RUN" -lt "$EXPECTED" ]; }; then
    rc=1
  fi
  exit "$rc"
}
trap on_exit EXIT

T=0
RC=0

# timed CMD...: run with output discarded; sets T (seconds) and RC (exit status).
timed() {
  local s e
  s=$(date +%s.%N)
  "$@" >/dev/null 2>&1 && RC=0 || RC=$?
  e=$(date +%s.%N)
  T=$(awk -v s="$s" -v e="$e" 'BEGIN { printf "%.3f", e - s }')
}

over() {
  awk -v a="$1" -v b="$2" 'BEGIN { exit !(a > b) }'
}

# row NAME SECONDS RESULT: print one result line and count it.
row() {
  RUN=$((RUN + 1))
  case "$3" in
    FAIL*) FAILS=$((FAILS + 1)) ;;
  esac
  printf '%s\t%s\t%s\n' "$1" "$2" "$3"
}

PY=(python3)
if command -v uv >/dev/null 2>&1; then
  PY=(uv run python)
fi

# Extra smoke arguments. Empty while the mount is up (the smoke picks its own file);
# for the blocked-mount run it names a file copied off the mount beforehand, so the
# smoke itself never walks or reads the dead mount and only BitBake's fetch is timed.
LIVE_ARGS=()

# smoke_live LIMIT_SECONDS EXPECTED_KIND: run the smoke in live mode, set T and KIND.
smoke_live() {
  local out="$SCRATCH/smoke.out" s e rc=0
  s=$(date +%s.%N)
  timeout -k 5 "$1" "${PY[@]}" "$SMOKE" --bitbake-dir "$BITBAKE_DIR" --mirror-dir "$DL" "${LIVE_ARGS[@]}" >"$out" 2>&1 || rc=$?
  e=$(date +%s.%N)
  T=$(awk -v s="$s" -v e="$e" 'BEGIN { printf "%.3f", e - s }')
  RC=$rc
  KIND=$(sed -n 's/^final-kind=//p' "$out" | sed -n 1p)
}

# --- safe rows ---------------------------------------------------------

# The mountpoint is a systemd automount: until something touches it, findmnt
# shows only the autofs stub. A bounded stat triggers the NFS mount, and only
# the nfs entry is read, so the stub's own options cannot satisfy the check.
timeout -k 5 30 stat -f "$MOUNT" >/dev/null 2>&1 || true
opts=$(findmnt -rn -t nfs,nfs4 -o OPTIONS --mountpoint "$MOUNT" 2>/dev/null || true)
case ",$opts," in
  *,ro,*) ro_ok=1 ;;
  *) ro_ok=0 ;;
esac
case ",$opts," in
  *,softerr,*) softerr_ok=1 ;;
  *) softerr_ok=0 ;;
esac
if [ "$ro_ok" -eq 1 ] && [ "$softerr_ok" -eq 1 ]; then
  row mount-info 0 "$opts"
else
  row mount-info 0 "FAIL options='$opts' (need an nfs mount with ro and softerr)"
fi

mapfile -t hit_files < <(find "$DL" -type f 2>/dev/null | sed -n 1,20p)
if [ "${#hit_files[@]}" -eq 0 ]; then
  row hit-stat 0 "FAIL no files under $DL"
else
  timed stat "${hit_files[@]}"
  if [ "$RC" -eq 0 ]; then
    row hit-stat "$T" "ok n=${#hit_files[@]}"
  else
    row hit-stat "$T" "FAIL stat rc=$RC"
  fi
fi

timed stat -f -c %T "$DL"
if [ "$RC" -ne 0 ]; then
  row statfs-probe-up "$T" "FAIL stat -f rc=$RC"
elif over "$T" 25; then
  row statfs-probe-up "$T" "FAIL over 25s"
else
  row statfs-probe-up "$T" ok
fi

mapfile -t big_files < <(find "$DL" -type f -size -200M -printf '%s\t%p\n' 2>/dev/null | sort -rn | sed -n 1,3p)
if [ "${#big_files[@]}" -eq 0 ]; then
  row read-throughput 0 "FAIL no files under 200 MB in $DL"
else
  total_bytes=0
  read_paths=()
  for line in "${big_files[@]}"; do
    total_bytes=$((total_bytes + ${line%%$'\t'*}))
    read_paths+=("${line#*$'\t'}")
  done
  timed cat "${read_paths[@]}"
  if [ "$RC" -ne 0 ]; then
    row read-throughput "$T" "FAIL read rc=$RC"
  else
    mbps=$(awk -v b="$total_bytes" -v t="$T" 'BEGIN { if (t > 0) printf "%.1f", b / 1000000 / t; else print "inf" }')
    row read-throughput "$T" "MB/s=$mbps"
  fi
fi

KIND=""
smoke_live 300 link
if [ "$RC" -eq 0 ] && [ "$KIND" = link ]; then
  row live-fetch-up "$T" final-kind=link
else
  row live-fetch-up "$T" "FAIL final-kind=${KIND:-none} rc=$RC"
fi

# --- injected rows -----------------------------------------------------

progress() { echo "measure: $*" >&2; }

# The dangling link must point at a file nothing has looked up yet: hit-stat and
# the read rows warmed the attribute cache for the first files, and a cached
# target answers stat without touching the server. The 21st file is past what
# hit-stat statted; with fewer files the first one is used. Chosen here, while
# the mount is still up.
cold_file=""
if [ "$INJECT" -eq 1 ]; then
  cold_file=$(find "$DL" -type f 2>/dev/null | sed -n 21p || true)
  # One file under the sstate mount for the informational comparison row; found
  # now because walking a blocked mount would hang. -quit stops at the first file.
  sstate_file=""
  if [ -n "$SSTATE" ]; then
    sstate_file=$(find "$SSTATE" -type f -print -quit 2>/dev/null || true)
  fi
  # A small mirror file copied off the mount now, for the blocked-mount fetch.
  # -maxdepth 1: the smoke looks the file up by name directly under the mirror
  # directory, so a file found inside git2/<n>/ would be a miss and time the wrong thing.
  small=$(find "$DL" -maxdepth 1 -type f -size -5M -print -quit 2>/dev/null || true)
  if [ -n "$small" ]; then
    mkdir -p "$SCRATCH/live-copy"
    cp "$small" "$SCRATCH/live-copy/"
    LIVE_ARGS=(--live-bytes "$SCRATCH/live-copy/$(basename "$small")")
  fi
fi

if [ "$INJECT" -eq 1 ]; then
  progress "installing the firewall rule (sudo may ask you to authenticate or touch a key)"
  if command -v iptables >/dev/null 2>&1; then
    # Inserts a firewall DROP rule on this host for TCP 2049 to the hub; the EXIT trap removes it.
    # RULE_KIND is set first: a signal that lands while sudo is running must still
    # reach the trap's removal (a removal of a rule that was never added only warns).
    RULE_KIND=iptables
    sudo iptables -I OUTPUT -d "$HUB" -p tcp --dport 2049 -j DROP
  elif command -v nft >/dev/null 2>&1; then
    # Creates an nft table with a DROP rule for TCP 2049 to the hub; the EXIT trap deletes the table.
    RULE_KIND=nft
    sudo nft add table inet bakar_dlmeasure
    sudo nft add chain inet bakar_dlmeasure out '{ type filter hook output priority 0 ; }'
    sudo nft add rule inet bakar_dlmeasure out ip daddr "$HUB" tcp dport 2049 drop
  else
    echo "measure_dl_mirror_mount.sh: neither iptables nor nft found; cannot inject" >&2
    exit 3
  fi

  # cold_file was chosen before the rule went in: walking the tree on a blocked
  # mount would hang this script for minutes before any row printed.
  link="$SCRATCH/dangling"
  target_file=${cold_file:-${hit_files[0]:-$DL/none}}
  ln -s "$target_file" "$link"
  worst=0
  verdict=ok
  for _ in 1 2 3; do
    timed timeout -k 5 40 stat -L "$link"
    if over "$T" "$worst"; then worst=$T; fi
    if [ "$RC" -eq 124 ]; then
      verdict="FAIL stat timed out at 40s"
    elif over "$T" 20 && [ "$verdict" = ok ]; then
      verdict="FAIL over 20s"
    fi
  done
  row dangling-stat "$worst" "$verdict"

  # Informational: the same cold stat on the existing sstate mount, blocked by the
  # same rule, to see whether the download mirror adds a new kind of stall or only
  # joins the one the builders already have. No threshold and never a FAIL.
  if [ -n "$SSTATE" ]; then
    progress "running sstate-cold-stat (up to 40 s)"
    if [ -z "$sstate_file" ]; then
      row sstate-cold-stat 0 "skipped: no file found under $SSTATE before the block"
    else
      timed timeout -k 5 40 stat "$sstate_file"
      if [ "$RC" -eq 124 ]; then
        row sstate-cold-stat "$T" "stalled: timed out at 40s (informational)"
      else
        row sstate-cold-stat "$T" "answered rc=$RC (informational)"
      fi
    fi
  fi

  progress "dangling-stat done; running statfs-probe-down"
  # bakar's readiness probe runs stat -f in a child killed at a 20 s deadline
  # (mounts.CACHE_PROBE_DEADLINE_S), so a dead mount must cost at most that, not
  # the hang of an unbounded stat. The row passes when the bounded call returns
  # within 25 s, whether stat -f itself answered or the deadline killed it.
  timed timeout -k 2 20 stat -f -c %T "$DL"
  if over "$T" 25; then
    row statfs-probe-down "$T" "FAIL bounded probe took over 25s"
  elif [ "$RC" -eq 124 ] || [ "$RC" -eq 137 ]; then
    row statfs-probe-down "$T" "ok bounded at 20s deadline"
  else
    row statfs-probe-down "$T" "ok rc=$RC"
  fi

  progress "running live-fetch-down (up to 150 s)"
  KIND=""
  smoke_live 150 file
  if [ "$RC" -eq 124 ]; then
    row live-fetch-down "$T" "FAIL timed out at 150s"
  elif over "$T" 120; then
    row live-fetch-down "$T" "FAIL over 120s"
  elif [ "$RC" -eq 0 ] && [ "$KIND" = file ]; then
    row live-fetch-down "$T" final-kind=file
  else
    row live-fetch-down "$T" "FAIL final-kind=${KIND:-none} rc=$RC"
  fi

  progress "removing the firewall rule"
  remove_rule

  start=$(date +%s.%N)
  recovered=0
  for _ in $(seq 1 60); do
    if timeout -k 2 5 stat "$MOUNT" >/dev/null 2>&1; then
      recovered=1
      break
    fi
    sleep 1
  done
  end=$(date +%s.%N)
  T=$(awk -v s="$start" -v e="$end" 'BEGIN { printf "%.3f", e - s }')
  if [ "$recovered" -eq 1 ]; then
    row recovery-stat "$T" ok
  else
    row recovery-stat "$T" "FAIL no successful stat within 60s"
  fi
fi
