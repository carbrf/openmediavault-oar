<?php

/**
 * This file is part of OpenMediaVault.
 *
 * @license   https://www.gnu.org/licenses/gpl.html GPL Version 3
 * @author    OpenMediaVault Plugin Developers <plugins@openmediavault.org>
 * @copyright Copyright (c) 2026 OpenMediaVault Plugin Developers
 *
 * OpenMediaVault is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * any later version.
 *
 * OpenMediaVault is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with OpenMediaVault. If not, see <https://www.gnu.org/licenses/>.
 */

namespace Engined\Rpc;

require_once("openmediavault/functions.inc");

class Oar extends \OMV\Rpc\ServiceAbstract
{
    /**
     * Get the RPC service name.
     */
    public function getName()
    {
        return "Oar";
    }

    /**
     * Initialize the RPC service.
     */
    public function initialize()
    {
        $this->registerMethod("enumeratePools");
        $this->registerMethod("getList");
        $this->registerMethod("get");
        $this->registerMethod("getCandidates");
        $this->registerMethod("getDisks");
        $this->registerMethod("preview");
        $this->registerMethod("create");
        $this->registerMethod("grow");
        $this->registerMethod("replace");
        $this->registerMethod("finalize");
        $this->registerMethod("delete");
        $this->registerMethod("scrub");
        $this->registerMethod("getDetail");
    }

    /**
     * Decode the JSON output of the 'omv-oar' command.
     * @param cmd The executed process object.
     * @param output The output of the executed command as an array of
     *   lines.
     * @return The decoded output as an associative array.
     * @throw \OMV\Exception
     */
    private function decodeEngineOutput($cmd, array $output)
    {
        $result = json_decode_safe(implode("", $output), TRUE);
        if (!is_array($result)) {
            throw new \OMV\Exception(
                "Failed to decode the output of the command '%s': %s",
                $cmd->getCommandLine(),
                implode("\n", $output)
            );
        }
        if (array_key_exists("error", $result)) {
            throw new \OMV\Exception(
                "The command '%s' failed: %s",
                $cmd->getCommandLine(),
                $result['error']
            );
        }
        return $result;
    }

    /**
     * Execute the 'omv-oar' command and decode its JSON output.
     * @param cmdArgs The command arguments.
     * @return The decoded output as an associative array.
     * @throw \OMV\ExecException
     * @throw \OMV\Exception
     */
    private function execEngineJson(array $cmdArgs)
    {
        $cmd = new \OMV\System\Process("omv-oar", $cmdArgs);
        $cmd->setRedirect2to1();
        $cmd->execute($output);
        return $this->decodeEngineOutput($cmd, $output);
    }

    /**
     * Get the status of the given pool.
     * @param name The name of the pool.
     * @return The pool status object or NULL if the pool does not exist.
     */
    private function getPoolStatus($name)
    {
        $cmdArgs = [];
        $cmdArgs[] = "status";
        $cmdArgs[] = "--json";
        $cmdArgs[] = escapeshellarg($name);
        $pools = $this->execEngineJson($cmdArgs);
        if (empty($pools)) {
            return NULL;
        }
        return $pools[0];
    }

    /**
     * Notify the configuration change for the given pool. Dispatches
     * one notification for the pool itself and one notification per
     * tier array using the notification identifier of the
     * openmediavault-md plugin, so its 'mdadm' module becomes dirty
     * and mdadm.conf is regenerated on Apply.
     * @param type The notification type, e.g. OMV_NOTIFY_CREATE,
     *   OMV_NOTIFY_MODIFY or OMV_NOTIFY_DELETE.
     * @param pool The pool status object.
     * @return void
     */
    private function notifyPool($type, array $pool)
    {
        $dispatcher = \OMV\Engine\Notify\Dispatcher::getInstance();
        // Notify about the pool itself.
        $devices = [];
        foreach ($pool['disks'] as $disk) {
            $devices[] = $disk['devicefile'];
        }
        $object = new \OMV\Config\ConfigObject(
            "conf.service.oar.pool"
        );
        $object->setAssoc([
            "name" => $pool['name'],
            "devicefile" => $pool['devicefile'],
            "fs" => $pool['fstype'],
//			"devices" => $devices
        ]);
        $object->set("devices", $devices); // Workaround
        $dispatcher->notify(
            $type,
            $object->getModel()->getNotificationId(),
            $object->getAssoc()
        );
        // Notify with the notification identifier of the
        // openmediavault-md plugin for each tier array.
        foreach ($pool['tiers'] as $tier) {
            $object = new \OMV\Config\ConfigObject(
                "conf.system.mdadm.device"
            );
            $object->setAssoc([
                "name" => $tier['name'],
                "devicefile" => $tier['devicefile'],
                "level" => $tier['level'],
                "numdevices" => count($tier['members']),
//				"devices" => $tier['members']
            ]);
            $object->set("devices", $tier['members']); // Workaround
            $dispatcher->notify(
                $type,
                $object->getModel()->getNotificationId(),
                $object->getAssoc()
            );
        }
    }

