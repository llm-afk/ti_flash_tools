# XDS110 / F28035 核心接口

依赖和接口分开放置，Windows / Linux 使用同一套 Python API。仅依赖 Python 标准库，建议 Python 3.10 及以上。

```text
ti_flash_tools/
├─ dependencies/       TI 程序、USB 驱动、目标配置、Linux 兼容依赖
├─ sdk/                Python SDK、内部常驻服务通信、config.json
├─ firmware/           待烧录的 Boot / App BIN，保留原文件名
├─ scripts/            auto_flash.py 入口和内部启动准备模块，Windows / Linux 共用
├─ 启动烧录.bat         Windows 双击启动入口
└─ README.md
```

## 接口

| 方法 | 作用 |
|---|---|
| `TIFlash.from_config()` | 按包内配置建立对象，也可指定自己的配置文件 |
| `tool.check()` | 检查依赖和工具启动；不枚举 USB、不访问目标 JTAG/Flash |
| `tool.probes()` | 只枚举 USB 烧录器，返回序列号列表 |
| `tool.detect()` | USB + JTAG IR/DR 探测，返回状态和说明 |
| `tool.plan(images)` | 校验固件与地址，返回命令和 SHA256；不访问硬件 |
| `tool.flash(images)` | 单次编程、校验；不包含等待、循环、重试等业务流程 |
| `tool.session()` | 上下文中复用 TI 本地服务；退出时关闭服务并释放占用 |
| `tool.start_session()` / `tool.end_session()` | 显式启动 / 关闭同一实例的常驻服务 |
| `tool.initialize()` | 提前初始化唯一 XDS110 的调试器配置和核心句柄；不连接 CPU、不烧录；返回本次是否新建了配置 |

```python
from sdk import TIFlash, Image

with TIFlash.from_config() as tool:
    print(tool.probes())
    status = tool.detect()
    print(status.state, status.detail)

    # 调用方按需执行单次烧录：
    # result = tool.flash([
    #     Image("boot.bin", 0x3F6000),
    #     Image("app.bin", 0x3E8000),
    # ])
    # print(result.seconds, result.log)
```

也可直接 `TIFlash(runtime, target, logs=...)`。构造和 import 都不访问硬件。
配置路径相对于配置文件；直接传入的固件路径相对于调用方当前目录。待烧录固件集中放在 `firmware/`，当前为 `ADM32F036_mc_boot_released_fw_237.bin`（16 KiB）和 `ADM32F036_mc_app_released_fw_245.bin`（48 KiB）。默认配置的 `images` 保持为空，调用方显式指定文件和起始地址。

`dependencies/f28035.ccxml` 基于 `mc/2.Firmware/ADM32F036_mc_app/targetConfigs/TMS320F28035.ccxml`，配置为 XDS110 + TMS320F28035。当前烧录 JTAG 时钟固定为 **5.5 MHz**，Windows / Linux 共用此文件。`detect()` 仍使用 `dbgjtag @xds110` 的工具预设，检测频率与烧录频率分别配置。

`detect()` 的状态为 `NO_PROBE`、`WAIT_BOARD`、`READY`、`UNKNOWN`，可用 `status.ready`。
READY 仅表示 JTAG 链路通过，不能替代芯片型号认证。当前支持单个 XDS110，多个探针会拒绝烧录。

同一轮轮询已经调用 `probes()` 时，可将其结果传给 `initialize(probes=probes)` 和 `detect(probes=probes)`，避免重复枚举 USB。只复用本轮刚取得的结果；不传该参数时 SDK 仍自行枚举。`flash()` 始终在烧录前重新检查探针数量，不依赖检测时的快照。

