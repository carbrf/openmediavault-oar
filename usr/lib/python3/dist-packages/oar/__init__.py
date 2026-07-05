# -*- coding: utf-8 -*-
#
# This file is part of OpenMediaVault.
#
# @license   https://www.gnu.org/licenses/gpl.html GPL Version 3
# @author    OpenMediaVault Plugin Developers <plugins@openmediavault.org>
# @copyright Copyright (c) 2026 OpenMediaVault Plugin Developers
#
# OpenMediaVault is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# any later version.
#
# OpenMediaVault is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with OpenMediaVault. If not, see <https://www.gnu.org/licenses/>.
"""
oar - Open Adaptive RAID expandable mixed-size single-parity storage pools.

Stack: GPT partition slices -> mdadm RAID5 per tier (PPL write-hole
protection) -> LVM VG/LV -> btrfs (default), ext4, xfs, f2fs, or jfs.
"""
__version__ = "8.0.0"
