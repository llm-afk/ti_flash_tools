"""Unpack the existing, hash-pinned TI/QEMU bundle once. No system installation."""
import hashlib
import json
from pathlib import Path
import sys
import tarfile
import tempfile


def main():
    if sys.platform != "linux":
        raise RuntimeError("在 Linux 机器上执行此脚本；Windows 运行库已就绪")
    base = Path(__file__).resolve().parent
    metadata = json.loads((base / "linux_manifest.json").read_text(encoding="utf-8"))
    archive, target = base / metadata["archive"], base / "linux"
    marker = target / ".bundle-sha256"
    if marker.is_file() and marker.read_text().strip() == metadata["sha256"]:
        print("Linux 运行库已就绪: " + str(target))
        return 0
    if target.exists() and any(target.iterdir()):
        raise RuntimeError("运行目录已有内容，先确认版本；不会自动覆盖: " + str(target))
    digest = hashlib.sha256()
    with archive.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != metadata["sha256"]:
        raise RuntimeError("Linux 运行包 SHA256 不一致，请重新复制")
    print("正在解压 TI / QEMU 运行库……", flush=True)
    with tempfile.TemporaryDirectory(prefix=".linux-", dir=base) as folder:
        stage = Path(folder)
        with tarfile.open(archive, "r:gz") as package:
            # Older Python also checks the pinned bundle's paths/links before extraction.
            for member in package.getmembers():
                path = (stage / member.name).resolve()
                if path != stage and stage not in path.parents:
                    raise RuntimeError("压缩包路径越界: " + member.name)
                if member.isdev() or member.isfifo():
                    raise RuntimeError("压缩包含特殊设备: " + member.name)
                if member.issym() or member.islnk():
                    link = ((path.parent if member.issym() else stage) / member.linkname).resolve()
                    if link != stage and stage not in link.parents:
                        raise RuntimeError("压缩包链接越界: " + member.name)
            if hasattr(tarfile, "data_filter"):
                package.extractall(stage, filter="data")
            else:
                package.extractall(stage)
        for relative, expected in metadata["runtime_files"].items():
            if hashlib.sha256((stage / relative).read_bytes()).hexdigest() != expected:
                raise RuntimeError("运行库校验失败: " + relative)
        (stage / ".bundle-sha256").write_text(metadata["sha256"] + "\n")
        stage.rename(target)
    print("Linux 运行库已就绪: " + str(target))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, tarfile.TarError) as exc:
        print("准备失败: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