    /**
     * Enumerate all OAR pools on the system.
     * @param params The method parameters.
     * @param context The context of the caller.
     * @return An array of pool status objects with the following
     *   fields: name, devicefile, fstype, state, size, allocated,
     *   free, unallocatable, pending_finalize, disks, tiers and _used.
     */
    public function enumeratePools($params, $context)
    {
        // Validate the RPC caller context.
        $this->validateMethodContext($context, [
            "role" => OMV_ROLE_ADMINISTRATOR
        ]);
        // Get all existing OAR pools.
        $cmdArgs = [];
        $cmdArgs[] = "status";
        $cmdArgs[] = "--json";
        $pools = $this->execEngineJson($cmdArgs);
        // Prepare the result object.
        $result = [];
        foreach ($pools as $pool) {
            // Check if the pool is in use, e.g. does the file system
            // on it have a mount point configuration?
            $used = FALSE;
            if (FALSE !== \OMV\Rpc\Rpc::call(
                "FsTab",
                "getByFsName",
                [ "fsname" => $pool['devicefile'] ],
                $context
            )) {
                $used = TRUE;
            }
            $pool['_used'] = $used;
            $result[] = $pool;
        }
        return $result;
    }

    /**
     * Get list of available OAR pools.
     * @param params An array containing the following fields:
     *   \em start The index where to start.
     *   \em limit The number of objects to process.
     *   \em sortfield The name of the column used to sort.
     *   \em sortdir The sort direction, ASC or DESC.
     * @param context The context of the caller.
     * @return An array containing the requested objects. The field \em total
     *   contains the total number of objects, \em data contains the object
     *   array. An exception will be thrown in case of an error.
     */
    public function getList($params, $context)
    {
        // Validate the RPC caller context.
        $this->validateMethodContext($context, [
            "role" => OMV_ROLE_ADMINISTRATOR
        ]);
        // Validate the parameters of the RPC service method.
        $this->validateMethodParams($params, "rpc.common.getlist");
        // Enumerate all OAR pools on the system.
        $pools = $this->callMethod("enumeratePools", null, $context);
        // Filter the result list.
        return $this->applyFilter(
            $pools,
            $params['start'],
            $params['limit'],
            $params['sortfield'],
            $params['sortdir']
        );
    }

    /**
     * Get the status of the given pool.
     * @param params The method parameters.
     *   \em name The name of the pool.
     * @param context The context of the caller.
     * @return The pool status object.
     */
    public function get($params, $context)
    {
        // Validate the RPC caller context.
        $this->validateMethodContext($context, [
            "role" => OMV_ROLE_ADMINISTRATOR
        ]);
        // Validate the parameters of the RPC service method.
        $this->validateMethodParams($params, "rpc.oar.get");
        // Get the pool status.
        $pool = $this->getPoolStatus($params['name']);
        if (is_null($pool)) {
            throw new \OMV\Exception(
                "The pool '%s' does not exist.",
                $params['name']
            );
        }
        return $pool;
    }

    /**
     * Get list of devices that can be used to create or grow a pool.
     * @param params The method parameters.
     * @param context The context of the caller.
     * @return An array containing objects with the following fields:
     *   devicefile, size, vendor, serialnumber and description.
     */
    public function getCandidates($params, $context)
    {
        // Validate the RPC caller context.
        $this->validateMethodContext($context, [
            "role" => OMV_ROLE_ADMINISTRATOR
        ]);
        // Get the list of unused whole disks.
        $cmdArgs = [];
        $cmdArgs[] = "candidates";
        $cmdArgs[] = "--json";
        return $this->execEngineJson($cmdArgs);
    }

