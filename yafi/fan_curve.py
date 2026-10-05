# fan_curve.py
#
# Copyright 2025 Stephen Horvath
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Save the fan set points and re-apply them on boot, without starting the GUI."""

import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from xml.sax.saxutils import escape

APPLY_ARG = "--apply-fan-curve"
AUTOSTART_NAME = "YAFI Fan Curve"
SET_POINT_KEYS = ("temp_fan_off", "temp_fan_max")

# The EC driver may not be ready straight after login
EC_RETRIES = 5
EC_RETRY_DELAY = 3


def config_path():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "yafi", "fan_curve.json")


def save(set_points):
    """Save the fan on/max temps of each sensor, indexed by sensor number."""
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = [{key: int(point[key]) for key in SET_POINT_KEYS} for point in set_points]
    with open(path, "w") as f:
        json.dump({"set_points": data}, f, indent=2)


def load():
    with open(config_path(), "r") as f:
        set_points = json.load(f)["set_points"]
    for point in set_points:
        for key in SET_POINT_KEYS:
            if type(point[key]) is not int:
                raise ValueError(f"{key} must be an integer")
        if point["temp_fan_off"] > point["temp_fan_max"]:
            raise ValueError("temp_fan_off cannot be higher than temp_fan_max")
    return set_points


def apply(ec):
    """Apply the saved set points to the EC, returns the number of sensors set."""
    import cros_ec_python.commands as ec_commands

    count = 0
    for i, point in enumerate(load()):
        # Only the fan temps are restored, the host thresholds are left to the EC
        thresholds = ec_commands.thermal.thermal_get_thresholds(ec, i)
        thresholds.update(point)
        ec_commands.thermal.thermal_set_thresholds(ec, i, thresholds)
        count += 1
    return count


def run():
    """Entry point for `yafi --apply-fan-curve`."""
    if getattr(sys, "frozen", False) and "_PYI_SPLASH_IPC" in os.environ:
        import pyi_splash
        pyi_splash.close()

    from cros_ec_python import get_cros_ec

    try:
        for attempt in range(EC_RETRIES):
            try:
                ec = get_cros_ec()
                break
            except Exception:
                if attempt == EC_RETRIES - 1:
                    raise
                time.sleep(EC_RETRY_DELAY)
        apply(ec)
    except Exception:
        traceback.print_exc()
        return 1
    return 0


def _command():
    if getattr(sys, "frozen", False):
        return [sys.executable, APPLY_ARG]
    executable = sys.executable
    if sys.platform == "win32":
        # Avoid a console window popping up on login
        pythonw = os.path.join(os.path.dirname(executable), "pythonw.exe")
        if os.path.exists(pythonw):
            executable = pythonw
    return [executable, "-m", "yafi", APPLY_ARG]


def autostart_supported():
    # The Flatpak sandbox can't see the host's autostart directory
    return not os.environ.get("FLATPAK_ID")


def autostart_enabled():
    if sys.platform == "win32":
        return _win_task_exists() or _win_run_key_exists()
    return os.path.exists(_xdg_autostart_path())


def set_autostart(enabled):
    if sys.platform == "win32":
        _win_set_autostart(enabled)
    elif enabled:
        path = _xdg_autostart_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(
                "[Desktop Entry]\n"
                "Type=Application\n"
                f"Name={AUTOSTART_NAME}\n"
                f"Exec={' '.join(_xdg_quote(arg) for arg in _command())}\n"
                "NoDisplay=true\n"
            )
    elif os.path.exists(_xdg_autostart_path()):
        os.remove(_xdg_autostart_path())


# Linux

def _xdg_autostart_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "autostart", "au.stevetech.yafi.fan-curve.desktop")


def _xdg_quote(arg):
    for char in ('\\', '"', '`', '$'):
        arg = arg.replace(char, '\\' + char)
    return f'"{arg}"'.replace('%', '%%')


# Windows

WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# schtasks' command line options can't allow starting on battery, so use XML
WIN_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Applies the fan set points saved in YAFI</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _win_schtasks(*args):
    return subprocess.run(
        ["schtasks", *args],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def _win_task_exists():
    return _win_schtasks("/Query", "/TN", AUTOSTART_NAME).returncode == 0


def _win_run_key_exists():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY) as key:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
        return True
    except FileNotFoundError:
        return False


def _win_set_autostart(enabled):
    import ctypes
    import winreg

    command = _command()

    if not enabled:
        if _win_task_exists():
            result = _win_schtasks("/Delete", "/TN", AUTOSTART_NAME, "/F")
            if result.returncode != 0:
                raise OSError(result.stderr.strip() or "Could not delete the scheduled task")
        if _win_run_key_exists():
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, AUTOSTART_NAME)
    elif ctypes.windll.shell32.IsUserAnAdmin():
        # Running as admin means the EC driver likely needs it (e.g. PawnIO),
        # only a scheduled task can start elevated without a UAC prompt.
        xml = WIN_TASK_XML.format(
            user=escape(f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"),
            command=escape(command[0]),
            arguments=escape(subprocess.list2cmdline(command[1:])),
        )
        fd, xml_path = tempfile.mkstemp(suffix=".xml")
        try:
            with os.fdopen(fd, "w", encoding="utf-16") as f:
                f.write(xml)
            result = _win_schtasks("/Create", "/TN", AUTOSTART_NAME, "/XML", xml_path, "/F")
        finally:
            os.remove(xml_path)
        if result.returncode != 0:
            raise OSError(result.stderr.strip() or "Could not create the scheduled task")
    else:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(command))