普通 `flash()` 使用 CLI，成功必须同时满足 TI 返回码 0 和 `Program verification successful`。常驻模式调用同一套 TI 底层接口，必须等 `symbols.verifyProgram` 明确成功，并完成运行请求和断开目标连接，才返回成功；任一步失败均抛出异常。常驻结果的 `returncode=0` 表示本次 API 流程完成，服务进程及目标配置仍保留。
返回原始输出、耗时和日志路径；环境/权限异常为 `EnvironmentError`，编程/校验失败为 `FlashError`，通用操作异常为 `TIError`。
`run_after_flash` 控制是否请求运行，默认 true；请求运行不代表应用功能已经验证。

BIN 使用 C28x 的 16 位 word 地址，检查非空、偶数字节、Flash 范围和 BIN 重叠；OUT/ELF 使用自身地址。
保留参考实现的 `-e -v -f ... -u`；其中 `-e` 是详细输出，实际擦除策略由 TI C2000 Flash 配置决定。
日志默认写到系统临时目录的 `ti_flash_logs`，也可以传入自己的日志目录，不在交付目录留下运行日志。

连续调用 SDK 时可以主动复用服务，Windows / Linux 接口相同：

```python
with TIFlash.from_config() as tool:
    with tool.session():
        # 可在仅接入 XDS110、尚未接触板子时提前初始化。
        tool.initialize()
        # 调用方确认每块板已连接后，分别调用 tool.flash(images)。
        # 两次 flash() 使用同一个 DSLite 服务进程。
        pass
```

`session()` / `start_session()` 只启动软件服务；`initialize()` 可提前建立目标配置和 C28xx 核心句柄，重复调用会复用配置，若没有提前调用则由首次 `flash()` 建立。之后一直保留并复用，每块板只重新连接、写入、校验、运行和断开连接。拔出板上的 JTAG 探针不销毁配置，原有 `dbgjtag` 检测继续用于确认换板。

XDS110 USB 消失、序列号/数量变化，或烧录过程出现异常时，丢弃服务和配置；重新具备条件后为后续烧录重建。CCXML 路径或文件内容变化时重新配置。每板结束不会执行 `deConfigure`，服务和配置的正常生命周期持续到退出脚本或关闭 SDK 会话。

常驻期间，该 SDK 实例持有工位独占锁。完成或退出时调用 `end_session()` / `close()`，或使用上述上下文自动关闭。烧录、校验、断开异常或超时会丢弃当前服务；本次不自动重试，调用方确认换板后可在后续调用中建立新服务。进程退出会释放操作系统锁。

常驻模式下，TI 上报错误或已连接目标意外断开时，会立即唤醒正在等待的调用、结束本次烧录并清理失效服务，不再等完整的烧录超时。另以 `timeout`（默认 30 秒）限制单次调用连续无进度的时间；重复的相同进度消息不会延长等待。`flash_timeout`（默认 300 秒）仍限制本次烧录的总时长。正常断开连接不触发失败。

## 产线连续烧录

Windows：双击根目录的 `启动烧录.bat`。启动器自动查找 Python 3.10 及以上版本，运行现有连续烧录脚本；运行结束后保留窗口。也可继续使用 `python C:\Users\32196\Desktop\ti_flash_tools\scripts\auto_flash.py`。

Linux：进入 `ti_flash_tools` 根目录，运行 `python3 scripts/auto_flash.py`。首次使用会自动校验并静默解压包内运行库。

脚本启动后持续运行，按 `Ctrl+C` 退出。固件文件名、起始地址、轮询间隔在脚本顶部集中配置；当前使用 `firmware/` 中的 Boot 237（`0x3F6000`）和 App 245（`0x3E8000`），地址单位为 C28x 16 位 word。

正常流程：等待唯一 XDS110 → 提前初始化 → 安静检测 JTAG → 自动烧录 Boot + App 并校验 → 显示结果和耗时 → 拔出板上的 JTAG 探针 → 连接下一块。XDS110 的 USB 可一直保持连接。

初始化在仅接入 XDS110 时进行，无需接触待烧录板。首次准备、USB 重插或失败后重建时，先显示一次“正在初始化烧录环境……”，完成后才提示“请将探针接触待烧录板”；详细过程写入日志。复用已准备好的环境时不重复提示初始化。约 5 秒的首次初始化移到了接入 XDS110 后的等待阶段，烧录耗时从探针接触且开始该次烧录时计算。初始化失败沿用原有异常提示，不计入烧录次数，也不会发起编程。

