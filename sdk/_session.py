"""Private TI DSLite local-service transport (Python standard library only)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import signal
import socket
import struct
import subprocess
import threading
import time

from .core import EnvironmentError, FlashError, Result, TIError


class _RPC:
    """One loopback WebSocket module, using TI's id/command/data protocol."""

    def __init__(self, port, protocol=None, *, timeout=30, event=None):
        if not isinstance(port, int) or not 0 < port < 65536:
            raise EnvironmentError("TI 服务返回了无效端口")
        self.socket = socket.create_connection(("127.0.0.1", port), timeout)
        self.buffer = bytearray()
        self.condition = threading.Condition()
        self.send_lock = threading.Lock()
        self.responses = {}
        self.pending = set()
        self.failure = None
        self.last_activity = time.monotonic()
        self.event = event
        self.next_id = 0
        self.reader = None
        try:
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            headers = ["GET / HTTP/1.1", "Host: 127.0.0.1:%d" % port,
                       "Upgrade: websocket", "Connection: Upgrade",
                       "Sec-WebSocket-Key: " + key, "Sec-WebSocket-Version: 13"]
            if protocol:
                if any(c in str(protocol) for c in "\r\n"):
                    raise EnvironmentError("无效的 TI 服务子协议")
                headers.append("Sec-WebSocket-Protocol: " + str(protocol))
            self.socket.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
            while b"\r\n\r\n" not in self.buffer:
                if len(self.buffer) > 16384:
                    raise EnvironmentError("TI 服务握手响应过大")
                self.buffer.extend(self._receive())
            raw, remaining = bytes(self.buffer).split(b"\r\n\r\n", 1)
            self.buffer[:] = remaining
            lines = raw.decode("ascii").split("\r\n")
            response = dict((k.lower().strip(), v.strip()) for k, v in
                            (line.split(":", 1) for line in lines[1:] if ":" in line))
            expected = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
            if (len(lines[0].split()) < 2 or lines[0].split()[1] != "101"
                    or response.get("sec-websocket-accept") != expected
                    or response.get("upgrade", "").lower() != "websocket"
                    or "upgrade" not in response.get("connection", "").lower()
                    or (protocol and response.get("sec-websocket-protocol") != str(protocol))):
                raise EnvironmentError("TI 服务 WebSocket 握手失败")
            self.socket.settimeout(None)
            self.reader = threading.Thread(target=self._read, daemon=True)
            self.reader.start()
        except BaseException:
            self.close()
            raise

    def _receive(self):
        data = self.socket.recv(65536)
        if not data:
            raise TIError("TI 常驻服务连接已关闭")
        return data

    def _exact(self, size):
        while len(self.buffer) < size:
            self.buffer.extend(self._receive())
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def _send(self, data, opcode=1):
        mask = os.urandom(4)
        size = len(data)
        length = (bytes([0x80 | size]) if size < 126 else
                  b"\xfe" + struct.pack("!H", size) if size < 65536 else
                  b"\xff" + struct.pack("!Q", size))
        payload = bytes(value ^ mask[i % 4] for i, value in enumerate(data))
        with self.send_lock:
            self.socket.sendall(bytes([0x80 | opcode]) + length + mask + payload)

    def _read(self):
        fragments = bytearray()
        fragmented = False
        try:
            while True:
                first, second = self._exact(2)
                final, opcode = bool(first & 0x80), first & 15
                if first & 0x70 or second & 0x80:
                    raise TIError("TI 服务返回了无效 WebSocket 帧")
                size = second & 127
                if size == 126:
                    size = struct.unpack("!H", self._exact(2))[0]
                elif size == 127:
                    size = struct.unpack("!Q", self._exact(8))[0]
                if size > 16 * 1024 * 1024 or len(fragments) + size > 16 * 1024 * 1024:
                    raise TIError("TI 服务响应超过大小限制")
                if opcode >= 8 and (not final or size > 125):
                    raise TIError("TI 服务控制帧无效")
                payload = self._exact(size)
                if opcode == 8:
                    raise TIError("TI 常驻服务连接已关闭")
                if opcode == 9:
                    self._send(payload, 10)
                    continue
                if opcode == 10:
                    continue
                if opcode == 1 and not fragmented:
                    fragments[:] = payload
                elif opcode == 0 and fragmented:
                    fragments.extend(payload)
                else:
                    raise TIError("TI 服务消息帧顺序无效")
                fragmented = not final
                if fragmented:
                    continue
                message = json.loads(fragments.decode("utf-8"))
                fragments.clear()
                if not isinstance(message, dict):
                    raise TIError("TI 服务响应格式无效")
                if "response" in message or "error" in message:
                    response_id = message.get("response", message.get("error"))
                    with self.condition:
                        if response_id in self.pending:
                            self.responses[response_id] = message
                            self.condition.notify_all()
                elif self.event:
                    self.event(message)
        except BaseException as error:
            self.abort(error)

    def abort(self, error):
        """Wake an RPC even when the fatal error arrived on another module."""
        with self.condition:
            if self.failure is None:
                self.failure = error
            self.condition.notify_all()

    def touch(self):
        with self.condition:
            self.last_activity = time.monotonic()
            self.condition.notify_all()

    def call(self, command, *data, timeout=30, idle_timeout=None):
        self.next_id += 1
        request_id = self.next_id
        deadline = time.monotonic() + timeout
        with self.condition:
            if self.failure:
                if isinstance(self.failure, TIError):
                    raise self.failure
                raise TIError("TI 服务通信失败: " + str(self.failure)) from self.failure
            self.pending.add(request_id)
            self.last_activity = time.monotonic()
        try:
            self._send(json.dumps({"id": request_id, "command": command, "data": data},
                                  ensure_ascii=False).encode("utf-8"))
            with self.condition:
                while True:
                    if self.failure:
                        if isinstance(self.failure, TIError):
                            raise self.failure
                        raise TIError("TI 服务通信失败: " + str(self.failure)) from self.failure
                    if request_id in self.responses:
                        break
                    now = time.monotonic()
                    remaining = deadline - now
                    if remaining <= 0:
                        raise TIError("TI 服务命令超时，目标状态未知: " + command)
                    idle_remaining = (self.last_activity + idle_timeout - now
                                      if idle_timeout is not None else remaining)
                    if idle_remaining <= 0:
                        raise TIError("TI 操作连续 %g s 无进度，目标状态未知: %s" % (idle_timeout, command))
                    # Bounded waits also let Ctrl+C interrupt on Windows.
                    self.condition.wait(min(remaining, idle_remaining, 0.2))
                reply = self.responses.pop(request_id)
            if "error" in reply:
                error = reply.get("data", {})
                raise TIError(str(error.get("message", error) if isinstance(error, dict) else error))
            return reply.get("data")
        finally:
            with self.condition:
                self.pending.discard(request_id)
                self.responses.pop(request_id, None)

    def close(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()
        if self.reader and self.reader is not threading.current_thread():
            self.reader.join(timeout=2)


class _DSService:
    """Own exactly one DSLite child; never attach to someone else's service."""

    @property
    def healthy(self):
        return (self.process.poll() is None and self.root is not None
                and self.root.failure is None
                and (self.core is None or self.core.failure is None))

    def __init__(self, tool):
        self.tool = tool
        self.process = None
        self.reader = None
        self.root = self.core = None
        self.configured = False
        self.config_signature = None
        self.lines = []
        self.deadline = None
        self.async_error = None
        self.target_connected = False
        self.disconnect_expected = False
        self.progress_tokens = {}
        self.closing = False
        try:
            command, env, _ = tool._command("dslite", [])
            _, _, _, files = tool._layout()
            options = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt"
                       else {"start_new_session": True})
            tool.log.info("SESSION_START %s", json.dumps(command, ensure_ascii=False))
            self.process = subprocess.Popen(command, cwd=files["dslite"].parent, env=env,
                                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, **options)
            ready = queue.Queue()
            def output():
                try:
                    with self.process.stdout:
                        for raw in iter(self.process.stdout.readline, b""):
                            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                            tool.log.info("[service] %s", line)
                            try:
                                data = json.loads(line)
                                if isinstance(data, dict) and "port" in data:
                                    ready.put(data["port"])
                            except ValueError:
                                pass
                finally:
                    ready.put(None)
            self.reader = threading.Thread(target=output, daemon=True)
            self.reader.start()
            try:
                port = ready.get(timeout=tool.timeout)
            except queue.Empty as exc:
                raise EnvironmentError("TI 常驻服务启动超时") from exc
            if port is None:
                raise EnvironmentError("TI 常驻服务启动失败；日志 " + str(tool.log_path))
            self.root = _RPC(port, timeout=tool.timeout, event=self._event)
        except BaseException as error:
            self.close()
            if isinstance(error, (OSError, ValueError)):
                raise EnvironmentError("TI 常驻服务启动或通信失败: %s；日志 %s" %
                                       (error, tool.log_path)) from error
            raise

    def _event(self, message):
        self.tool.log.debug("[service event] %s", json.dumps(message, ensure_ascii=False))
        if self.closing:
            return
        data = message.get("data", {})
        if not isinstance(data, dict):
            return
        event = message.get("event")
        if event == "progress.update":
            key = (data.get("task"), data.get("id"))
            token = (data.get("name"), data.get("percent"), data.get("subActivity"), data.get("isComplete"))
            if token != self.progress_tokens.get(key):
                self.progress_tokens[key] = token
                for module in (self.root, self.core):
                    if module:
                        module.touch()
            if data.get("isFirstUpdate"):
                self._notice(str(data.get("name", "")))
            if not data.get("isComplete") and data.get("subActivity"):
                self._notice(str(data["subActivity"]))
        elif event in ("statusMessage", "gelOutput"):
            text = str(data.get("message", ""))
            if str(data.get("type", "")).lower() == "error":
                self._abort(text or "TI 服务报告错误")
            self._notice(text)
        elif event == "targetState.changed" and self.target_connected and not self.disconnect_expected:
            state = data.get("state", {})
            if (state.get("connectionState") in ("Disconnected", "Disconnecting")
                    or state.get("isPowered") is False):
                self._abort("烧录期间 JTAG 连接已中断，当前板需要重新烧录")

    def _abort(self, message):
        if self.async_error is None:
            self.async_error = message
        error = FlashError(self.async_error)
        for module in (self.root, self.core):
            if module:
                module.abort(error)

    def _notice(self, text):
        if text:
            self.lines.append(text)
            self.tool._progress_line(text)

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TIError("TI 常驻烧录超时，目标状态未知")
        return remaining

    def _call(self, module, command, *data):
        self.tool.log.info("RPC %s %s", command, json.dumps(data, ensure_ascii=False))
        result = module.call(command, *data, timeout=self._remaining(), idle_timeout=self.tool.timeout)
        if self.async_error:
            raise FlashError(self.async_error)
        self.tool.log.debug("RPC_OK %s", command)
        return result

    def _ensure_configured(self):
        """Initialize once; retaining this state avoids the repeated ~5s setup."""
        signature = (str(self.tool.target), hashlib.sha256(self.tool.target.read_bytes()).hexdigest())
        if self.configured and self.config_signature != signature:
            self._call(self.root, "deConfigure")
            self.configured = False
            self.core.close()
            self.core = None
        if not self.configured:
            self._notice("Initializing flash environment...")
            config = self._call(self.root, "configure", str(self.tool.target))
            self.configured = True
            cores = config.get("cores", []) if isinstance(config, dict) else []
            if not cores:
                cores = self._call(self.root, "listCores").get("cores", [])
            if not cores or not isinstance(cores[0], str) or not cores[0].endswith("/C28xx"):
                raise EnvironmentError("TI 配置的第一个核心不是 C28xx，拒绝烧录")
            sub = self._call(self.root, "createSubModule", cores[0])
            self.core = _RPC(sub["port"], sub.get("subProtocol"),
                             timeout=self._remaining(), event=self._event)
            self.config_signature = signature
            return True
        else:
            self.tool.log.info("SESSION_CONFIG_REUSED pid=%s", self.process.pid)
            return False

    def initialize(self):
        """Preload debugger metadata and core modules, without connecting a CPU."""
        self.deadline = time.monotonic() + self.tool.timeout
        self.lines = []
        self.async_error = None
        self.progress_tokens = {}
        if self.process.poll() is not None:
            raise TIError("TI 常驻服务已退出")
        return self._ensure_configured()

    def flash(self, plan):
        """Keep the debugger configuration and core module across target swaps.

        Only connect/disconnect brackets each board. Reconfigure when the CCXML
        changes; the SDK discards the service on USB changes or an uncertain flash.
        """
        started = time.monotonic()
        self.deadline = started + self.tool.flash_timeout
        self.lines = []
        self.async_error = None
        self.progress_tokens = {}
        self.target_connected = False
        self.disconnect_expected = False
        if self.process.poll() is not None:
            raise TIError("TI 常驻服务已退出；本次不自动重试")
        self._ensure_configured()
        files = [{"filename": entry["path"], **({"loadAddress": int(entry["address"], 0)}
                  if entry["address"] is not None else {})} for entry in plan["images"]]
        self._notice("Connecting...")
        self._call(self.core, "targetState.connect")
        self.target_connected = True
        self._call(self.core, "settings.setCLI", {
            "AutoRunToLabelOnRestart": False, "AutoRunToLabelOnReset": False})
        self._notice("Preparing firmware...")
        self._call(self.core, "symbols.loadProgramOnly", files)
        self._notice("Verifying firmware...")
        self._call(self.core, "symbols.verifyProgram", files)
        # This is an explicit verification API acknowledgement, not a GUI message.
        self._notice("Program verification successful (TI verifyProgram acknowledged)")
        if self.tool.run_after_flash:
            self._notice("Running...")
            self._call(self.core, "targetState.run", True)
        self._call(self.root, "waitForIdle")
        self.disconnect_expected = True
        self._call(self.core, "targetState.disconnect")
        self.target_connected = False
        self._call(self.root, "waitForIdle")
        self.disconnect_expected = False
        seconds = time.monotonic() - started
        self.tool.log.info("SESSION_FLASH_OK seconds=%.3f pid=%s", seconds, self.process.pid)
        return Result(0, "\n".join(self.lines), seconds, self.tool.log_path)

    def close(self):
        self.closing = True
        for module in (self.core, self.root):
            if module:
                module.close()
        self.core = self.root = None
        if self.process is not None:
            try:
                self.process.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    try:
                        subprocess.run([str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"),
                                        "/PID", str(self.process.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                    except (OSError, subprocess.TimeoutExpired):
                        self.tool.log.exception("SESSION_TASKKILL_FAILED pid=%s", self.process.pid)
                else:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if self.process.poll() is None:
                    self.process.kill()
            self.process.wait(timeout=5)
            self.tool.log.info("SESSION_STOP rc=%s", self.process.returncode)
        if self.reader:
            self.reader.join(timeout=2)
