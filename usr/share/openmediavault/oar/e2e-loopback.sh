#!/usr/bin/env bash
#
# This file is part of the openmediavault-oar plugin.
#
# @license   https://www.gnu.org/licenses/gpl.html GPL Version 3
# @author    carbrf <carbrf@gmail.com>
# @copyright Copyright (c) 2026 carbrf
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# End-to-end loopback test for the openmediavault-oar engine.
#
# Exercises the full pool lifecycle on sparse-file loop devices:
#   create (4 mixed disks) -> mount -> write data -> grow (+1 disk) ->
#   finalize -> verify capacity increased and data intact -> degrade
#   (mdadm --fail) -> verify degraded -> re-add -> scrub -> delete.
#
# MUST be run as root on a scratch (virtual) machine. It is DESTRUCTIVE:
# it creates and destroys loop devices, md arrays and an LVM volume
# group named "e2etest".

set -euo pipefail
export LC_ALL=C.UTF-8

POOL="e2etest"
CLI="omv-oar"
DISK_SIZES=(2G 2G 4G 4G)
GROW_DISK_SIZE="6G"
PAYLOAD_MB=64

STEP_NO=0
WORKDIR=""
MNT=""
LOOPS=()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

step() {
	STEP_NO=$((STEP_NO + 1))
	echo
	echo "======================================================================"
	echo "  STEP ${STEP_NO}: $*"
	echo "======================================================================"
}

die() {
	echo "ERROR: $*" >&2
	exit 1
}

run() {
	echo "+ $*" >&2
	"$@"
}

# Extract a field from a pool status JSON document read on stdin. The
# document may be a single pool object or an array of pool objects (in
# which case the first entry is used). $1 is a python subscript
# expression, e.g. '["state"]' or '["tiers"][0]["members"][0]'.
PY_EXTRACT='
import json
import sys
d = json.load(sys.stdin)
if isinstance(d, list):
    if not d:
        sys.exit("error: empty pool status array")
    d = d[0]
print(eval("d" + sys.argv[1], {"d": d}))
'

json_field() { # $1=json document (string), $2=subscript expression
	python3 -c "$PY_EXTRACT" "$2" <<<"$1"
}

pool_field() { # $1=subscript expression; queries live pool status
	"$CLI" status --json "$POOL" | python3 -c "$PY_EXTRACT" "$1"
}

wait_for_state() { # $1=wanted state, $2=timeout seconds
	local want="$1" timeout="${2:-120}" state=""
	local deadline=$((SECONDS + timeout))
	while ((SECONDS < deadline)); do
		state=$(pool_field '["state"]' 2>/dev/null) || state=""
		if [[ "$state" == "$want" ]]; then
			echo "pool state: ${state}"
			return 0
		fi
		sleep 2
	done
	die "timed out after ${timeout}s waiting for pool state '${want}' (last seen: '${state:-unknown}')"
}

vg_size_bytes() {
	vgs --noheadings --nosuffix --units b -o vg_size "$POOL" | tr -d '[:space:]'
}

assert_gt() { # $1=after, $2=before, $3=label
	python3 -c 'import sys; sys.exit(0 if int(sys.argv[1]) > int(sys.argv[2]) else 1)' \
		"$1" "$2" || die "$3 did not increase (before=$2, after=$1)"
	echo "$3 increased: $2 -> $1"
}

create_loop() { # $1=backing file path, $2=size
	truncate -s "$2" "$1"
	losetup --find --show --partscan "$1"
}

# ---------------------------------------------------------------------------
# Cleanup — ALWAYS runs; must leave no loops, workdir, md arrays or VG behind.
# ---------------------------------------------------------------------------

