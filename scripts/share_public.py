"""公网共享启动器：启动题库，再开一条 Cloudflare 隧道给同事一个可访问的网址。

用法：双击「启动题库系统-公网共享.bat」，或在项目目录执行
    python -B -m scripts.share_public
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from scripts import local_launcher as ll

CLOUDFLARED = ll.PROJECT_ROOT / "tools" / "cloudflared.exe"
URL_FILE = ll.PROJECT_ROOT / "公网地址.txt"
URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def ensure_server() -> None:
    """确保本地题库在 8000 端口运行（隧道只需要连本机回环）。"""
    if ll.existing_service_ready():
        print("题库已在运行，直接开启公网通道。")
        return
    with ll.launcher_lock(ll.PROJECT_ROOT / ".system_generated"):
        if ll.existing_service_ready():
            return
        if ll.port_in_use():
            raise ll.LauncherError("端口 8000 已被占用，或题库尚未就绪；请稍后重试。")
        python = Path(sys.executable)
        ll.ensure_dependencies(ll.PROJECT_ROOT, python)
        ll.start_server(ll.PROJECT_ROOT, python)


def start_tunnel() -> tuple[subprocess.Popen, str]:
    """拉起 cloudflared 并从日志里取出公网地址。"""
    if not CLOUDFLARED.is_file():
        raise ll.LauncherError(
            "缺少穿透程序 tools/cloudflared.exe。\n"
            "请把它放回 tools 目录，或重新向我索取下载步骤。"
        )
    print("正在建立公网通道...")
    proc = subprocess.Popen(
        [str(CLOUDFLARED), "tunnel", "--url", ll.BASE_URL,
         "--no-autoupdate", "--protocol", "http2"],
        cwd=str(ll.PROJECT_ROOT), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    # 读日志可能长时间阻塞，用看门狗兜底。
    watchdog = threading.Timer(90, proc.terminate)
    watchdog.start()
    try:
        while True:
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                if proc.poll() is not None:
                    raise ll.LauncherError("公网通道启动失败：程序提前退出，请检查网络后重试。")
                continue
            match = URL_PATTERN.search(line)
            if match:
                return proc, match.group(0)
    finally:
        watchdog.cancel()


def public_ready(url: str, tries: int = 6) -> bool:
    """隧道刚建立时域名解析可能还没生效，轮询确认真正可访问。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(tries):
        try:
            with opener.open(url + "/healthz", timeout=6) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


def announce(url: str, reachable: bool = True) -> None:
    line = "=" * 68
    print()
    print(line)
    print("  公网地址（发给同事，任何网络下都能打开）：")
    print(f"  {url}")
    print(line)
    if not reachable:
        print("  ⚠ 本机暂时探测不到这个网址，可能只是解析延迟。")
        print("    请先用手机（关掉 Wi-Fi，走流量）打开验证一下再发给同事。")
    print("  把上面这行网址复制发到群里即可，同事无需安装任何东西。")
    print("  · 保持这个窗口开着，关闭窗口或关电脑，链接就会失效")
    print("  · 电脑睡眠同样会断开，建议临时关掉睡眠")
    print("  · 每次重启，网址都会变，重新复制即可")
    print(line)
    with open(URL_FILE, "w", encoding="utf-8") as handle:
        handle.write(url + "\n")
    try:  # 顺手放进剪贴板，方便直接粘贴到微信
        subprocess.run("clip", input=url, text=True, check=False, shell=True)
        print("  网址已复制到剪贴板。")
    except (OSError, subprocess.SubprocessError):
        pass


def run() -> None:
    ensure_server()
    proc, url = start_tunnel()
    reachable = public_ready(url)
    announce(url, reachable)
    try:
        while proc.poll() is None:
            time.sleep(2)
        print("公网通道已断开（可能是网络波动），请关闭本窗口重新启动一次。")
    except KeyboardInterrupt:
        print("\n正在关闭公网通道...")
        proc.terminate()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if callable(getattr(stream, "reconfigure", None)):
            stream.reconfigure(encoding="utf-8", errors="replace")
    print("本地数学题库（MathBank）公网共享启动器")
    try:
        run()
        return 0
    except (ll.LauncherError, OSError) as exc:
        print(f"启动未完成：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
