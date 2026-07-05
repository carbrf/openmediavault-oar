# openmediavault-oar

Expandable, mixed-drive-size storage pools with single-parity redundancy for
[openmediavault](https://www.openmediavault.org) — **Open Adaptive RAID (OAR-1)**:

```
disks → GPT slices per size tier → mdadm RAID5 per tier (PPL write-hole
protection) → LVM volume group → single logical volume → BTRFS, EXT4, XFS, F2FS or JFS
```

Every component is standard, mature Linux storage infrastructure (mdadm, LVM2,
Btrfs). There is no proprietary on-disk format: a pool assembles on any Linux
system, with or without this plugin installed.

## Installation on a running openmediavault system

The plugin is a standard Debian package and can be installed on an existing,
already-running OMV 8.x installation at any time. No reinstall, no reboot.

### Install

```sh
wget https://github.com/carbrf/openmediavault-oar/releases/download/v8.1.0-beta1/openmediavault-oar_8.1.0.beta1-1_all.deb
sudo apt-get install ./openmediavault-oar_8.1.0.beta1-1_all.deb
```

That's it. The package's install hooks automatically restart `omv-engined`
(loads the RPC service), rebuild the workbench UI (adds the navigation entry
and the *File Systems → Create* menu item), and enable the boot-time
`omv-oar-finalize.service` unit. Reload the browser tab afterwards.

### Prefer installing from the OMV UI instead of the command line?

*System → Plugins* is where official and community plugins install from, and
it works for this one too — OMV ships a local trusted package archive for
exactly this purpose, no extra repository hosting needed:

```sh
# on the OMV host, as root
sudo wget -P /var/cache/openmediavault/archives \
  https://github.com/carbrf/openmediavault-oar/releases/download/v8.1.0-beta1/openmediavault-oar_8.1.0.beta1-1_all.deb
cd /var/cache/openmediavault/archives && sudo apt-ftparchive packages . > Packages && sudo apt-ftparchive release . > Release
```

Then in the web UI: **System → Plugins → ⟳ "Check for new plugins"**, select
`openmediavault-oar`, click **Install**. Uninstall works the same way from
the same page. Re-run the two commands above with a newer release any time
you want to upgrade — it'll show up as an update on that page too.

### Building it yourself instead

```sh
sudo apt-get install debhelper fakeroot gettext
git clone https://github.com/carbrf/openmediavault-oar.git
cd openmediavault-oar
fakeroot debian/rules clean binary
# result: ../openmediavault-oar_<version>_all.deb — install with either method above
```

### Surviving system updates

- **apt/OMV updates:** the plugin is a regular dpkg package; `apt upgrade`,
  `omv-upgrade` and unattended upgrades never remove manually installed
  packages. Its files live in plugin-owned paths and are never touched by
  core updates.
- **OMV major release upgrades** (e.g. 8 → 9): grab the matching release (or
  rebuild against the new OMV version) and install it again. This is the same
  rule that applies to every OMV plugin (`Depends: openmediavault (>= 8.5.0)`).
- **Your data does not depend on the plugin.** Pools are self-describing:
  GPT partition labels (`oar:<pool>:t<NN>`), mdadm superblocks and LVM
  metadata/tags live on the disks. Even with the plugin removed, Debian's
  standard udev/mdadm/LVM machinery assembles the arrays and activates the
  volume group at boot, and the filesystem stays mounted via the mount point
  managed by the OMV core. The plugin is management tooling, not a data-path
  dependency.

## Using it

### Create a pool

*Storage → File Systems → Create ▾ → Open Adaptive RAID (OAR-1)* — pick a pool
name, filesystem (Btrfs recommended) and two or more disks of any sizes. A
progress dialog streams the creation; afterwards you land on the standard
*Mount* page to mount the new filesystem and use it for shared folders as usual.

(The same form is available under *Storage → Open Adaptive RAID → Create*.)

Capacity rule of thumb: usable ≈ total − largest disk. Space that cannot be
protected yet (e.g. the top of a single largest disk) is reported as
*Unallocatable* and is used automatically once enough disks provide space at
that tier.

### Manage a pool

*Storage → Open Adaptive RAID* lists every pool with live state
(**Online / Checking / Expanding / Rebuilding / Degraded / Failed**), a
running-activity column (e.g. `reshape (42%)`), capacity, free space,
unallocatable space and member disks. Buttons:

| Button | What it does |
|---|---|
| **Create** | Same create form as File Systems → Create. |
| **Grow** | Add disks of any size. Arrays reshape online in the background; capacity appears when finalization completes automatically. |
| **Replace/repair device** | Replace a healthy disk (hot-replace, redundancy kept) or rebuild a **failed/missing** disk from parity onto a new one. This is the repair path when a pool is *Degraded*. |
| **Finalize expansion** | Manual kick for the (normally automatic) background finalization: waits for reshapes, restores PPL write-hole protection, grows LVM and the filesystem. Enabled only while an expansion is pending. |
| **Scrub** | Verifies parity of all tier arrays and (Btrfs, when mounted) data checksums in the background. |
| **Show details** | Per-tier state: array health, sync action and progress, consistency policy, members, mdadm details. |
| **Delete** | Destroys the pool and wipes the member disks. Blocked while the filesystem is referenced (mounted/shared). |

Everything the CLI can do is available in the UI. The CLI equivalent is
`omv-oar` (all mutating commands support `--dry-run` to print the exact
command plan without executing):

```
omv-oar candidates --json          # unused disks
omv-oar preview --json DEV...      # capacity calculator
omv-oar create [--fs btrfs|ext4|xfs|f2fs|jfs] POOL DEV...
omv-oar status --json [POOL]
omv-oar detail POOL
omv-oar grow POOL DEV...
omv-oar replace POOL OLDDEV|missing NEWDEV
omv-oar finalize [--all|POOL]
omv-oar scrub POOL
omv-oar delete [--force] POOL
```

### When a disk dies

1. The pool shows **Degraded** (mdmonitor also sends the usual email
   notifications via the openmediavault-md plugin). Data stays available.
2. *Storage → Open Adaptive RAID → Replace/repair device*: choose the failed or
   `missing` entry as the device to replace and the new disk. The rebuild
   runs in the background (**Rebuilding**); the pool returns to **Online**
   when done. A larger replacement automatically expands the pool where
   possible.

## Reliability notes

- The mdadm RAID5 write hole is closed with the kernel's Partial Parity Log
  (`--consistency-policy=ppl`); no journal device is needed. Because the
  kernel refuses reshapes while PPL is active, grow operations temporarily
  switch to a write-intent bitmap and finalization restores PPL — this is
  automatic and reboot-safe (marker tag on the volume group + boot-time
  finalize unit).
- Btrfs is created with metadata `DUP`, data `single` — checksums detect
  corruption; parity protection comes from the md layer beneath.
- Write performance costs roughly 30–40% versus plain RAID5 due to PPL —
  the price of closing the write hole without an extra device.

## Testing

- Unit tests (layout math, command plans, system introspection):

  ```sh
  cd openmediavault-oar   # repo root
  PYTHONPATH=usr/lib/python3/dist-packages \
    python3 -m unittest discover -s usr/share/openmediavault/unittests/python -v
  ```

- End-to-end test on loop devices (requires root, a Debian VM is fine —
  creates, grows, degrades, repairs, scrubs and deletes a real pool on
  sparse files):

  ```sh
  sudo /usr/share/openmediavault/oar/e2e-loopback.sh
  ```

## Current limitations

- Single parity only (OAR-1; one disk failure per pool).
  Dual-parity (RAID6 tiers, OAR-2) is a planned future variant.
- A pool needs at least two disks; the largest disk's excess over the
  second-largest is unallocatable until more disks are added.
- Removing a disk from a pool (shrinking) is not supported.