cleanup() {
	local rc=$?
	trap - EXIT
	set +e
	echo
	echo "--- cleanup ---"
	if [[ -n "$MNT" ]] && mountpoint -q "$MNT" 2>/dev/null; then
		umount -l "$MNT" 2>/dev/null
	fi
	# Best-effort: deactivate a leftover VG and stop leftover md arrays.
	if vgs "$POOL" >/dev/null 2>&1; then
		vgchange -an "$POOL" >/dev/null 2>&1
	fi
	local md
	for md in /dev/md/"${POOL}"-t*; do
		[[ -e "$md" ]] && mdadm --stop "$md" >/dev/null 2>&1
	done
	local loop
	for loop in "${LOOPS[@]}"; do
		losetup -d "$loop" 2>/dev/null
	done
	if [[ -n "$WORKDIR" && -d "$WORKDIR" ]]; then
		# Catch any loop device still attached to our backing files.
		local img
		for img in "$WORKDIR"/disk*.img; do
			[[ -e "$img" ]] || continue
			for loop in $(losetup --noheadings --output NAME --associated "$img" 2>/dev/null); do
				losetup -d "$loop" 2>/dev/null
			done
		done
		rm -rf "$WORKDIR"
	fi
	echo
	if [[ $rc -eq 0 ]]; then
		echo "[E2E] PASS (exit code 0)"
	else
		echo "[E2E] FAIL (exit code $rc)"
	fi
	exit $rc
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
step "Preflight checks"
# ---------------------------------------------------------------------------

[[ $EUID -eq 0 ]] || die "this test must be run as root"
command -v "$CLI" >/dev/null 2>&1 || die "'$CLI' CLI not found in PATH"
for tool in losetup mdadm vgs vgchange lvs python3 truncate sha256sum \
	mountpoint udevadm mkdir dd sync; do
	command -v "$tool" >/dev/null 2>&1 || die "required tool '$tool' not found"
done
modprobe loop 2>/dev/null || true
if vgs "$POOL" >/dev/null 2>&1; then
	die "a volume group named '$POOL' already exists; refusing to run"
fi
if [[ -e "/dev/md/${POOL}-t00" ]]; then
	die "md array '${POOL}-t00' already exists; refusing to run"
fi
echo "preflight OK"

# ---------------------------------------------------------------------------
step "Prepare scratch workspace"
# ---------------------------------------------------------------------------

WORKDIR=$(mktemp -d /tmp/oar-e2e.XXXXXX)
MNT="${WORKDIR}/mnt"
mkdir -p "$MNT"
echo "workdir: $WORKDIR"

# ---------------------------------------------------------------------------
step "Create sparse backing files (${DISK_SIZES[*]}) and attach loop devices"
# ---------------------------------------------------------------------------

i=0
for size in "${DISK_SIZES[@]}"; do
	i=$((i + 1))
	img="${WORKDIR}/disk${i}.img"
	loop=$(create_loop "$img" "$size")
	LOOPS+=("$loop")
	echo "attached ${loop} (${size}, ${img})"
done

# ---------------------------------------------------------------------------
step "Create pool '${POOL}' (btrfs, default) on ${LOOPS[*]}"
# ---------------------------------------------------------------------------

CREATE_JSON=$(run "$CLI" create --json "$POOL" "${LOOPS[@]}")
echo "$CREATE_JSON"
CREATE_STATE=$(json_field "$CREATE_JSON" '["state"]')
# A fresh RAID5 array performs its initial sync right after creation, so
# the pool may legitimately report 'rebuilding' here.
case "$CREATE_STATE" in
	online|rebuilding|checking) ;;
	*) die "pool state after create is '$CREATE_STATE', expected online/rebuilding" ;;
esac
wait_for_state online 600
echo "pool created, state: online"

# ---------------------------------------------------------------------------
step "Mount /dev/${POOL}/data and write ${PAYLOAD_MB} MiB payload"
# ---------------------------------------------------------------------------

udevadm settle 2>/dev/null || true
for _ in $(seq 1 20); do
	[[ -e "/dev/${POOL}/data" ]] && break
	sleep 1
done
[[ -e "/dev/${POOL}/data" ]] || die "logical volume /dev/${POOL}/data did not appear"
run mount "/dev/${POOL}/data" "$MNT"
run dd if=/dev/urandom of="${MNT}/payload.bin" bs=1M count="$PAYLOAD_MB" status=none
sync
(cd "$MNT" && sha256sum payload.bin) >"${WORKDIR}/payload.sha256"
echo "payload checksum: $(cut -d' ' -f1 "${WORKDIR}/payload.sha256")"

SIZE_BEFORE=$(pool_field '["size"]')
VG_SIZE_BEFORE=$(vg_size_bytes)
FSTYPE=$(pool_field '["fstype"]')
echo "pool size before grow:   ${SIZE_BEFORE} bytes (status JSON)"
echo "VG size before grow:     ${VG_SIZE_BEFORE} bytes (vgs)"
echo "filesystem type:         ${FSTYPE}"