    /**
     * Preview the tier layout and capacity for the given devices.
     * @param params The method parameters.
     *   \em devices The devices to be used.
     * @param context The context of the caller.
     * @return An object with the following fields: usable,
     *   unallocatable and tiers.
     */
    public function preview($params, $context)
    {
        // Validate the RPC caller context.
        $this->validateMethodContext($context, [
            "role" => OMV_ROLE_ADMINISTRATOR
        ]);
        // Validate the parameters of the RPC service method.
        $this->validateMethodParams($params, "rpc.oar.preview");
        // Compute the tier layout preview.
        $cmdArgs = [];
        $cmdArgs[] = "preview";
        $cmdArgs[] = "--json";
        foreach (explode(",", $params['devices']) as $devicefile) {
            $cmdArgs[] = escapeshellarg($devicefile);
        }
        return $this->execEngineJson($cmdArgs);
    }

    /**
     * Create a OAR pool.
     * @param params The method parameters.
     *   \em name The name of the pool.
     *   \em devices The devices to be used.
     *   \em fs The file system to create, e.g. btrfs, ext4, xfs, f2fs, or jfs.
     * @param context The context of the caller.
     * @return The name of the background process status file.
     * @throw \OMV\Exception
     */
    public function create($params, $context)
    {
        // Validate the RPC caller context.
        $this->validateMethodContext($context, [
            "role" => OMV_ROLE_ADMINISTRATOR
        ]);
        // Validate the parameters of the RPC service method.
        $this->validateMethodParams($params, "rpc.oar.create");
        // Check if the pool name is already in use.
        if (!is_null($this->getPoolStatus($params['name']))) {
            throw new \OMV\Exception(
                "The pool '%s' already exists.",
                $params['name']
            );
        }
        // Create the background process.
        return $this->execBgProc(function ($bgStatusFilename, $bgOutputFilename) use ($params) {
            // Create the pool.
            $cmdArgs = [];
            $cmdArgs[] = "create";
            $cmdArgs[] = "--json";
            $cmdArgs[] = "--fs";
            $cmdArgs[] = escapeshellarg($params['fs']);
            $cmdArgs[] = escapeshellarg($params['name']);
            foreach (explode(",", $params['devices']) as $devicefile) {
                $cmdArgs[] = escapeshellarg($devicefile);
            }
            $cmd = new \OMV\System\Process("omv-oar", $cmdArgs);
            $cmd->setRedirect2to1();
            if (0 !== ($exitStatus = $this->exec(
                $cmd,
                $output,
                $bgOutputFilename
            ))) {
                throw new \OMV\ExecException($cmd, $output, $exitStatus);
            }
            // Get the pool status to build the notification payloads.
            $pool = $this->getPoolStatus($params['name']);
            if (is_null($pool)) {
                throw new \OMV\Exception(
                    "Failed to get the status of the pool '%s'.",
                    $params['name']
                );
            }
            // Notify the configuration change.
            $this->notifyPool(OMV_NOTIFY_CREATE, $pool);
            return $output;
        });
    }

    /**
     * Grow a OAR pool by adding new devices.
     * @param params The method parameters.
     *   \em name The name of the pool.
     *   \em devices The devices to be added.
     * @param context The context of the caller.
     * @return The name of the background process status file.
     * @throw \OMV\Exception
     */
    public function grow($params, $context)
    {
        // Validate the RPC caller context.
        $this->validateMethodContext($context, [
            "role" => OMV_ROLE_ADMINISTRATOR
        ]);
        // Validate the parameters of the RPC service method.
        $this->validateMethodParams($params, "rpc.oar.grow");
        // Create the background process.
        return $this->execBgProc(function ($bgStatusFilename, $bgOutputFilename) use ($params) {
            // Grow the pool.
            $cmdArgs = [];
            $cmdArgs[] = "grow";
            $cmdArgs[] = "--json";
            $cmdArgs[] = escapeshellarg($params['name']);
            foreach (explode(",", $params['devices']) as $devicefile) {
                $cmdArgs[] = escapeshellarg($devicefile);
            }
            $cmd = new \OMV\System\Process("omv-oar", $cmdArgs);
            $cmd->setRedirect2to1();
            if (0 !== ($exitStatus = $this->exec(
                $cmd,
                $output,
                $bgOutputFilename
            ))) {
                throw new \OMV\ExecException($cmd, $output, $exitStatus);
            }
            // Get the pool status to build the notification payloads.
            $pool = $this->getPoolStatus($params['name']);
            if (is_null($pool)) {
                throw new \OMV\Exception(
                    "Failed to get the status of the pool '%s'.",
                    $params['name']
                );
            }
            // Notify the configuration change.
            $this->notifyPool(OMV_NOTIFY_MODIFY, $pool);
            return $output;
        });
    }