脚本默认开启 TI 常驻服务，退出时关闭自己启动的服务；无需增加 Node.js、Java 或 Python 第三方包。`--check` 仍只检查软件，不启动常驻服务、不访问目标。若要对比原有方式，运行时加 `--no-session`，每次烧录便使用独立的 CLI 进程。日志中的 `SESSION_START`、`SESSION_CONFIG_REUSED` 和 `SESSION_FLASH_OK ... pid=...` 可分别确认进程启动、配置复用和烧录完成。

控制台仅在 XDS110 数量/序列号变化、烧录阶段和结果变化时输出；JTAG 轮询只写详细日志。检测后的空等间隔为 50 ms，每轮只枚举一次 USB，实际检测频率仍受 TI 工具耗时限制。烧录成功或失败后都必须先确认链路断开（至少连续 2 次明确检测且覆盖 100 ms），才允许下一次烧录；未知检测结果、链路恢复和通信异常都会清除本次断开确认计时。失败不会在板子仍连接时自动重烧。

烧录途中拔针会把当前板判为失败，该板需要完整重新烧录。失败后先恢复拔针检测，确认链路断开后再初始化新会话，避免重建环境阻塞换板确认。

每轮烧录结束后只空一行。确认探针拔出时提示“探针已拔出”，待环境准备好后提示“请将探针接触待烧录板”；相同等待状态不重复打印。

终端中仅“成功”显示绿色、“失败”显示红色，其余文字保持普通颜色；重定向或被其他程序捕获时输出纯文本。

软件环境或 USB 异常会保留脚本运行，修复后继续检测；相同异常不重复刷屏。只允许一个自动烧录脚本占用工位。固件在启动时记录 SHA256，运行中改变固件会停止发起烧录，需恢复文件或重启脚本确认新批次。

只做启动准备、依赖检查、已接入 USB 的驱动/权限检查及固件地址检查：给上述命令加 `--check`。该模式只读取操作系统 USB 设备信息，不打开探针、不访问 JTAG、不烧录；成功不输出内容并返回 0，失败打印原因、处理方法和日志位置并返回 1。Linux 首次运行也会自动准备运行库。

## 接入其他自动化流程

`scripts/auto_flash.py` 调用 SDK，组合出连接等待、换板确认和连续烧录；命令行参数和打印也留在脚本中。其他程序可以调用这个脚本，也可以直接调用 SDK；SDK 不依赖调用方的具体流程。导入模块不会启动检测或烧录。

在项目根目录可直接调用组合流程：

```python
import threading
from scripts.auto_flash import run

stop = threading.Event()

def handle_event(event):
    if event.kind in ("success", "failure"):
        print(event.kind, event.attempt, event.message)
        # 成功事件的 event.result 是 SDK Result，包含原始输出、耗时和日志路径。

result = run(max_attempts=1, stop_event=stop, on_event=handle_event)
print(result.successes, result.failures, result.reason)
```

`run()` 默认不打印，可传 `on_event` 接收结构化事件；不传 `max_attempts` 就持续运行。`images` 可传自己的 `Image` 列表，`config` 可指定自己的 SDK 配置文件。默认 `persistent=True`；传 `persistent=False` 使用每次独立启动的 CLI。`stop.set()` 会立即结束连接等待；若已开始烧录，则完成当前烧录和校验后再返回。进度事件可能来自 SDK 的读取线程，GUI 调用方应将其转交自己的消息队列；回调异常只记录到日志，不改变固件烧录结果。

返回值 `RunSummary` 包含尝试次数、成功/失败次数、停止原因、最后一次成功返回的 SDK 结果（最后一次失败则为 `None`）和日志路径。停止原因是 `stop_requested`、`limit_reached` 或 `keyboard_interrupt`。

