"""Small XDS110/F28035 API. Python standard library; Windows/Linux share this file."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import tempfile
import threading
import time
from typing import Callable, Iterable
from xml.etree import ElementTree as ET


class TIError(RuntimeError):
    """An operation failed; the complete TI output is retained in the log."""


class EnvironmentError(TIError):
    """Missing/broken runtime, permissions or configuration; retrying contact is insufficient."""


class FlashError(TIError):
    """Programming/verification failed, or the target state became unknown."""


class State(str, Enum):
    NO_PROBE = "NO_PROBE"
    WAIT_BOARD = "WAIT_BOARD"
    READY = "READY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Detection:
    state: State
    detail: str = ""

    @property
    def ready(self) -> bool:
        return self.state is State.READY


@dataclass(frozen=True)
class Image:
    path: Path | str
    address: int | None = None  # C28x 16-bit word address, NOT a byte offset.


@dataclass(frozen=True)
class Result:
    returncode: int
    output: str
    seconds: float
    log: Path


def _integer(value: int | str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise EnvironmentError("地址必须是整数或十六进制字符串")
    try:
        return int(value, 0) if isinstance(value, str) else int(value)
    except (ValueError, TypeError) as exc:
        raise EnvironmentError("无效地址: " + str(value)) from exc


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else hashlib.sha256(source.read()).hexdigest()


class TIFlash:
    """One XDS110, one F28035. Constructing/importing never touches the target.

    A runtime is either a TI CLI export (ccs_base/...), or the supplied Linux
    bundle (ti/ccs_base, rootfs, bin and compat). No driver/firmware installation.
    """

    def __init__(self, runtime: str | Path, target: str | Path, *,
                 images: Iterable[Image] = (), logs: str | Path | None = None,
                 timeout: float = 30, flash_timeout: float = 300,
                 run_after_flash: bool = True,
                 progress: Callable[[str], None] | None = None):
        self.runtime = Path(runtime).expanduser().resolve()
        self.target = Path(target).expanduser().resolve()
        self.images = tuple(images)
        self.timeout, self.flash_timeout = float(timeout), float(flash_timeout)
        if not all(math.isfinite(x) and x > 0 for x in (self.timeout, self.flash_timeout)):
            raise EnvironmentError("timeout 和 flash_timeout 必须大于零")
        if not isinstance(run_after_flash, bool):
            raise EnvironmentError("run_after_flash 必须为 true 或 false")
        self.run_after_flash, self.progress = run_after_flash, progress
        log_dir = Path(logs).expanduser().resolve() if logs is not None else Path(tempfile.gettempdir()) / "ti_flash_logs"
        self.log_path = log_dir / ("ti_flash_%s_%s.log" % (time.strftime("%Y%m%d_%H%M%S"), os.getpid()))
        self.log = logging.Logger("ti_flash.%s" % id(self), logging.DEBUG)
        self._handler = None
        self._thread_lock = threading.Lock()
        self._process_lock = None
        self._session_enabled = False
        self._session = None
        self._session_probes = None

    @classmethod
    def from_config(cls, config: str | Path | None = None, **overrides) -> TIFlash:
        """All paths are relative to config.json, independent of the working directory."""
        path = Path(config).expanduser().resolve() if config is not None else Path(__file__).with_name("config.json")
        try:
            settings = json.loads(path.read_text(encoding="utf-8-sig"))
            def resolve(value):
                return (path.parent / Path(value).expanduser()).resolve()
            system = platform.system().lower()
            runtime = settings["runtime"]
            runtime = runtime[system] if isinstance(runtime, dict) else runtime
            images = [Image(resolve(x["path"]), _integer(x["address"]) if x.get("address") is not None else None)
                      for x in settings.get("images", [])]
            options = dict(images=images, logs=resolve(settings["logs"]) if settings.get("logs") else None,
                           timeout=settings.get("timeout", 30), flash_timeout=settings.get("flash_timeout", 300),
                           run_after_flash=settings.get("run_after_flash", True))
            options.update(overrides)
            return cls(resolve(runtime), resolve(settings["target"]), **options)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise EnvironmentError("配置无法加载: %s: %s" % (path, exc)) from exc

    def close(self):
        if self._session_enabled or self._session is not None:
            self.end_session()
        if self._handler:
            self._handler.close()
            self.log.removeHandler(self._handler)
            self._handler = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def start_session(self):
        """Keep a private TI service and exclusive probe lease until end_session().

        Starts the software service only. The first flash configures the debugger;
        later boards reuse that configuration and connect/disconnect individually.
        """
        with self._exclusive():
            if self._session is not None and not self._session.healthy:
                self._discard_session()
            if self._session is None:
                self._open_log()
                from ._session import _DSService
                self._session = _DSService(self)
            self._session_enabled = True
        return self

    def _discard_session(self):
        """Caller owns _exclusive(); an uncertain transaction is never resubmitted."""
        try:
            if self._session is not None:
                self._session.close()
        finally:
            self._session = None
            self._session_probes = None

    def initialize(self, *, probes: Iterable[str] | None = None) -> bool:
        """Preconfigure the sole XDS110; no CPU connection or Flash operation.

        Enables the persistent service. Returns True only when configuration was
        created/refreshed; repeated calls reuse it. Close/end_session releases it.
        probes may be the fresh probes() snapshot from this same polling cycle.
        """
        self.start_session()
        with self._exclusive():
            probes = self._probe_snapshot(probes)
            if len(probes) != 1:
                raise EnvironmentError("初始化要求只连接一个 XDS110，检测到 %s 个" % len(probes))
            try:
                if self._session is None:
                    from ._session import _DSService
                    self._session = _DSService(self)
                service = self._session
                self._session_probes = probes
                initialized = service.initialize()
                if initialized and (tuple(self._probes()) != probes or self._session is not service):
                    raise EnvironmentError("初始化期间 XDS110 连接发生变化")
                return initialized
            except BaseException as error:
                self.log.exception("SESSION_INITIALIZE_FAILED; no target programming requested")
                self._discard_session()
                if not isinstance(error, (EnvironmentError, KeyboardInterrupt, SystemExit)):
                    raise EnvironmentError("初始化烧录环境失败: %s；日志 %s" % (error, self.log_path)) from error
                raise

    def end_session(self):
        """Stop only this instance's TI service and release its exclusive lease."""
        with self._exclusive():
            try:
                self._discard_session()
            finally:
                self._session_enabled = False

    @contextmanager
    def session(self):
        """Use the same TI service for successive flash() calls on either OS."""
        already_enabled = self._session_enabled
        self.start_session()
        try:
            yield self
        finally:
            if not already_enabled:
                self.end_session()

    def _layout(self):
        system, machine = platform.system().lower(), platform.machine().lower()
        if system not in ("windows", "linux"):
            raise EnvironmentError("只支持 Windows 和 Linux")
        if system == "windows" and machine not in ("amd64", "x86_64"):
            raise EnvironmentError("Windows 后端需要 x64 主机")
        if system == "linux" and machine not in ("x86_64", "amd64", "aarch64", "arm64"):
            raise EnvironmentError("Linux 后端需要 x86_64 或 ARM64 主机")
        root = self.runtime / "ti" if (self.runtime / "ti/ccs_base").is_dir() else self.runtime
        suffix = ".exe" if system == "windows" else ""
        files = {"dslite": root / ("ccs_base/DebugServer/bin/DSLite" + suffix),
                 "dbgjtag": root / ("ccs_base/common/uscif/dbgjtag" + suffix),
                 "xdsdfu": root / ("ccs_base/common/uscif/xds110/xdsdfu" + suffix)}
        return system, machine, root, files

    def _validate(self):
        system, machine, root, files = self._layout()
        for name, file in files.items():
            if not file.is_file():
                raise EnvironmentError("缺少 %s: %s；Linux 包首次使用先执行 python3 dependencies/setup_linux.py" % (name, file))
            with file.open("rb") as binary:
                magic = binary.read(20)
            if system == "linux" and (magic[:4] != b"\x7fELF" or magic[4:6] != b"\x02\x01" or int.from_bytes(magic[18:20], "little") != 62):
                raise EnvironmentError("需要 Linux x86_64 TI 程序，不能混用 Windows EXE: " + str(file))
            if system == "windows" and magic[:2] != b"MZ":
                raise EnvironmentError("需要 Windows TI 程序: " + str(file))
        required = ("connections/TIXDS110_Connection.xml", "devices/f28035.xml",
                    "drivers/tixds510c28x.xml", "drivers/tixds510cla.xml",
                    "drivers/TI_reg_ids/TMS320C28XX_regids.xml",
                    "drivers/TI_reg_ids/TMS192C2006_regids.xml")
        for name in required:
            if not (root / "ccs_base/common/targetdb" / name).is_file():
                raise EnvironmentError("TI 运行库不完整: " + name)
        try:
            target = ET.parse(self.target)
        except (OSError, ET.ParseError) as exc:
            raise EnvironmentError("目标配置无法读取: " + str(self.target)) from exc
        connections = target.findall(".//connection")
        devices = [n.get("href", "").replace("\\", "/") for n in target.iter("instance") if n.get("xmlpath", "").strip("/") == "devices"]
        if len(connections) != 1 or devices != ["devices/f28035.xml"]:
            raise EnvironmentError("本工具需要单个 XDS110 / F28035 CCXML")
        if not any(n.get("href", "").replace("\\", "/") == "connections/TIXDS110_Connection.xml" for n in target.iter("instance")):
            raise EnvironmentError("目标配置不是 XDS110")
        if system == "linux" and machine in ("aarch64", "arm64"):
            if not (self.runtime / "bin/qemu-x86_64-static").is_file() or not (self.runtime / "rootfs/lib64/ld-linux-x86-64.so.2").is_file():
                raise EnvironmentError("ARM64 需要已解压的 QEMU + AMD64 rootfs 运行包")
        return system, machine, root, files

    def _command(self, tool: str, args: list[str]):
        system, machine, root, files = self._validate()
        env = os.environ.copy()
        libraries = [root / p for p in ("ccs_base/DebugServer/bin", "ccs_base/DebugServer/drivers", "ccs_base/common/bin", "ccs_base/common/uscif")]
        prefix = []
        if system == "windows":
            env["PATH"] = os.pathsep.join(str(x) for x in libraries if x.is_dir()) + os.pathsep + env.get("PATH", "")
        else:
            sysroot = self.runtime / "rootfs"
            libraries += [sysroot / p for p in ("lib/x86_64-linux-gnu", "usr/lib/x86_64-linux-gnu", "lib64")]
            library_path = ":".join(str(x) for x in libraries if x.is_dir())
            env["LC_ALL"] = "C"
            shim = self.runtime / "compat/libdslite_exit.so"
            # The supplied 9.3 Linux bundle retains the previously validated exit workaround.
            if machine in ("aarch64", "arm64"):
                prefix = [str(self.runtime / "bin/qemu-x86_64-static"), "-L", str(sysroot), "-E", "LD_LIBRARY_PATH=" + library_path]
                if tool == "dslite" and shim.is_file():
                    prefix += ["-E", "LD_PRELOAD=" + str(shim)]
            else:
                loader = sysroot / "lib64/ld-linux-x86-64.so.2"
                if loader.is_file():
                    prefix = [str(loader)]
                env["LD_LIBRARY_PATH"] = library_path
                if tool == "dslite" and shim.is_file():
                    env["LD_PRELOAD"] = str(shim)
        return prefix + [str(files[tool]), *args], env, root

    @contextmanager
    def _exclusive(self):
        # Kernel locks are released on process exit; no stale PID-file cleanup.
        if not self._thread_lock.acquire(blocking=False):
            raise TIError("同一实例正在执行另一个 TI 操作")
        try:
            if self._process_lock is None:
                lock = (Path(tempfile.gettempdir()) / "ti_flash_xds110.lock").open("a+b")
                try:
                    if lock.tell() == 0:
                        lock.write(b"0")
                        lock.flush()
                    lock.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    lock.close()
                    raise TIError("另一个进程正在使用 TI 烧录工具") from exc
                self._process_lock = lock
            yield
        finally:
            try:
                if self._process_lock is not None and not self._session_enabled:
                    lock, self._process_lock = self._process_lock, None
                    lock.seek(0)
                    try:
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                    finally:
                        lock.close()
            finally:
                self._thread_lock.release()

    def _open_log(self):
        if self._handler is None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._handler = RotatingFileHandler(self.log_path, maxBytes=2*1024*1024, backupCount=2, encoding="utf-8")
            self._handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self.log.addHandler(self._handler)

    def _progress_line(self, line):
        if self.progress and re.match(
                r"\s*(?:Configuring|Initializing|Executing Startup Scripts|Connecting|Preparing|Loading|Writing|Programming|Verifying|Erasing|"
                r"Program |Flash |Running|Success|\d+\s+of\s+\d+\s+at\s+0x[0-9a-f]+)", line, re.I):
            try:
                self.progress(line.strip())
            except Exception:
                self.log.exception("progress callback failed")

    def _run(self, tool: str, args: list[str], timeout: float | None = None) -> Result:
        command, env, cwd = self._command(tool, args)
        self._open_log()
        self.log.info("EXEC %s", json.dumps(command, ensure_ascii=False))
        lines = []
        start = time.monotonic()
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        try:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **options)
        except OSError as exc:
            raise EnvironmentError("%s 启动失败: %s；日志 %s" % (tool, exc, self.log_path)) from exc
        def read_output():
            with process.stdout:
                for raw in iter(process.stdout.readline, b""):
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    lines.append(line)
                    self.log.info("[%s] %s", tool, line)
                    if tool == "dslite":
                        self._progress_line(line)
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        try:
            try:
                rc = process.wait(timeout=timeout or self.timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                if os.name == "nt":
                    subprocess.run([str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"), "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                reader.join(timeout=5)
                if isinstance(exc, KeyboardInterrupt):
                    raise
                raise TIError("%s 超时，目标状态未知；日志 %s" % (tool, self.log_path)) from exc
            reader.join(timeout=5)
            if reader.is_alive():
                raise EnvironmentError("TI 子进程退出后输出管道仍未关闭，停止继续访问目标")
            output = "\n".join(lines)
            elapsed = time.monotonic() - start
            self.log.info("EXIT tool=%s rc=%s seconds=%.3f", tool, rc, elapsed)
            return Result(rc, output, elapsed, self.log_path)
        finally:
            self.log.info("FINISH tool=%s seconds=%.3f", tool, time.monotonic() - start)

    @staticmethod
    def _runtime_error(result: Result):
        if result.returncode < 0 or result.returncode > 0xFFFF or re.search(
                r"No regids xml file exists|error while loading shared libraries|cannot open shared object|"
                r"exec format error|segmentation fault|uncaught target signal|permission denied|"
                r"GLIBC[^\n]*not found|could not open.*ld-linux|cannot find[^\n]*\.(?:xml|gel)", result.output, re.I):
            raise EnvironmentError("TI 运行库或 USB 权限异常；日志 " + str(result.log))

    def check(self) -> dict:
        """Validate files, target XML and tool startup; no JTAG or Flash access."""
        with self._exclusive():
            system, machine, root, files = self._validate()
            for tool, args in (("dslite", ["flash", "--help"]), ("dbgjtag", ["-h"]), ("xdsdfu", [])):
                result = self._run(tool, args)
                self._runtime_error(result)
                if result.returncode not in (0, 1) or not result.output.strip():
                    raise EnvironmentError("%s 自检失败；日志 %s" % (tool, result.log))
            return {"system": system, "machine": machine, "runtime": str(root), "target": str(self.target), "log": str(self.log_path)}

    def _probes(self) -> list[str]:
        result = self._run("xdsdfu", ["-e"])
        self._runtime_error(result)
        found = re.search(r"Found\s+(\d+)\s+device", result.output, re.I)
        if result.returncode != 0 or not found:
            raise EnvironmentError("XDS110 枚举失败；日志 " + str(result.log))
        count = int(found.group(1))
        serials = re.findall(r"Serial\s+(?:Num(?:ber)?|No\.?)\s*[:=]\s*(\S+)", result.output, re.I)
        probes = serials if len(serials) == count else ["unknown-%s" % (i+1) for i in range(count)]
        self._observe_probes(probes)
        return probes

    def _observe_probes(self, probes):
        if (self._session is not None and self._session.configured
                and tuple(probes) != self._session_probes):
            self.log.info("SESSION_PROBE_CHANGED previous=%s current=%s", self._session_probes, probes)
            self._discard_session()

    def _probe_snapshot(self, probes):
        if probes is None:
            return tuple(self._probes())
        if isinstance(probes, (str, bytes)):
            raise EnvironmentError("probes 必须是本轮 probes() 返回的序列号列表")
        try:
            snapshot = tuple(probes)
        except TypeError as error:
            raise EnvironmentError("probes 必须是本轮 probes() 返回的序列号列表") from error
        if any(not isinstance(serial, str) or not serial for serial in snapshot):
            raise EnvironmentError("probes 中的序列号必须是非空字符串")
        self._observe_probes(snapshot)
        return snapshot

    def probes(self) -> list[str]:
        """List USB probe serial numbers. Does not access the target's JTAG."""
        with self._exclusive():
            return self._probes()

    @staticmethod
    def _scan_state(result: Result) -> Detection:
        TIFlash._runtime_error(result)
        text = result.output.lower()
        failed = bool(re.search(r"has failed|failed:\s*[1-9]|values were corrupted|stuck-at-", text))
        if failed and "succeeded" in text:
            return Detection(State.UNKNOWN, "部分 JTAG 测试失败")
        ir_open = re.search(r"jtag\s+ir\b[^\n]*\bstuck-at-(?:ones|zeros)\b", text)
        dr_open = re.search(r"jtag\s+dr\b[^\n]*\bstuck-at-(?:ones|zeros)\b", text)
        if ir_open and dr_open:
            return Detection(State.WAIT_BOARD, "IR/DR 链路均为固定电平")
        ir_ok = re.search(r"jtag ir[^\n]*\bsucceeded\b", text)
        dr_ok = re.search(r"jtag dr[^\n]*\bsucceeded\b", text)
        if result.returncode == 0 and not failed and ir_ok and dr_ok:
            return Detection(State.READY, "JTAG IR/DR integrity 通过")
        if re.search(r"SC_ERR_PATH_BROKEN|SC_ERR_JTAG_OPEN|scan[- ]path.*broken|path[- ]length.*(?:zero|0 bits)|voltage[^\n]*(?:too low|absent|missing|out of range)", text, re.I):
            return Detection(State.WAIT_BOARD, "目标未上电或 JTAG 链路未连通")
        return Detection(State.UNKNOWN, "JTAG 返回未确认的结果，请查看日志")

    def detect(self, *, probes: Iterable[str] | None = None) -> Detection:
        """USB + IR/DR integrity, optionally reusing this cycle's probes() result.

        READY confirms a link, not chip identity. flash() always enumerates again.
        """
        with self._exclusive():
            probes = self._probe_snapshot(probes)
            if not probes:
                return Detection(State.NO_PROBE, "未检测到 XDS110")
            if len(probes) != 1:
                raise EnvironmentError("当前接口要求只连接一个 XDS110，检测到 %s 个" % len(probes))
            return self._scan_state(self._run("dbgjtag", ["-f", "@xds110", "-S", "integrity"]))

    def plan(self, images: Iterable[Image] | None = None) -> dict:
        """Resolve images and compile the exact command without invoking TI tools."""
        selected = tuple(self.images if images is None else images)
        if not selected:
            raise EnvironmentError("没有配置固件")
        args, manifest, ranges = [], [], []
        for image in selected:
            path = Path(image.path).expanduser().resolve()
            if not path.is_file() or path.stat().st_size == 0:
                raise EnvironmentError("固件不存在或为空: " + str(path))
            if "," in str(path):
                raise EnvironmentError("固件路径不能包含逗号: " + str(path))
            size = path.stat().st_size
            address = _integer(image.address) if image.address is not None else None
            if path.suffix.lower() == ".bin":
                if address is None or size % 2:
                    raise EnvironmentError("C28x BIN 需要明确的 word 地址，且字节数必须为偶数")
                end = address + size // 2
                if address < 0x3E8000 or end > 0x3F8000:
                    raise EnvironmentError("BIN 超出 F28035 Flash word 地址范围: " + str(path))
                for other_start, other_end in ranges:
                    if address < other_end and end > other_start:
                        raise EnvironmentError("固件在目标 Flash 中重叠")
                ranges.append((address, end))
                argument = str(path) + "," + hex(address)
            else:
                if path.suffix.lower() not in (".out", ".elf") or image.address is not None:
                    raise EnvironmentError("支持 BIN+地址，或包含自身地址的 OUT/ELF")
                argument = str(path)
            args.append(argument)
            manifest.append({"path": str(path), "bytes": size, "address": hex(address) if address is not None else None, "sha256": _digest(path)})
        options = ["flash", "--config=" + str(self.target), "-e", "-v", "-f", *args]
        if self.run_after_flash:
            options.append("-u")
        command, _, cwd = self._command("dslite", options)
        return {"command": command, "arguments": options, "cwd": str(cwd), "images": manifest, "run_requested": self.run_after_flash}

    def flash(self, images: Iterable[Image] | None = None) -> Result:
        """Program+verify once; raises on failure. Does not repeat the JTAG scan.

        -e is verbose, -f programs, -v verifies, -u requests execution.
        Erase policy is the TI device/default Flash configuration.
        """
        with self._exclusive():
            plan = self.plan(images)
            probes = self._probes()
            if len(probes) != 1:
                raise FlashError("烧录要求只连接一个 XDS110，检测到 %s 个" % len(probes))
            self.log.info("IMAGES %s", json.dumps(plan["images"], ensure_ascii=False))
            if self._session_enabled:
                try:
                    if self._session is None:
                        from ._session import _DSService
                        self._session = _DSService(self)
                    self._session_probes = tuple(probes)
                    return self._session.flash(plan)
                except BaseException as error:
                    # Never resubmit an uncertain flash. The caller must require
                    # removal; a new service may be created for the next board.
                    self.log.exception("SESSION_FLASH_FAILED; no automatic retry")
                    self._discard_session()
                    if not isinstance(error, (EnvironmentError, FlashError, KeyboardInterrupt, SystemExit)):
                        raise FlashError("常驻烧录/校验未通过: %s；日志 %s" % (error, self.log_path)) from error
                    raise
            self._progress_line("Initializing flash environment...")
            result = self._run("dslite", plan["arguments"], self.flash_timeout)
            self._runtime_error(result)
            if result.returncode != 0 or "Program verification successful" not in result.output:
                raise FlashError("烧录/校验未通过；日志 " + str(result.log))
            return result
