"""Production-script startup preparation; independent of the flashing SDK."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess
import sys


VC_REDIST = "https://aka.ms/vc14/vc_redist.x64.exe"
_XDS_ID = re.compile(r"VID_0451&PID_BEF[0-4]|VID_1CBE&PID_(?:029E|029F|02A5)", re.I)


def require_file(path: Path):
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("依赖文件缺失或为空: %s；请重新复制完整工具目录" % path)
    with path.open("rb") as source:
        header = source.read(128)
    if header.startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise RuntimeError("Git LFS 文件尚未下载: %s；在项目根目录执行 git lfs pull，或重新复制完整目录" % path)
    return header


def prepare_linux(runtime: Path, log):
    """Only unpack our default bundle. Custom runtime directories are never replaced."""
    dependencies = Path(__file__).resolve().parents[1] / "dependencies"
    if runtime != (dependencies / "linux").resolve():
        return
    if runtime.is_dir() and any(runtime.iterdir()):
        return
    setup = dependencies / "setup_linux.py"
    require_file(setup)
    require_file(dependencies / "linux.tar.gz")
    command = [sys.executable, "-B", str(setup), "--quiet"]
    log.info("PREPARE_LINUX %s", command)
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=180, check=False)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Linux 运行库解压超时；检查磁盘空间和目录写权限，再重新运行") from error
    output = result.stdout.decode("utf-8", errors="replace").strip()
    log.info("PREPARE_LINUX_EXIT rc=%s output=%s", result.returncode, output)
    if result.returncode:
        raise RuntimeError("Linux 运行库准备失败: %s；检查压缩包、磁盘空间和目录写权限" % output)


def check_windows_runtime(root: Path):
    # Keep the C++ runtime set together, including CONCRT used by older TI
    # combinations; help output alone cannot prove delay imports are available.
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    folders = [root / "ccs_base/DebugServer/bin", root / "ccs_base/DebugServer/drivers",
               root / "ccs_base/common/bin", root / "ccs_base/common/uscif", system]
    missing = []
    for name in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll"):
        path = next((folder / name for folder in folders if (folder / name).is_file()), None)
        if path is None:
            missing.append(name)
        else:
            require_file(path)
    if missing:
        raise RuntimeError("缺少 VC++ x64 运行库: %s；安装微软运行库 %s 后重新运行" %
                           (", ".join(missing), VC_REDIST))
    manifest = root / "vc_runtime_manifest.json"
    if manifest.is_file():
        try:
            files = json.loads(manifest.read_text(encoding="utf-8"))["files"]
            for name in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll"):
                path = root / "ccs_base/DebugServer/bin" / name
                require_file(path)
                if hashlib.sha256(path.read_bytes()).hexdigest() != files[name]["sha256"]:
                    raise RuntimeError("VC++ 运行库校验失败: %s；请重新复制完整依赖目录" % path)
        except (ValueError, KeyError, TypeError) as error:
            raise RuntimeError("VC++ 运行库校验清单无效: %s；请恢复完整依赖目录" % manifest) from error


def driver_command(root: Path):
    return 'pnputil /add-driver "%s" /install' % (root / "ccs_base/emulation/windows/xds110_drivers/*.inf")


def _windows_usb_devices():
    """Inspect present PnP nodes; never open the probe or its target."""
    import ctypes as C
    from ctypes import wintypes as W

    class Device(C.Structure):
        _fields_ = [("size", W.DWORD), ("guid", C.c_byte * 16),
                    ("instance", W.DWORD), ("reserved", C.c_size_t)]

    api = C.WinDLL("setupapi", use_last_error=True)
    cfg = C.WinDLL("cfgmgr32", use_last_error=True)
    api.SetupDiGetClassDevsW.argtypes = [C.c_void_p, W.LPCWSTR, W.HWND, W.DWORD]
    api.SetupDiGetClassDevsW.restype = W.HANDLE
    api.SetupDiEnumDeviceInfo.argtypes = [W.HANDLE, W.DWORD, C.POINTER(Device)]
    api.SetupDiEnumDeviceInfo.restype = W.BOOL
    api.SetupDiGetDeviceRegistryPropertyW.argtypes = [W.HANDLE, C.POINTER(Device), W.DWORD,
                                                    C.c_void_p, C.c_void_p, W.DWORD, C.c_void_p]
    api.SetupDiGetDeviceRegistryPropertyW.restype = W.BOOL
    api.SetupDiDestroyDeviceInfoList.argtypes = [W.HANDLE]
    api.SetupDiDestroyDeviceInfoList.restype = W.BOOL
    cfg.CM_Get_DevNode_Status.argtypes = [C.POINTER(W.DWORD), C.POINTER(W.DWORD), W.DWORD, W.DWORD]
    cfg.CM_Get_DevNode_Status.restype = W.DWORD
    handle = api.SetupDiGetClassDevsW(None, "USB", None, 0x02 | 0x04)
    if handle == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    devices = []
    try:
        def property_of(device, prop):
            buffer = C.create_unicode_buffer(4096)
            if api.SetupDiGetDeviceRegistryPropertyW(handle, C.byref(device), prop, None,
                                                    buffer, C.sizeof(buffer), None):
                return buffer.value
            return ""

        index = 0
        while True:
            device = Device()
            device.size = C.sizeof(device)
            if not api.SetupDiEnumDeviceInfo(handle, index, C.byref(device)):
                if C.get_last_error() == 259:  # ERROR_NO_MORE_ITEMS
                    break
                raise C.WinError(C.get_last_error())
            index += 1
            identity = property_of(device, 1)  # SPDRP_HARDWAREID
            if not _XDS_ID.search(identity):
                continue
            status, problem = W.DWORD(), W.DWORD()
            if cfg.CM_Get_DevNode_Status(C.byref(status), C.byref(problem), device.instance, 0) != 0:
                continue  # Device may have disappeared during enumeration.
            devices.append((identity, property_of(device, 4),
                            problem.value if status.value & 0x400 else 0))
    finally:
        api.SetupDiDestroyDeviceInfoList(handle)
    return devices


def _windows_usb_problem(root: Path, devices):
    for identity, service, problem in devices:
        interface = re.search(r"&MI_([0-9A-F]{2})", identity, re.I)
        if interface and interface.group(1).upper() != "02":
            continue  # UART/data interfaces are not required for Flash programming.
        if problem:
            if problem in (1, 10, 18, 28, 31, 39, 48, 52):
                return ("XDS110 USB 驱动异常，设备管理器代码 %s，设备 %s；"
                        "管理员 PowerShell 执行 %s，然后重新插入 USB" %
                        (problem, identity, driver_command(root)))
            return "XDS110 USB 设备异常，设备管理器代码 %s，设备 %s；检查 USB 线、供电和设备管理器" % (problem, identity)
        if "&MI_02" in identity.upper() and service.lower() != "winusb":
            return ("XDS110 调试接口未绑定 WinUSB，当前驱动 %s，设备 %s；"
                    "管理员 PowerShell 执行 %s，然后重新插入 USB" %
                    (service or "未安装", identity, driver_command(root)))
    return ""


def _linux_usb_problem(runtime: Path, sysfs=Path("/sys/bus/usb/devices"), devfs=Path("/dev/bus/usb")):
    if not sysfs.is_dir():
        return ""
    for device in sysfs.iterdir():
        try:
            vendor = (device / "idVendor").read_text().strip()
            product = (device / "idProduct").read_text().strip()
            if not _XDS_ID.search("VID_%s&PID_%s" % (vendor, product)):
                continue
            bus = int((device / "busnum").read_text())
            number = int((device / "devnum").read_text())
        except (FileNotFoundError, ValueError):
            continue  # Hot-unplug or a sysfs interface node rather than a device.
        node = devfs / ("%03d" % bus) / ("%03d" % number)
        if node.exists() and not os.access(node, os.R_OK | os.W_OK):
            rules = shlex.quote(str(runtime / "rules/71-ti-permissions.rules"))
            return ("XDS110 USB 权限不足: %s；执行 sudo install -m 644 %s "
                    "/etc/udev/rules.d/71-ti-permissions.rules，"
                    "然后 sudo udevadm control --reload-rules，并重新插入 USB" % (node, rules))
    return ""


def usb_problem(system: str, root: Path, runtime: Path):
    if system == "windows":
        return _windows_usb_problem(root, _windows_usb_devices())
    return _linux_usb_problem(runtime)


def prepare_environment(runtime: Path, log):
    """Run once at production-script startup, before any probe/JTAG/Flash call."""
    runtime = Path(runtime).resolve()
    system = platform.system().lower()
    machine = platform.machine().lower()
    supported = {"windows": ("amd64", "x86_64"), "linux": ("amd64", "x86_64", "aarch64", "arm64")}
    if system not in supported or machine not in supported[system]:
        raise RuntimeError("启动环境仅支持 Windows x64、Linux x86_64/ARM64")
    if system == "linux":
        prepare_linux(runtime, log)
    root = runtime / "ti" if (runtime / "ti/ccs_base").is_dir() else runtime
    if system == "windows":
        check_windows_runtime(root)
        for name in ("DebugServer.dll", "lldbSM.dll", "Flash28xx.dll"):
            require_file(root / "ccs_base/DebugServer/bin" / name)
    suffix = ".exe" if system == "windows" else ""
    for name in ("ccs_base/DebugServer/bin/DSLite", "ccs_base/common/uscif/dbgjtag",
                 "ccs_base/common/uscif/xds110/xdsdfu"):
        require_file(root / (name + suffix))
    issue = usb_problem(system, root, runtime)
    if issue:
        raise RuntimeError(issue)
    log.info("STARTUP_DEPENDENCIES_READY system=%s runtime=%s", system, root)


def diagnose_error(error, tool):
    """Enrich startup errors from existing SDK logs, without changing SDK APIs."""
    message = str(error).split("；日志", 1)[0]
    if tool is None or not tool.log_path.is_file():
        return message
    try:
        with tool.log_path.open("rb") as source:
            source.seek(0, 2)
            source.seek(max(0, source.tell() - 32768))
            output = source.read().decode("utf-8", errors="replace")
    except OSError:
        return message
    exits = re.findall(r"EXIT tool=(\S+) rc=(-?\d+)", output)
    if exits:
        name, code = exits[-1]
        if int(code) not in (0, 1):
            marker = "[%s] " % name
            native_output = "\n".join(line.split(marker, 1)[1] for line in output.splitlines() if marker in line)
            return message + "；" + native_failure(name, int(code), native_output)
    return message


def native_failure(tool: str, code: int, output: str):
    """Keep a useful native diagnostic in the console as well as the full log."""
    status = code & 0xFFFFFFFF
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    detail = next((line for line in lines if re.search(r"error|failed|denied|not found|cannot|GLIBC|LIBUSB", line, re.I)),
                  lines[-1] if lines else "未返回诊断信息")[:500]
    if status == 0xC0000135:
        remedy = "缺少 DLL；检查依赖是否完整，并安装 VC++ x64 运行库 " + VC_REDIST
    elif status == 0xC000007B:
        remedy = "程序或 DLL 架构不匹配；使用完整 Windows x64 依赖"
    elif status == 0xC0000142:
        remedy = "DLL 初始化失败；检查 VC++ x64 运行库和安全软件拦截"
    elif re.search(r"LIBUSB_ERROR_ACCESS|permission denied|access denied|insufficient permissions", output, re.I):
        remedy = "检查 XDS110 USB 驱动或 Linux udev 权限，以及其他程序是否占用烧录器"
    elif re.search(r"GLIBC|shared librar|shared object|ld-linux", output, re.I):
        remedy = "Linux 运行库不完整或版本冲突；检查包内 rootfs 和动态库，不要混用不同版本"
    else:
        remedy = "检查依赖文件、系统架构和安全软件拦截，完整输出见日志"
    return "%s 执行失败，返回码 %s (0x%08X): %s；%s" % (tool, code, status, detail, remedy)
