"""Firecracker microVM control plane — drop-in replacement for e2b SDK.

Each task gets a disposable copy of the base rootfs image, a fresh tap
interface, and its own Firecracker process. Communication is via SSH/SCP.

Host requirements (one-time setup on Hetzner server):
  /opt/vm/rootfs.ext4   — Ubuntu 22.04 image with SSH + Gemini CLI installed
  /opt/vm/vmlinux.bin   — Firecracker-compatible kernel
  /root/.ssh/id_ed25519 — private key (public key baked into rootfs root's authorized_keys)
  ip_forward enabled + iptables MASQUERADE rule for outbound internet access
"""

import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

_BASE_ROOTFS = Path("/opt/vm/rootfs.ext4")
_KERNEL = Path("/opt/vm/vmlinux.bin")
_VM_DIR = Path("/opt/vm/instances")
_SSH_KEY = "/root/.ssh/id_ed25519"
_SSH_USER = "root"
_SUBNETS_FILE = Path("/opt/vm/.subnets_used")


class CommandResult:
    """Result of a command executed inside a VM via SSH."""

    def __init__(self, stdout: str, stderr: str, exit_code: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class FirecrackerVM:
    """One disposable Firecracker microVM, managed via SSH."""

    def __init__(
        self,
        vm_id: str,
        tap_name: str,
        vm_ip: str,
        subnet_idx: int,
        rootfs_path: Path,
        config_path: Path,
        log_path: Path,
        proc: subprocess.Popen,
    ) -> None:
        self._vm_id = vm_id
        self._tap_name = tap_name
        self._vm_ip = vm_ip
        self._subnet_idx = subnet_idx
        self._rootfs_path = rootfs_path
        self._config_path = config_path
        self._log_path = log_path
        self._proc = proc

    # ── Public API ────────────────────────────────────────────────────────────

    @classmethod
    def create(cls, timeout: int = 300) -> "FirecrackerVM":
        """Boot a fresh VM from the base image and wait for SSH to be ready.

        Args:
            timeout: Total seconds to wait for SSH to become available.

        Returns:
            A ready FirecrackerVM instance.
        """
        _VM_DIR.mkdir(parents=True, exist_ok=True)
        vm_id = uuid.uuid4().hex[:8]
        subnet_idx = _alloc_subnet()
        tap_name = f"tap{subnet_idx}"
        host_ip = f"172.16.{subnet_idx}.1"
        vm_ip = f"172.16.{subnet_idx}.2"

        # Copy base rootfs (sparse copy is fast)
        rootfs_path = _VM_DIR / f"rootfs-{vm_id}.ext4"
        subprocess.run(
            ["cp", "--sparse=always", str(_BASE_ROOTFS), str(rootfs_path)],
            check=True,
        )

        # Create tap interface for this VM
        subprocess.run(["ip", "tuntap", "add", tap_name, "mode", "tap"], check=True)
        subprocess.run(["ip", "addr", "add", f"{host_ip}/24", "dev", tap_name], check=True)
        subprocess.run(["ip", "link", "set", tap_name, "up"], check=True)

        # Ensure NAT rule exists for this subnet (idempotent)
        subprocess.run(
            [
                "iptables", "-t", "nat", "-C", "POSTROUTING",
                "-s", f"172.16.{subnet_idx}.0/24",
                "!", "-d", f"172.16.{subnet_idx}.0/24",
                "-j", "MASQUERADE",
            ],
            capture_output=True,
        )
        # Add rule if it doesn't already exist (returncode != 0 means missing)
        _r = subprocess.run(
            [
                "iptables", "-t", "nat", "-C", "POSTROUTING",
                "-s", f"172.16.{subnet_idx}.0/24",
                "!", "-d", f"172.16.{subnet_idx}.0/24",
                "-j", "MASQUERADE",
            ],
            capture_output=True,
        )
        if _r.returncode != 0:
            subprocess.run(
                [
                    "iptables", "-t", "nat", "-A", "POSTROUTING",
                    "-s", f"172.16.{subnet_idx}.0/24",
                    "!", "-d", f"172.16.{subnet_idx}.0/24",
                    "-j", "MASQUERADE",
                ],
                check=False,
            )

        # Write per-VM Firecracker config (IP passed via kernel arg — no netplan needed)
        config_path = _VM_DIR / f"config-{vm_id}.json"
        config_path.write_text(_build_fc_config(rootfs_path, tap_name, vm_ip, host_ip))

        # Start Firecracker process
        log_path = _VM_DIR / f"serial-{vm_id}.log"
        proc = subprocess.Popen(
            ["firecracker", "--no-api", "--config-file", str(config_path)],
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
        )

        vm = cls(vm_id, tap_name, vm_ip, subnet_idx, rootfs_path, config_path, log_path, proc)
        vm._wait_for_ssh(timeout=90)

        # Write DNS config now that SSH is up (kernel ip= doesn't set resolv.conf)
        vm.run("echo 'nameserver 8.8.8.8' > /etc/resolv.conf", timeout=10)

        return vm

    def run(
        self,
        cmd: str,
        timeout: int = 60,
        env: Optional[dict[str, str]] = None,
    ) -> CommandResult:
        """Execute *cmd* in the VM via SSH.

        Args:
            cmd: Shell command to run inside the VM.
            timeout: Seconds before aborting.
            env: Environment variables to export before running the command.

        Returns:
            CommandResult with stdout, stderr, and exit_code.
        """
        if env:
            env_str = " ".join(f"{k}={v}" for k, v in env.items())
            full_cmd = f"export {env_str}; {cmd}"
        else:
            full_cmd = cmd

        ssh_args = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={min(timeout, 10)}",
            "-i", _SSH_KEY,
            f"{_SSH_USER}@{self._vm_ip}",
            full_cmd,
        ]
        try:
            result = subprocess.run(
                ssh_args,
                capture_output=True,
                text=True,
                timeout=timeout + 5,
            )
            return CommandResult(result.stdout, result.stderr, result.returncode)
        except subprocess.TimeoutExpired:
            return CommandResult("", f"Command timed out after {timeout}s", 1)

    def write_file(self, remote_path: str, content: str | bytes) -> None:
        """Upload *content* to *remote_path* inside the VM via SCP.

        Args:
            remote_path: Absolute path inside the VM.
            content: File contents as str or bytes.
        """
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(content if isinstance(content, bytes) else content.encode())
            tmp = f.name
        try:
            subprocess.run(
                [
                    "scp",
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "BatchMode=yes",
                    "-i", _SSH_KEY,
                    tmp,
                    f"{_SSH_USER}@{self._vm_ip}:{remote_path}",
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
        finally:
            os.unlink(tmp)

    def kill(self) -> None:
        """Terminate the VM and clean up all host-side resources."""
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()

        for path in [self._rootfs_path, self._config_path]:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass

        # Delete serial log separately so it survives into error messages on crash
        try:
            Path(self._log_path).unlink(missing_ok=True)
        except Exception:
            pass

        try:
            subprocess.run(["ip", "link", "del", self._tap_name], capture_output=True)
        except Exception:
            pass

        _free_subnet(self._subnet_idx)

    # ── Private ───────────────────────────────────────────────────────────────

    def _wait_for_ssh(self, timeout: int = 30) -> None:
        """Block until SSH is accepting connections or timeout is reached."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc.poll() is not None:
                log = Path(self._log_path).read_text(errors="replace")[-1000:]
                raise RuntimeError(
                    f"Firecracker exited (code {self._proc.returncode}) before SSH ready.\n"
                    f"Serial log:\n{log}"
                )
            r = subprocess.run(
                [
                    "ssh",
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "BatchMode=yes",
                    "-o", "ConnectTimeout=2",
                    "-i", _SSH_KEY,
                    f"{_SSH_USER}@{self._vm_ip}",
                    "true",
                ],
                capture_output=True,
            )
            if r.returncode == 0:
                return
            time.sleep(1)
        raise TimeoutError(f"VM {self._vm_id} SSH not ready after {timeout}s")


# ── Module helpers ─────────────────────────────────────────────────────────────


def _alloc_subnet() -> int:
    """Claim and return a free subnet index in range 1–250.

    Uses a plain text file to track used indices. Not safe for high
    concurrency but fine for the grindbot task queue (sequential tasks).
    """
    _SUBNETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    used: set[int] = set()
    if _SUBNETS_FILE.exists():
        try:
            used = {int(x) for x in _SUBNETS_FILE.read_text().split() if x.isdigit()}
        except ValueError:
            pass
    for i in range(1, 251):
        if i not in used:
            used.add(i)
            _SUBNETS_FILE.write_text(" ".join(map(str, sorted(used))))
            return i
    raise RuntimeError("No free VM subnet slots (max 250 concurrent VMs)")


def _free_subnet(idx: int) -> None:
    """Release a previously allocated subnet index."""
    if not _SUBNETS_FILE.exists():
        return
    try:
        used = {int(x) for x in _SUBNETS_FILE.read_text().split() if x.isdigit()}
        used.discard(idx)
        _SUBNETS_FILE.write_text(" ".join(map(str, sorted(used))))
    except Exception:
        pass



def _build_fc_config(rootfs_path: Path, tap_name: str, vm_ip: str, host_ip: str) -> str:
    """Return the Firecracker JSON config for one VM instance.

    Args:
        rootfs_path: Path to this VM's private rootfs copy.
        tap_name: Host tap interface name (e.g. tap1).
        vm_ip: IP to assign inside the VM (e.g. 172.16.1.2).
        host_ip: Gateway IP on the host tap side (e.g. 172.16.1.1).

    Returns:
        JSON string suitable for ``firecracker --config-file``.
    """
    # ip= format: <client>:<server>:<gw>:<mask>:<hostname>:<dev>:<autoconf>
    ip_arg = f"ip={vm_ip}::{host_ip}:255.255.255.0::eth0:off"
    return json.dumps(
        {
            "boot-source": {
                "kernel_image_path": str(_KERNEL),
                "boot_args": f"console=ttyS0 reboot=k panic=1 pci=off {ip_arg}",
            },
            "drives": [
                {
                    "drive_id": "rootfs",
                    "path_on_host": str(rootfs_path),
                    "is_root_device": True,
                    "is_read_only": False,
                }
            ],
            "machine-config": {"vcpu_count": 1, "mem_size_mib": 512},
            "network-interfaces": [
                {
                    "iface_id": "eth0",
                    "guest_mac": "AA:FC:00:00:00:01",
                    "host_dev_name": tap_name,
                }
            ],
        },
        indent=2,
    )
