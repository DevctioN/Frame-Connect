"""Read-only status collector, sent to the headset over SSH stdin.

Nothing is installed on the headset. Keep this file standard-library only.
"""

import json
import math
from pathlib import Path
import platform
import shutil
import socket
import subprocess

from steam_api import query_steam


# Steam scales the physical battery gauge. Ask its battery API for the same
# estimate used by the Steam client, without installing a service or keeping a
# listener alive. The timeout also disposes the listener if no value arrives.
STEAM_BATTERY_QUERY = """new Promise((resolve) => {
    let subscription;
    let complete = false;
    const dispose = () => { try { subscription?.unregister?.(); } catch (_) {} };
    const finish = (value) => {
        if (complete) return;
        complete = true;
        clearTimeout(timer);
        dispose();
        resolve(value);
    };
    const timer = setTimeout(() => finish(null), 750);
    try {
        subscription = SteamClient.System.RegisterForBatteryStateChanges((state) => {
            finish(state.bHasBattery ? state.flLevel : null);
        });
        if (complete) dispose();
    } catch (_) { finish(null); }
})"""


def read_steam_battery() -> int | None:
    """Prefer Steam's displayed estimate, preserving the system fallback."""
    level = query_steam(STEAM_BATTERY_QUERY)
    if type(level) in (int, float) and math.isfinite(level) and 0 <= level <= 1:
        return int(level * 100 + .5)
    return None


def downloads_directory() -> str:
    try:
        result = subprocess.run(
            ["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True,
            check=True, timeout=3,
        )
        path = result.stdout.strip()
        if path and Path(path).is_absolute():
            return path
    except (OSError, subprocess.SubprocessError):
        pass
    return str(Path.home() / "Downloads")


def read_attribute(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def read_batteries(root: Path = Path("/sys/class/power_supply")) -> list[dict]:
    batteries = []
    try:
        supplies = sorted(root.iterdir())
    except OSError:
        return batteries

    for supply in supplies:
        if read_attribute(supply / "type") != "Battery":
            continue
        # Controllers can also appear here. Only report system batteries.
        if read_attribute(supply / "scope") == "Device":
            continue
        if read_attribute(supply / "present") == "0":
            continue
        try:
            percent = int(read_attribute(supply / "capacity") or "")
        except ValueError:
            continue
        if 0 <= percent <= 100:
            batteries.append({
                "name": supply.name,
                "percent": percent,
                "state": read_attribute(supply / "status") or "Unknown",
            })
    return batteries


def read_temperature(root: Path = Path("/sys/class/thermal")) -> dict | None:
    """Highest readable thermal-zone temperature, preserving its sensor name."""
    try:
        zones = sorted(root.glob("thermal_zone[0-9]*"))
    except OSError:
        return None
    readings = []
    for zone in zones:
        try:
            # Linux thermal-zone temperatures are in millidegrees Celsius.
            celsius = int(read_attribute(zone / "temp") or "") / 1000
        except ValueError:
            continue
        # Reject implausible headset readings and driver error sentinels.
        if -100 <= celsius <= 200:
            readings.append({
                "celsius": celsius,
                "sensor": read_attribute(zone / "type") or zone.name,
            })
    return max(readings, key=lambda reading: reading["celsius"], default=None)


def read_memory(path: Path = Path("/proc/meminfo")) -> dict | None:
    """Usable RAM and the kernel's available-memory estimate, in bytes."""
    source = read_attribute(path)
    if source is None:
        return None
    values = {}
    for line in source.splitlines():
        key, separator, value = line.partition(":")
        if key not in ("MemTotal", "MemAvailable"):
            continue
        parts = value.split()
        if not separator or key in values or len(parts) != 2 or parts[1] != "kB":
            return None
        try:
            values[key] = int(parts[0]) * 1024
        except ValueError:
            return None
    total, available = values.get("MemTotal", 0), values.get("MemAvailable", -1)
    if total <= 0 or not 0 <= available <= total:
        return None
    return {"total": total, "available": available}


def read_os_version() -> str | None:
    """Read the distribution version, rather than the Linux kernel version."""
    try:
        release = platform.freedesktop_os_release()
    except (OSError, UnicodeError):
        return None
    version = release.get("VERSION_ID", "").strip() or release.get("VERSION", "").strip()
    if not version:
        return None
    name = release.get("NAME", "OS").strip() or "OS"
    return f"{name} {version}"


def collect_status() -> dict:
    # SteamOS keeps user content under /home, separate from the OS partition.
    usage = shutil.disk_usage(Path.home())
    batteries = read_batteries()
    if len(batteries) == 1:
        percent = read_steam_battery()
        if percent is not None:
            batteries[0].update(percent=percent, source="steam")
    return {
        "hostname": socket.gethostname(),
        "downloads": downloads_directory(),
        "batteries": batteries,
        "storage": {"total": usage.total, "used": usage.used, "free": usage.free},
        "temperature": read_temperature(),
        "memory": read_memory(),
        "os_version": read_os_version(),
    }


if __name__ == "__main__":
    print(json.dumps(collect_status()))
