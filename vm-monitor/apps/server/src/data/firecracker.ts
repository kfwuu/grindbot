import { exec } from 'node:child_process';
import { existsSync } from 'node:fs';
import { readdir, readFile } from 'node:fs/promises';
import { promisify } from 'node:util';
import type { FirecrackerLoad, FirecrackerVm } from '@dashdot/common';

const execAsync = promisify(exec);

const VM_DIR = '/opt/vm/instances';
const BASE_ROOTFS = '/opt/vm/rootfs.ext4';
const KERNEL = '/opt/vm/vmlinux.bin';

/**
 * Parse ps output to find running firecracker processes.
 * Returns a map from config-file path → { pid, elapsed seconds }.
 */
async function getRunningPids(): Promise<
  Map<string, { pid: number; elapsed: number }>
> {
  try {
    const { stdout } = await execAsync('ps -eo pid,etimes,cmd --no-headers');
    const map = new Map<string, { pid: number; elapsed: number }>();

    for (const line of stdout.trim().split('\n')) {
      if (!line.includes('firecracker') || !line.includes('--config-file')) {
        continue;
      }
      const parts = line.trim().split(/\s+/);
      const pid = parseInt(parts[0]);
      const elapsed = parseInt(parts[1]);
      const cfgIdx = parts.findIndex((p) => p === '--config-file');
      if (cfgIdx >= 0 && cfgIdx + 1 < parts.length) {
        map.set(parts[cfgIdx + 1], { pid, elapsed });
      }
    }
    return map;
  } catch {
    return new Map();
  }
}

const getFirecrackerLoad = async (): Promise<FirecrackerLoad> => {
  const base_image_ok = existsSync(BASE_ROOTFS);
  const kernel_ok = existsSync(KERNEL);
  const runningPids = await getRunningPids();

  let files: string[] = [];
  try {
    const all = await readdir(VM_DIR);
    files = all.filter((f) => f.startsWith('config-') && f.endsWith('.json'));
  } catch {
    return { vms: [], base_image_ok, kernel_ok };
  }

  const vms: FirecrackerVm[] = [];

  for (const file of files) {
    const configPath = `${VM_DIR}/${file}`;
    try {
      const raw = await readFile(configPath, 'utf-8');
      const cfg = JSON.parse(raw);

      // vm_id from filename: config-{vm_id}.json
      const vm_id = file.replace('config-', '').replace('.json', '');

      // IP is embedded in kernel boot_args: ip={vm_ip}::{host_ip}:...
      const bootArgs: string = cfg['boot-source']?.boot_args ?? '';
      const ipMatch = bootArgs.match(/ip=(\d+\.\d+\.\d+\.\d+)/);
      const ip = ipMatch ? ipMatch[1] : 'unknown';

      const tap_name: string =
        cfg['network-interfaces']?.[0]?.host_dev_name ?? 'unknown';
      const vcpu_count: number = cfg['machine-config']?.vcpu_count ?? 1;
      const mem_size_mib: number = cfg['machine-config']?.mem_size_mib ?? 512;

      const info = runningPids.get(configPath);
      vms.push({
        vm_id,
        ip,
        tap_name,
        vcpu_count,
        mem_size_mib,
        pid: info?.pid ?? null,
        running: !!info,
        uptime_s: info?.elapsed ?? null,
      });
    } catch {
      // skip malformed config files
    }
  }

  vms.sort((a, b) => a.vm_id.localeCompare(b.vm_id));
  return { vms, base_image_ok, kernel_ok };
};

export default { dynamic: getFirecrackerLoad };
