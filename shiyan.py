# -*- coding: utf-8 -*-
"""Safely execute a copied LLM-generated ADB UI action sequence.

Default input source is the Windows clipboard. Only a small allow-list of
ADB UI commands is accepted; commands are executed without shell=True.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


MAX_LINES = 100
MAX_WAIT_SECONDS = 30.0
MAX_COORDINATE = 10000
DEFAULT_SWIPE_DURATION_MS = 500
ALLOWED_KEYCODES = {
    "KEYCODE_BACK",
    "KEYCODE_ENTER",
    "KEYCODE_DEL",
    "KEYCODE_SPACE",
    "KEYCODE_TAB",
    "KEYCODE_ESCAPE",
}


class CommandRejected(ValueError):
    pass


def read_clipboard() -> str:
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            value = root.clipboard_get()
        finally:
            root.destroy()
        return value
    except Exception as exc:  # pragma: no cover - depends on desktop clipboard
        raise RuntimeError(
            "无法读取剪贴板"
        ) from exc


def read_pasted() -> str:
    print("请将网页端生成的 ADB 命令粘贴到这里。")
    print("粘贴完成后，另起一行输入 END 并回车：")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "END":
            break
        lines.append(line)
    return "\n".join(lines)


def read_source(args: argparse.Namespace) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.clipboard:
        return read_clipboard()
    return read_pasted()


def clean_lines(raw: str) -> list[str]:
    """Remove Markdown fences and join safe backslash-continued lines."""
    cleaned: list[str] = []
    pending = ""
    for raw_line in raw.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("```"):
            continue
        if line.startswith("#"):
            continue
        if line.startswith("$"):
            line = line[1:].lstrip()
        if line.endswith("\\"):
            pending += line[:-1].rstrip() + " "
            continue
        line = pending + line
        pending = ""
        cleaned.append(line)
    if pending.strip():
        cleaned.append(pending.strip())
    return cleaned


def parse_number(value: str, name: str, minimum: float = 0, maximum: float = MAX_COORDINATE) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise CommandRejected(f"{name} 不是数字: {value}") from exc
    if not minimum <= number <= maximum:
        raise CommandRejected(f"{name} 超出允许范围: {value}")
    return number


def parse_int(value: str, name: str, minimum: int = 0, maximum: int = MAX_COORDINATE) -> int:
    number = parse_number(value, name, minimum, maximum)
    if not number.is_integer():
        raise CommandRejected(f"{name} 必须是整数: {value}")
    return int(number)


def format_num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def parse_command(line: str, selected_serial: str | None) -> dict:
    if ";" in line or "&&" in line or "||" in line or "|" in line:
        raise CommandRejected("不允许使用命令连接符、管道或重定向")
    try:
        tokens = shlex.split(line, posix=True)
    except ValueError as exc:
        raise CommandRejected(f"引号解析失败: {exc}") from exc
    if not tokens:
        raise CommandRejected("空命令")

    if tokens[0] == "sleep":
        if len(tokens) != 2:
            raise CommandRejected("sleep 格式应为: sleep SECONDS")
        seconds = parse_number(tokens[1], "等待时间", 0, MAX_WAIT_SECONDS)
        return {"kind": "wait", "seconds": seconds, "display": f"sleep {format_num(seconds)}"}

    first_name = Path(tokens[0]).name.lower()
    if first_name not in {"adb", "adb.exe"}:
        raise CommandRejected("只允许 adb UI 命令或 sleep 命令")

    idx = 1
    command_serial = None
    if idx < len(tokens) and tokens[idx] == "-s":
        if idx + 1 >= len(tokens):
            raise CommandRejected("-s 缺少设备序列号")
        command_serial = tokens[idx + 1]
        idx += 2
    if command_serial and selected_serial and command_serial != selected_serial:
        raise CommandRejected(
            f"命令指定的设备 {command_serial} 与当前设备 {selected_serial} 不一致"
        )
    if idx >= len(tokens) or tokens[idx] != "shell":
        raise CommandRejected("只允许 adb shell UI 命令")
    idx += 1
    inner = tokens[idx:]
    if not inner:
        raise CommandRejected("adb shell 后缺少具体操作")

    if inner[0] == "sleep":
        if len(inner) != 2:
            raise CommandRejected("adb shell sleep 格式错误")
        seconds = parse_number(inner[1], "等待时间", 0, MAX_WAIT_SECONDS)
        return {"kind": "wait", "seconds": seconds, "display": f"sleep {format_num(seconds)}"}

    if inner[0] != "input":
        raise CommandRejected("只允许 adb shell input 操作")

    # adb shell input tap X Y
    if len(inner) == 4 and inner[1] == "tap":
        x = parse_int(inner[2], "tap x")
        y = parse_int(inner[3], "tap y")
        args = ["shell", "input", "tap", str(x), str(y)]
        return {"kind": "adb", "args": args, "display": "adb shell " + " ".join(args[1:])}

    # adb shell input swipe X1 Y1 X2 Y2 [DURATION_MS]
    if inner[1] == "swipe" and len(inner) in {6, 7}:
        x1 = parse_int(inner[2], "swipe x1")
        y1 = parse_int(inner[3], "swipe y1")
        x2 = parse_int(inner[4], "swipe x2")
        y2 = parse_int(inner[5], "swipe y2")
        duration = (
            parse_int(inner[6], "swipe duration", 1, 30000)
            if len(inner) == 7
            else DEFAULT_SWIPE_DURATION_MS
        )
        args = ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration)]
        return {"kind": "adb", "args": args, "display": "adb shell " + " ".join(args[1:])}

    # adb shell input touchscreen swipe X1 Y1 X2 Y2 DURATION_MS
    if len(inner) == 8 and inner[1:3] == ["touchscreen", "swipe"]:
        x1 = parse_int(inner[3], "swipe x1")
        y1 = parse_int(inner[4], "swipe y1")
        x2 = parse_int(inner[5], "swipe x2")
        y2 = parse_int(inner[6], "swipe y2")
        duration = parse_int(inner[7], "swipe duration", 1, 30000) if len(inner) > 7 else None
        if duration is None:
            raise CommandRejected("touchscreen swipe 缺少 duration")
        args = ["shell", "input", "touchscreen", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration)]
        return {"kind": "adb", "args": args, "display": "adb shell " + " ".join(args[1:])}

    # adb shell input keyevent KEYCODE_...
    if len(inner) == 3 and inner[1] == "keyevent":
        keycode = inner[2].upper()
        if keycode not in ALLOWED_KEYCODES:
            raise CommandRejected(f"不允许的按键: {keycode}")
        args = ["shell", "input", "keyevent", keycode]
        return {"kind": "adb", "args": args, "display": "adb shell " + " ".join(args[1:])}

    # adb shell input text TEXT
    if len(inner) >= 3 and inner[1] == "text":
        text = " ".join(inner[2:])
        if len(text) > 500:
            raise CommandRejected("输入文本过长")
        args = ["shell", "input", "text", text]
        safe_display = text.replace("\n", "\\n")
        return {"kind": "adb", "args": args, "display": f'adb shell input text "{safe_display}"'}

    raise CommandRejected("不支持的 input 操作；允许 tap、swipe、text、keyevent")


def normalize_commands(raw: str, selected_serial: str | None) -> list[dict]:
    lines = clean_lines(raw)
    if not lines:
        raise CommandRejected("剪贴板中没有可执行命令")
    if len(lines) > MAX_LINES:
        raise CommandRejected(f"命令行数超过上限 {MAX_LINES}")
    commands = []
    for line_no, line in enumerate(lines, start=1):
        try:
            commands.append(parse_command(line, selected_serial))
        except CommandRejected as exc:
            raise CommandRejected(f"第 {line_no} 行被拒绝: {exc}\n原文: {line}") from exc
    return commands


def _run_adb(adb_path: str, args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a non-interactive ADB command without invoking a shell."""
    return subprocess.run(
        [adb_path] + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _run_adb_bytes(adb_path: str, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run ADB while preserving binary stdout, used for PNG screenshots."""
    return subprocess.run(
        [adb_path] + args,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def capture_screenshot(
    adb_path: str,
    serial: str,
    output_dir: str | Path,
) -> Path:
    """Capture the current emulator screen as a PNG using ADB."""
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    screenshot_path = directory / f"screen_{timestamp}.png"
    adb_prefix = ["-s", serial]

    # exec-out keeps the PNG binary intact on Windows.
    result = _run_adb_bytes(
        adb_path,
        adb_prefix + ["exec-out", "screencap", "-p"],
        timeout=30,
    )
    if result.returncode == 0 and result.stdout.startswith(b"\x89PNG"):
        screenshot_path.write_bytes(result.stdout)
        print(f"Screenshot saved: {screenshot_path}")
        return screenshot_path

    # Fallback for emulators where exec-out is unreliable: save remotely, then pull.
    remote_path = "/sdcard/shiyan111_screen.png"
    remote_result = _run_adb(
        adb_path,
        adb_prefix + ["shell", "screencap", "-p", remote_path],
        timeout=30,
    )
    if remote_result.returncode == 0:
        pull_result = _run_adb_bytes(
            adb_path,
            adb_prefix + ["pull", remote_path, str(screenshot_path)],
            timeout=30,
        )
        _run_adb(adb_path, adb_prefix + ["shell", "rm", "-f", remote_path], timeout=10)
        if pull_result.returncode == 0 and screenshot_path.exists():
            data = screenshot_path.read_bytes()
            if data.startswith(b"\x89PNG"):
                print(f"Screenshot saved: {screenshot_path}")
                return screenshot_path

    error = (result.stderr or b"").decode("utf-8", errors="replace").strip()
    raise RuntimeError(f"ADB 截图失败: {error or 'screencap 未返回有效 PNG'}")


def list_adb_devices(adb_path: str) -> list[tuple[str, str]]:
    """Return (serial, status) entries reported by adb devices."""
    try:
        result = _run_adb(adb_path, ["devices"], timeout=10)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "找不到 adb。请把 Android SDK platform-tools 加入 PATH，或使用 --adb 指定路径。"
        ) from exc
    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"adb devices 执行失败: {error}")

    devices = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] != "List" and fields[1] in {
            "device",
            "offline",
            "unauthorized",
        }:
            devices.append((fields[0], fields[1]))
    return devices


def ensure_adb_device(
    adb_path: str,
    requested_serial: str | None = None,
    connect_address: str | None = None,
    wait_seconds: int = 20,
) -> str:
    """Start the ADB server, optionally connect to host:port, and select a device."""
    try:
        start_result = _run_adb(adb_path, ["start-server"], timeout=15)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "找不到 adb。请把 Android SDK platform-tools 加入 PATH，或使用 --adb 指定路径。"
        ) from exc
    if start_result.returncode != 0:
        error = start_result.stderr.strip() or start_result.stdout.strip()
        raise RuntimeError(f"ADB server 启动失败: {error}")
    print("ADB server is running.")

    if connect_address:
        connect_result = _run_adb(adb_path, ["connect", connect_address], timeout=15)
        connect_output = (connect_result.stdout or connect_result.stderr).strip()
        print(f"ADB connect {connect_address}: {connect_output}")
        if connect_result.returncode != 0:
            raise RuntimeError(f"无法连接到模拟器地址 {connect_address}: {connect_output}")

    deadline = time.time() + wait_seconds
    last_devices: list[tuple[str, str]] = []
    while time.time() <= deadline:
        last_devices = list_adb_devices(adb_path)
        ready = [serial for serial, status in last_devices if status == "device"]

        if requested_serial:
            if requested_serial in ready:
                print(f"Device connected: {requested_serial}")
                return requested_serial
        elif len(ready) == 1:
            print(f"Device connected: {ready[0]}")
            return ready[0]
        elif len(ready) > 1:
            raise RuntimeError(
                f"检测到多个 device 设备 {ready}，请使用 --serial 指定模拟器。"
            )

        time.sleep(0.5)

    status_text = ", ".join(f"{serial} ({status})" for serial, status in last_devices) or "无"
    if requested_serial:
        raise RuntimeError(
            f"指定设备 {requested_serial} 未上线。当前 ADB 设备: {status_text}"
        )
    raise RuntimeError(
        f"在等待时间内没有找到状态为 device 的模拟器。当前 ADB 设备: {status_text}"
    )


def execute(
    commands: list[dict],
    adb_path: str,
    serial: str | None,
    delay: float,
    log_path: str | Path,
) -> None:
    adb_prefix = [adb_path]
    if serial:
        adb_prefix += ["-s", serial]
    log_file = Path(log_path).expanduser().resolve()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    def write_log(message: str) -> None:
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(message.rstrip() + "\n")

    state_result = _run_adb(adb_path, (["-s", serial] if serial else []) + ["get-state"], timeout=10)
    state_text = (state_result.stdout or state_result.stderr).strip()
    if state_result.returncode != 0 or state_text != "device":
        raise RuntimeError(f"执行前设备状态异常: {state_text or '无输出'}")
    print(f"ADB target: {serial or 'default'}")
    write_log(f"START target={serial or 'default'} state={state_text}")
    for index, command in enumerate(commands, start=1):
        full_command = adb_prefix + command.get("args", [])
        if command["kind"] == "wait":
            full_command = ["local", "sleep", format_num(command["seconds"])]
        started_at = datetime.now().isoformat(timespec="milliseconds")
        print(f"[{index}/{len(commands)}] {command['display']}")
        print(f"           exec: {subprocess.list2cmdline(full_command)}")
        write_log(f"{started_at} COMMAND {index}/{len(commands)} {subprocess.list2cmdline(full_command)}")
        if command["kind"] == "wait":
            time.sleep(command["seconds"])
            print("           result: local wait completed")
            print("           [EXECUTED]")
            write_log(f"{datetime.now().isoformat(timespec='milliseconds')} RESULT returncode=0 local_wait")
            continue
        result = subprocess.run(
            adb_prefix + command["args"], capture_output=True, text=True, timeout=35, check=False
        )
        print(f"           returncode: {result.returncode}")
        if result.stdout.strip():
            print(result.stdout.strip())
        if result.returncode != 0:
            error = result.stderr.strip() or "无错误输出"
            write_log(f"{datetime.now().isoformat(timespec='milliseconds')} RESULT returncode={result.returncode} stderr={error}")
            raise RuntimeError(f"第 {index} 条命令失败，已停止执行:\n{error}")
        health_result = _run_adb(
            adb_path,
            (["-s", serial] if serial else []) + ["get-state"],
            timeout=10,
        )
        health_text = (health_result.stdout or health_result.stderr).strip()
        print(f"           device_state: {health_text or '无输出'}")
        print("           [EXECUTED]")
        write_log(
            f"{datetime.now().isoformat(timespec='milliseconds')} RESULT "
            f"returncode={result.returncode} device_state={health_text or 'NO_OUTPUT'}"
        )
        if result.stdout.strip():
            write_log(f"stdout={result.stdout.strip()}")
        if result.stderr.strip():
            write_log(f"stderr={result.stderr.strip()}")
        if delay > 0:
            time.sleep(delay)


def main() -> int:
    parser = argparse.ArgumentParser(description="安全执行剪贴板中的 LLM 生成 ADB UI 命令")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--paste", action="store_true", help="直接粘贴多行命令，输入 END 结束（默认）")
    source.add_argument("--clipboard", action="store_true", help="读取当前 Windows 剪贴板")
    source.add_argument("--file", help="从 UTF-8 文本文件读取命令")
    parser.add_argument("--adb", default="adb", help="adb 可执行文件路径，默认从 PATH 查找")
    parser.add_argument("--serial", help="指定设备序列号；连接多个设备时必须指定")
    parser.add_argument(
        "--connect",
        dest="connect_address",
        help="连接网络模拟器地址 host:port，例如 127.0.0.1:5555",
    )
    parser.add_argument("--delay", type=float, default=0.15, help="相邻 ADB 命令之间的间隔秒数")
    parser.add_argument(
        "--screenshot-dir",
        default=str(Path(__file__).resolve().parent / "adb_screenshots"),
        help="执行前截图保存目录，默认是脚本目录下的 adb_screenshots",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="ADB 执行日志路径；默认保存到脚本目录下的 adb_command_logs",
    )
    parser.add_argument("--dry-run", action="store_true", help="只校验和预览，不执行")
    args = parser.parse_args()

    if args.delay < 0 or args.delay > 10:
        parser.error("--delay 必须在 0 到 10 秒之间")

    try:
        serial = ensure_adb_device(
            adb_path=args.adb,
            requested_serial=args.serial,
            connect_address=args.connect_address,
        )

        capture_screenshot(args.adb, serial, args.screenshot_dir)
        raw = read_source(args)
        commands = normalize_commands(raw, serial)
    except (RuntimeError, CommandRejected, OSError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    print(f"\n设备: {serial}")
    print(f"已通过白名单校验，共 {len(commands)} 条命令：\n")
    for index, command in enumerate(commands, start=1):
        print(f"  {index:02d}. {command['display']}")

    if args.dry_run:
        print("\nDry run：未执行任何命令。")
        return 0

    print("\n设备屏幕应处于预期的验证码页面，下面将自动执行命令。")
    print("命令校验完成，开始自动执行。")

    try:
        log_path = args.log_file
        if not log_path:
            log_dir = Path(__file__).resolve().parent / "adb_command_logs"
            log_path = str(log_dir / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.log")
        print(f"执行日志: {Path(log_path).resolve()}")
        execute(commands, args.adb, serial, args.delay, log_path)
    except (RuntimeError, subprocess.SubprocessError) as exc:
        print(f"\n执行中止: {exc}", file=sys.stderr)
        return 1
    print("\n全部命令执行完成。请根据应用实际状态记录验证码是否通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