调用方的 Python 路径包含 `ti_flash_tools` 的上一级目录时，也可以用 `from ti_flash_tools.scripts.auto_flash import run` 导入。

如果外部流程用子进程调用，可加 `--once`：等待一块板并烧录一次后退出，成功返回码为 0，失败为 1，用户中断为 130。每次重新调用都是新会话，调用方负责换板；连续生产应保持同一个 `run()` 运行。若外部流程已经管理连接和工位状态，直接调用 SDK 的 `flash()` 即可。

## 一次性环境准备

安装 Python 3.10 及以上版本后，直接使用现有启动入口即可。启动准备只由 `scripts/auto_flash.py` 调用内部的 `_environment.py` 执行，不放进 SDK 的探测、JTAG 或烧录接口。每次启动自动检查依赖，成功静默，正常烧录和换板提示保持显示。

Windows 的四个 VC++ x64 DLL 已按同一版本 14.50.35719.0 随包携带，共约 1 MiB，较原有三个 DLL 净增加约 274 KiB；包含此前缺失的 `concrt140.dll`。来源、版本、微软签名核验和 SHA256 保存在 `dependencies/windows/vc_runtime_manifest.json`，启动时校验这四个小文件。无需为此单独安装整套 Visual Studio。Python 不随包附带，缺失时由启动器明确提示安装 3.10 或更新版本。

TI 子进程继续使用 SDK 原有的包内 DLL/动态库搜索路径，ARM64 使用包内 QEMU。脚本启动准备仅对自带默认 Linux 目录自动解压，不替换调用方指定的运行库，也不覆盖已有非空运行目录。

通过 Git 克隆项目时需安装 Git LFS，并在仓库目录执行 `git lfs pull` 获取完整大文件。`dependencies/linux.tar.gz` 和 Windows 运行库中的 `lldbSM.dll` 使用 LFS 存储；检查发现指针文件时会明确提示该路径和 `git lfs pull` 命令。直接复制完整交付目录不需要 Git LFS。

- Windows：依赖已集中在 `dependencies/windows`。缺 USB 驱动时，管理员 PowerShell 执行：
  `pnputil /add-driver ".\dependencies\windows\ccs_base\emulation\windows\xds110_drivers\*.inf" /install`
- Windows：启动时检查并校验包内 VC++ x64 运行库；缺失或损坏时打印具体 DLL、恢复依赖目录的处理方法，必要时给出微软运行库安装地址：https://aka.ms/vc14/vc_redist.x64.exe 。
- Linux：首次自动校验并解压到 `dependencies/linux`；也可以手动执行 `python3 dependencies/setup_linux.py`，加 `--quiet` 可使成功静默。不需要安装 CCS。
- Linux x86_64 原生执行 Linux TI 工具；A733/ARM64 使用包内 QEMU + AMD64 rootfs，API 不变。
- 启动阶段读取系统当前已接入的 XDS110 USB 节点：Windows 输出设备管理器问题代码及管理员驱动安装命令；Linux 输出具体 USB 设备节点及 udev 配置命令。未接入探针不判为依赖异常，之后由原有流程等待接入；没有连接探针时，启动检查无法确认它的驱动绑定。需要重新诊断时运行 `--check` 或重启脚本。启动完成后的探测轮询不增加依赖检查。相同启动异常不重复打印，完整原始输出保存在临时日志。
- 驱动和 VC++ 安装、Linux 系统 USB 权限配置需要管理员处理；脚本检测失败时给出操作方法，本地运行库和子进程环境自动准备，不自动提权或下载外部程序。

依赖保留所需 targetdb、GEL、寄存器表、DLL/so 和原有许可，不能只复制三个 EXE。
原 A733 包的 DSLite 9.3 退出兼容库只作用于 DSLite，退出状态保留。
原 `ADM_autoFlash` 仅作参考，未修改。TI CLI 参数说明：
https://software-dl.ti.com/ccs/esd/uniflash/docs/latest_qsguide.html
