#!/usr/bin/env python3
"""Continuous XDS110/F28035 production flashing. Windows and Linux share this file."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time

sys.dont_write_bytecode = True
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if sys.version_info < (3, 10):
    raise SystemExit("需要 Python 3.10 或更新版本。")

if __package__ and __package__.endswith(".scripts"):
    from ..sdk import EnvironmentError, Image, Result, State, TIFlash
else:
    sys.path.insert(0, str(PROJECT_ROOT))
    from sdk import EnvironmentError, Image, Result, State, TIFlash

if __package__:
    from ._environment import prepare_environment, diagnose_error
else:
    from _environment import prepare_environment, diagnose_error


# Batch settings. C28x addresses below are 16-bit WORD addresses.
BOOT_FILENAME = "ADM32F036_mc_boot_released_fw_237.bin"
BOOT_ADDRESS = 0x3F6000
APP_FILENAME = "ADM32F036_mc_app_released_fw_245.bin"
APP_ADDRESS = 0x3E8000
POLL_INTERVAL = 0.05
ERROR_RETRY_INTERVAL = 2.0
DISCONNECT_HOLD_SECONDS = 0.1
DISCONNECT_MIN_SAMPLES = 2


def firmware_images():
    return (
        Image(PROJECT_ROOT / "firmware" / BOOT_FILENAME, BOOT_ADDRESS),
        Image(PROJECT_ROOT / "firmware" / APP_FILENAME, APP_ADDRESS),
    )


def colored_tag(tag):
    colors = {"成功": "32", "失败": "31"}
    if tag not in colors or not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return tag
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetStdHandle.argtypes = [wintypes.DWORD]
            kernel.GetStdHandle.restype = wintypes.HANDLE
            kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            handle = kernel.GetStdHandle(-11)
            mode = wintypes.DWORD()
            if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                return tag
            if not (mode.value & 0x0004) and not kernel.SetConsoleMode(handle, mode.value | 0x0004):
                return tag
        except (OSError, AttributeError):
            return tag
    return "\033[%sm%s\033[0m" % (colors[tag], tag)


def emit(tag, message):
    print("[%s] [%s] %s" % (time.strftime("%H:%M:%S"), colored_tag(tag), message), flush=True)


def short_error(error):
    # Log paths change between tool instances; do not treat that as a new fault.
    return str(error).split("；日志", 1)[0].replace("\r", " ").replace("\n", " ")


@dataclass(frozen=True)
class FlashEvent:
    kind: str
    message: str
    attempt: int
    successes: int
    failures: int
    result: Result | None = None


@dataclass(frozen=True)
class RunSummary:
    attempts: int
    successes: int
    failures: int
    reason: str
    last_result: Result | None
    log_path: Path | None


CONSOLE_TAGS = {
    "started": "启动", "environment": "环境", "firmware": "固件", "log": "日志",
    "probe_missing": "等待", "probe_connected": "连接", "probe_multiple": "等待",
    "target_removed": "状态",
    "operator": "操作", "flash_started": "开始", "progress": "进度",
    "success": "成功", "failure": "失败", "interrupted": "中断",
    "statistics": "统计", "error": "异常", "busy": "等待", "stopped": "退出",
}


def console_event(event):
    if event.kind == "environment":
        return  # Dependency checks are silent on success; retained in the log/API.
    if event.kind == "cycle_finished":
        print(flush=True)
        return
    emit(CONSOLE_TAGS.get(event.kind, event.kind), event.message)


class StationLock:
    """Only one production script may own the station; no files in the project."""

    def __init__(self):
        self.file = None

    def acquire(self):
        if self.file is not None:
            return True
        file = (Path(tempfile.gettempdir()) / "ti_flash_production.lock").open("a+b")
        try:
            if file.tell() == 0:
                file.write(b"0")
                file.flush()
            file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            file.close()
            if error.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
        self.file = file
        return True

    def close(self):
        if self.file is not None:
            # Closing the handle releases the OS lock, including after a crash.
            self.file.close()
            self.file = None


class ProductionFlasher:
    """Reusable station state machine; poll_once() never starts a background loop."""

    def __init__(self, *, images=None, factory=TIFlash.from_config,
                 clock=time.monotonic, on_event=None, stop_event=None, persistent=True):
        if not isinstance(persistent, bool):
            raise ValueError("persistent 必须为 true 或 false")
        self.factory, self.clock, self.on_event = factory, clock, on_event
        self.persistent = persistent
        self.stop_event = stop_event
        self.images = tuple(images) if images is not None else firmware_images()
        self.roles = (("Boot", "App") if images is None else
                      tuple("固件 %d" % (i + 1) for i in range(len(self.images))))
        self.tool = None
        self.prepared = False
        self.manifest = None
        self.last_probes = None
        self.contact_prompt_pending = False
        self.last_error = None
        self.wait_disconnect = False
        self.disconnect_since = None
        self.disconnect_samples = 0
        self.burning = False
        self.last_phase = None
        self.last_result = None
        self.attempts = self.successes = self.failures = 0

    def say(self, kind, message, result=None):
        if self.tool is not None:
            self.tool.log.info("PRODUCTION [%s] %s", kind, message)
        if self.on_event is not None:
            event = FlashEvent(kind, message, self.attempts, self.successes, self.failures, result)
            try:
                self.on_event(event)
            except Exception:
                # A display/consumer callback must not turn a verified flash into failure.
                if self.tool is not None:
                    self.tool.log.exception("PRODUCTION_EVENT_CALLBACK_FAILED kind=%s", kind)

    def stopping(self):
        return self.stop_event is not None and self.stop_event.is_set()

    def prepare(self):
        if self.prepared:
            return
        if self.tool is None:
            self.tool = self.factory(progress=self.progress)
        self.tool._open_log()
        prepare_environment(self.tool.runtime, self.tool.log)
        self.tool.check()  # SDK operation stays a software-only tool check.
        plan = self.tool.plan(self.images)
        self.manifest = plan["images"]
        self.prepared = True
        self.say("environment", "工具和固件检查通过，目标 XDS110 / F28035。")
        for role, entry in zip(self.roles, self.manifest):
            self.say("firmware", "%s: %s | %s | %g KiB | SHA256 %s" % (
                role, Path(entry["path"]).name, entry["address"],
                entry["bytes"] / 1024, entry["sha256"][:12]))
        self.say("log", str(self.tool.log_path))

    def progress(self, line):
        text = line.lower()
        initializing = (text.startswith("configuring") or text.startswith("initializing")
                        or text.startswith("executing startup scripts"))
        if not self.burning and not initializing:
            return
        if text.startswith("initializing flash environment"):
            # The SDK emits this once per actual configuration/rebuild.
            self.last_phase = None
        # Only the SDK's final return-code + verification check can report success.
        if "success" in text:
            return
        if initializing:
            phase, message = "initialize", "正在初始化烧录环境……"
        elif "connecting" in text:
            phase, message = "connect", "正在连接目标芯片……"
        elif "verif" in text:
            phase, message = "verify", "正在校验固件……"
        elif "eras" in text:
            phase, message = "erase", "正在擦除 Flash……"
        elif "prepar" in text and self.last_phase == "verify":
            return
        elif "loading program" in text or "prepar" in text:
            phase, message = "prepare", "正在准备固件……"
        elif re.match(r"\d+\s+of\s+\d+\s+at\s+0x[0-9a-f]+", text):
            if self.last_phase not in ("erase", "write"):
                return
            phase, message = "write", "正在写入固件……"
        elif "loading" in text or "writing" in text or "programming" in text:
            phase, message = "write", "正在写入固件……"
        else:
            return
        if phase != self.last_phase:
            self.last_phase = phase
            self.say("progress", message)

    def forget_disconnect(self):
        self.disconnect_since = None
        self.disconnect_samples = 0

    def observe_disconnect(self, disconnected):
        if not self.wait_disconnect:
            return
        if not disconnected:
            self.forget_disconnect()
            return
        now = self.clock()
        if self.disconnect_since is None:
            self.disconnect_since = now
        self.disconnect_samples += 1
        if (self.disconnect_samples >= DISCONNECT_MIN_SAMPLES
                and now - self.disconnect_since >= DISCONNECT_HOLD_SECONDS):
            self.wait_disconnect = False
            self.forget_disconnect()
            self.say("target_removed", "探针已拔出")
            if self.last_probes and len(self.last_probes) == 1:
                self.contact_prompt_pending = True

    def announce_probes(self, probes):
        if probes == self.last_probes:
            return
        self.last_probes = probes
        self.contact_prompt_pending = False
        if not probes:
            self.say("probe_missing", "未检测到 XDS110，请检查 USB 是否接入。")
        elif len(probes) == 1:
            self.say("probe_connected", "已检测到 XDS110，序列号：%s。" % probes[0])
            if self.wait_disconnect:
                self.say("operator", "等待拔出探针，开始下一次烧录")
            else:
                self.contact_prompt_pending = True
        else:
            self.say("probe_multiple", "检测到 %d 个 XDS110，请只保留一个。" % len(probes))

    def poll_once(self):
        if self.stopping():
            return
        self.prepare()
        if self.stopping():
            return
        if self.persistent:
            self.tool.start_session()  # Idempotent; no target connection here.
        probes = tuple(sorted(self.tool.probes()))
        self.announce_probes(probes)
        if not probes:
            self.observe_disconnect(True)
            return
        if len(probes) != 1:
            self.observe_disconnect(False)
            return
        if self.stopping():
            return
        if self.persistent and not self.wait_disconnect:
            # Warm up as soon as USB is ready, including before board contact.
            # Only the initialization phase is shown before an active flash.
            # After failure, confirm removal before spending time rebuilding.
            if self.tool.initialize(probes=probes):
                self.contact_prompt_pending = True
            if self.stopping():
                return
        if self.contact_prompt_pending and not self.wait_disconnect:
            self.contact_prompt_pending = False
            self.say("operator", "请将探针接触待烧录板")
        detection = self.tool.detect(probes=probes)
        if detection.state is State.NO_PROBE:
            self.announce_probes(())
            self.observe_disconnect(True)
        elif detection.state is State.WAIT_BOARD:
            self.observe_disconnect(True)
        elif detection.state is State.READY:
            self.observe_disconnect(False)
            if not self.wait_disconnect and not self.stopping():
                self.flash_once()
        else:
            # UNKNOWN and runtime errors cannot count as confirmed removal.
            self.observe_disconnect(False)

    def flash_once(self):
        if self.tool.plan(self.images)["images"] != self.manifest:
            raise EnvironmentError("固件文件已改变，请恢复原文件或重启脚本确认新批次")
        if self.stopping():
            return
        # Close the gate BEFORE programming: even a timeout must require removal.
        self.wait_disconnect = True
        self.forget_disconnect()
        self.attempts += 1
        self.last_phase = None
        self.last_result = None
        self.say("flash_started", "探针已接触，开始第 %d 次烧录。" % self.attempts)
        started = self.clock()
        self.burning = True
        try:
            self.last_result = self.tool.flash(self.images)
        except KeyboardInterrupt:
            self.failures += 1
            self.say("interrupted", "本次烧录被用户中断，当前板需要重新烧录。")
            raise
        except Exception as error:
            self.failures += 1
            self.tool.log.exception("PRODUCTION_FLASH_FAILED attempt=%s", self.attempts)
            self.say("failure", "第 %d 次烧录失败，耗时 %.1f s。%s" % (
                self.attempts, self.clock() - started, short_error(error)))
            self.say("log", str(self.tool.log_path))
        else:
            self.successes += 1
            self.say("success", "耗时 %.1f s" % (self.clock() - started), self.last_result)
        finally:
            self.burning = False
        self.say("statistics", "成功 %d 次，失败 %d 次。" % (self.successes, self.failures))
        self.say("operator", "等待拔出探针，开始下一次烧录")
        self.say("cycle_finished", "本轮烧录结束", self.last_result)

    def report_error(self, error):
        self.forget_disconnect()
        key = (type(error).__name__, short_error(diagnose_error(error, self.tool)))
        if key != self.last_error:
            self.last_error = key
            self.say("error", "%s；修复后继续等待，Ctrl+C 退出。" % key[1])
            if self.tool is not None and self.tool.log_path.is_file():
                self.say("log", str(self.tool.log_path))
        if not self.prepared and self.tool is not None:
            # Reload config on the next attempt, without resetting the removal gate.
            self.tool.close()
            self.tool = None

    def close(self):
        if self.tool is not None:
            self.tool.close()


def run(*, images=None, config=None, stop_event=None, on_event=None, max_attempts=None,
        persistent=True):
    """Run the station and return structured results; silent unless on_event is given.

    max_attempts=1 waits for a board, flashes once, then returns even on failure.
    stop_event is a threading.Event: stop waiting immediately, or finish an active
    flash+verify before stopping. KeyboardInterrupt aborts via the SDK instead.
    Event callbacks notify only; progress events may come from the SDK reader
    thread. Use stop_event, not callback exceptions, to request a graceful stop.
    """
    if max_attempts is not None and (isinstance(max_attempts, bool)
                                    or not isinstance(max_attempts, int) or max_attempts < 1):
        raise ValueError("max_attempts 必须为正整数或 None")
    stop_event = stop_event if stop_event is not None else threading.Event()
    station = ProductionFlasher(images=images, stop_event=stop_event, on_event=on_event,
                                persistent=persistent,
                                factory=lambda **kwargs: TIFlash.from_config(config, **kwargs))
    ownership = StationLock()
    station.say("started", "XDS110 / F28035 连续烧录，按 Ctrl+C 退出。")
    busy_reported = False
    reason = "stop_requested"
    try:
        while not stop_event.is_set():
            try:
                if not ownership.acquire():
                    if not busy_reported:
                        station.say("busy", "另一个自动烧录脚本正在运行；请关闭重复窗口，或等待其退出。")
                        busy_reported = True
                    # Taking over after another process must also see a removal.
                    station.wait_disconnect = True
                    stop_event.wait(ERROR_RETRY_INTERVAL)
                    continue
                station.poll_once()
                station.last_error = None
                if max_attempts is not None and station.attempts >= max_attempts:
                    reason = "limit_reached"
                    break
                delay = POLL_INTERVAL
            except Exception as error:
                station.report_error(error)
                delay = ERROR_RETRY_INTERVAL
            stop_event.wait(delay)
    except KeyboardInterrupt:
        reason = "keyboard_interrupt"
    finally:
        try:
            station.say("stopped", "已停止。累计成功 %d 次，失败 %d 次。" % (station.successes, station.failures))
        finally:
            try:
                station.close()
            finally:
                ownership.close()
    return RunSummary(station.attempts, station.successes, station.failures, reason,
                      station.last_result, station.tool.log_path if station.tool is not None else None)


def main(argv=None):
    parser = argparse.ArgumentParser(description="XDS110 / F28035 连续烧录；Ctrl+C 退出")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="只做启动准备、软件和系统 USB 驱动/权限检查；成功静默，不打开探针、不访问 JTAG、不烧录")
    mode.add_argument("--once", action="store_true",
                      help="等待一块板并只烧录一次；成功返回 0，失败返回 1，Ctrl+C 返回 130")
    parser.add_argument("--no-session", action="store_true",
                        help="关闭常驻服务，每次烧录使用独立的 TI CLI 进程")
    args = parser.parse_args(argv)
    if args.check:
        station = ProductionFlasher()
        try:
            station.prepare()
            return 0
        except Exception as error:
            if station.tool is not None:
                station.tool.log.exception("ENVIRONMENT_CHECK_FAILED")
            emit("失败", short_error(diagnose_error(error, station.tool)))
            if station.tool is not None and station.tool.log_path.is_file():
                emit("日志", str(station.tool.log_path))
            return 1
        except KeyboardInterrupt:
            emit("退出", "已停止检查。")
            return 130
        finally:
            station.close()
    result = run(on_event=console_event, max_attempts=1 if args.once else None,
                 persistent=not args.no_session)
    if result.reason == "keyboard_interrupt":
        return 130
    return 1 if result.failures else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    raise SystemExit(main())