    /**
     * Get the member disks of the given pool, including synthesized
     * entries for missing (dead and removed) disks. Used by the
     * replace/repair disk form.
     * @param params The method parameters.
     *   \em name The name of the pool.
     * @param context The context of the caller.
     * @return An array containing objects with the following fields:
     *   devicefile, size, state and description.
     */
    public function getDisks($params, $context)
    {
        // Validate the RPC caller context.
        $this->validateMethodContext($context, [
            "role" => OMV_ROLE_ADMINISTRATOR
        ]);
        // Validate the parameters of the RPC service method.
        $this->validateMethodParams($params, "rpc.oar.getdisks");
        // Get the pool status.
        $pool = $this->getPoolStatus($params['name']);
        if (is_null($pool)) {
            throw new \OMV\Exception(
                "The pool '%s' does not exist.",
                $params['name']
            );
        }
        $result = [];
        foreach ($pool['disks'] as $disk) {
            $result[] = [
                "devicefile" => $disk['devicefile'],
                "size" => $disk['size'],
                "state" => $disk['state'],
                "description" => sprintf(
                    "%s [%s, %s]",
                    $disk['devicefile'],
                    binary_format($disk['size']),
                    $disk['state']
                )
            ];
        }
        return $result;
    }

    /**
     * Replace a member disk of a OAR pool. When the old disk
     * is still alive a hot-replace is performed (redundancy is kept
     * during the rebuild); a dead or missing disk ('missing') is
     * rebuilt from parity onto the new disk.
     * @param params The method parameters.
     *   \em name The name of the pool.
     *   \em olddevice The device to be replaced, or 'missing'.
     *   \em newdevice The replacement device.
     * @param context The context of the caller.
     * @return The name of the background process status file.
     * @throw \OMV\Exception
     */
    public function replace($params, $context)
    {
        // Validate the RPC caller context.
        $this->validateMethodContext($context, [
            "role" => OMV_ROLE_ADMINISTRATOR
        ]);
        // Validate the parameters of the RPC service method.
        $this->validateMethodParams($params, "rpc.oar.replace");
        // Create the background process.
        return $this->execBgProc(function ($bgStatusFilename, $bgOutputFilename) use ($params) {
            // Replace the disk.
            $cmdArgs = [];
            $cmdArgs[] = "replace";
            $cmdArgs[] = "--json";
            $cmdArgs[] = escapeshellarg($params['name']);
            $cmdArgs[] = escapeshellarg($params['olddevice']);
            $cmdArgs[] = escapeshellarg($params['newdevice']);
            $cmd = new \OMV\System\Process("omv-oar", $cmdArgs);
            $cmd->setRedirect2to1();
            if (0 !== ($exitStatus = $this->exec(
                $cmd,
                $output,
                $bgOutputFilename
            ))) {
                throw new \OMV\ExecException($cmd, $output, $exitStatus);
            }
            // Get the pool status to build the notification payloads.
            $pool = $this->getPoolStatus($params['name']);
            if (is_null($pool)) {
                throw new \OMV\Exception(
                    "Failed to get the status of the pool '%s'.",
                    $params['name']
                );
            }
            // Notify the configuration change.
            $this->notifyPool(OMV_NOTIFY_MODIFY, $pool);
            return $output;
        });
    }