# ---------------------------------------------------------------------------
step "Grow pool with a 5th disk (${GROW_DISK_SIZE})"
# ---------------------------------------------------------------------------

GROW_IMG="${WORKDIR}/disk5.img"
GROW_LOOP=$(create_loop "$GROW_IMG" "$GROW_DISK_SIZE")
LOOPS+=("$GROW_LOOP")
echo "attached ${GROW_LOOP} (${GROW_DISK_SIZE}, ${GROW_IMG})"
GROW_JSON=$(run "$CLI" grow --json "$POOL" "$GROW_LOOP")
echo "$GROW_JSON"

# ---------------------------------------------------------------------------
step "Finalize directly (not via systemd)"
# ---------------------------------------------------------------------------

run "$CLI" finalize "$POOL"
wait_for_state online 900

# ---------------------------------------------------------------------------
step "Verify capacity increased and payload intact"
# ---------------------------------------------------------------------------

SIZE_AFTER=$(pool_field '["size"]')
VG_SIZE_AFTER=$(vg_size_bytes)
assert_gt "$SIZE_AFTER" "$SIZE_BEFORE" "pool size (status JSON)"
assert_gt "$VG_SIZE_AFTER" "$VG_SIZE_BEFORE" "VG size (vgs)"
(cd "$MNT" && sha256sum -c "${WORKDIR}/payload.sha256") \
	|| die "payload checksum mismatch after grow/finalize"
echo "capacity increased and payload intact"

# ---------------------------------------------------------------------------
step "Degrade tier 0 (mdadm --fail) and verify state 'degraded'"
# ---------------------------------------------------------------------------

TIER0_MD=$(pool_field '["tiers"][0]["devicefile"]')
TIER0_MEMBER=$(pool_field '["tiers"][0]["members"][0]')
echo "tier 0 array: ${TIER0_MD}, failing member: ${TIER0_MEMBER}"
run mdadm --manage "$TIER0_MD" --fail "$TIER0_MEMBER"
wait_for_state degraded 60

# ---------------------------------------------------------------------------
step "Re-add failed member and wait for resync"
# ---------------------------------------------------------------------------

run mdadm --manage "$TIER0_MD" --remove "$TIER0_MEMBER"
run mdadm --manage "$TIER0_MD" --add "$TIER0_MEMBER"
mdadm --wait "$TIER0_MD" || true
wait_for_state online 600

# ---------------------------------------------------------------------------
step "Scrub pool"
# ---------------------------------------------------------------------------

SCRUB_JSON=$(run "$CLI" scrub --json "$POOL")
echo "$SCRUB_JSON"
# Wait for the md 'check' pass on every tier array to complete.
while IFS= read -r md; do
	[[ -n "$md" ]] || continue
	mdadm --wait "$md" || true
done < <("$CLI" status --json "$POOL" | python3 -c '
import json
import sys
d = json.load(sys.stdin)
if isinstance(d, list):
    d = d[0] if d else sys.exit("error: empty pool status array")
for t in d["tiers"]:
    print(t["devicefile"])
')
# Wait for the btrfs scrub (started by the engine on the mounted fs).
if [[ "$FSTYPE" == "btrfs" ]] && command -v btrfs >/dev/null 2>&1; then
	deadline=$((SECONDS + 300))
	while btrfs scrub status "$MNT" 2>/dev/null | grep -q "running"; do
		((SECONDS < deadline)) || die "timed out waiting for btrfs scrub to finish"
		sleep 2
	done
fi
wait_for_state online 120
echo "scrub finished"

# ---------------------------------------------------------------------------
step "Unmount filesystem"
# ---------------------------------------------------------------------------

run umount "$MNT"

# ---------------------------------------------------------------------------
step "Delete pool and verify it is gone"
# ---------------------------------------------------------------------------

DELETE_JSON=$(run "$CLI" delete --json --force "$POOL")
echo "$DELETE_JSON"
if vgs "$POOL" >/dev/null 2>&1; then
	die "volume group '$POOL' still present after delete"
fi
if [[ -e "/dev/md/${POOL}-t00" ]]; then
	die "md array '${POOL}-t00' still present after delete"
fi
echo "pool deleted"

# Success: the EXIT trap prints the final PASS line and cleans up.
exit 0