    /**
     * Kick off the finalization of a pending pool expansion via the
     * omv-oar-finalize@.service systemd unit. The unit waits
     * for running reshapes, restores the PPL consistency policy,
     * resizes the PVs/LV and grows the file system. Finalization is
     * normally started automatically; this method is the manual
     * fallback (e.g. after the automatic start failed).
     * @param params The method parameters.
     *   \em name The name of the pool.
     * @param context The context of the caller.
     * @return void
     */
    public function finalize($params, $context)
    {
        // Validate the RPC caller context.
        $this->validateMethodContext($context, [
            "role" => OMV_ROLE_ADMINISTRATOR
        ]);
        // Validate the parameters of the RPC service method.
        $this->validateMethodParams($params, "rpc.oar.finalize");
        // Make sure the pool exists.
        $pool = $this->getPoolStatus($params['name']);
        if (is_null($pool)) {
            throw new \OMV\Exception(
                "The pool '%s' does not exist.",
                $params['name']
            );
        }
        // Start the (idempotent) finalize systemd unit. It survives
        // an omv-engined restart and continues after a reboot.
        $cmdArgs = [];
        $cmdArgs[] = "start";
        $cmdArgs[] = "--no-block";
        $cmdArgs[] = escapeshellarg(sprintf(
            "omv-oar-finalize@%s.service",
            $params['name']
        ));
        $cmd = new \OMV\System\Process("systemctl", $cmdArgs);
        $cmd->setRedirect2to1();
        $cmd->execute();
    }

    /**
     * Delete a OAR pool. All member disks are wiped.
     * @param params The method parameters.
     *   \em name The name of the pool.
     *   \em force Set to TRUE to delete the pool even if its file
     *   system is mounted.
     * @param context The context of the caller.
     * @return The name of the background process status file.
     * @throw \OMV\Exception
     */
    public function delete($params, $context)
    {
        // Validate the RPC caller context.
        $this->validateMethodContext($context, [
            "role" => OMV_ROLE_ADMINISTRATOR
        ]);
        // Validate the parameters of the RPC service method.
        $this->validateMethodParams($params, "rpc.oar.delete");
        // Get the pool status BEFORE deleting it. The tier information
        // is required to build the notification payloads afterwards.
        $pool = $this->getPoolStatus($params['name']);
        if (is_null($pool)) {
            throw new \OMV\Exception(
                "The pool '%s' does not exist.",
                $params['name']
            );
        }
        // Create the background process.
        return $this->execBgProc(function ($bgStatusFilename, $bgOutputFilename) use ($params, $pool) {
            // Delete the pool.
            $cmdArgs = [];
            $cmdArgs[] = "delete";
            $cmdArgs[] = "--json";
            if (TRUE === $params['force']) {
                $cmdArgs[] = "--force";
            }
            $cmdArgs[] = escapeshellarg($params['name']);
            $cmd = new \OMV\System\Process("omv-oar", $cmdArgs);
            $cmd->setRedirect2to1();
            if (0 !== ($exitStatus = $this->exec(
                $cmd,
                $output,
                $bgOutputFilename
            ))) {
                throw new \OMV\ExecException($cmd, $output, $exitStatus);
            }
            // Notify the configuration change.
            $this->notifyPool(OMV_NOTIFY_DELETE, $pool);
            return $output;
        });
    }

    /**
     * Start a scrub of the given pool.
     * @param params The method parameters.
     *   \em name The name of the pool.
     * @param context The context of the caller.
     * @return The decoded output of the scrub command.
     */
    public function scrub($params, $context)
    {
        // Validate the RPC caller context.
        $this->validateMethodContext($context, [
            "role" => OMV_ROLE_ADMINISTRATOR
        ]);
        // Validate the parameters of the RPC service method.
        $this->validateMethodParams($params, "rpc.oar.scrub");
        // Start the scrub.
        $cmdArgs = [];
        $cmdArgs[] = "scrub";
        $cmdArgs[] = "--json";
        $cmdArgs[] = escapeshellarg($params['name']);
        return $this->execEngineJson($cmdArgs);
    }

    /**
     * Get details about the given pool.
     * @param params The method parameters.
     *   \em name The name of the pool.
     * @param context The context of the caller.
     * @return The details of the given pool as string.
     */
    public function getDetail($params, $context)
    {
        // Validate the RPC caller context.
        $this->validateMethodContext($context, [
            "role" => OMV_ROLE_ADMINISTRATOR
        ]);
        // Validate the parameters of the RPC service method.
        $this->validateMethodParams($params, "rpc.oar.getdetail");
        // Get the pool details.
        $cmdArgs = [];
        $cmdArgs[] = "detail";
        $cmdArgs[] = escapeshellarg($params['name']);
        $cmd = new \OMV\System\Process("omv-oar", $cmdArgs);
        $cmd->setRedirect2to1();
        $cmd->execute($output);
        return implode("\n", $output);
    }
}
