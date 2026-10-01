"""
Runtime entry point for testing CAPTCHAMind on a connected Android device.

This script connects to the phone, runs the full pipeline in
gui_information_acquisition.py, and prints the structured outputs:

1. GUI Information Acquisition
2. CAPTCHA Recognition
3. CAPTCHA Solving
4. Optional Verification/Feedback after execution

By default, it does not execute taps or drags. Pass --execute to run the
generated action plan on the device.
"""

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime
from typing import Optional

try:
    import uiautomator2 as u2
except ImportError:
    u2 = None

from gui_information_acquisition import (
    DEFAULT_MAX_SOLVING_ATTEMPTS,
    DEFAULT_ZHIPUAI_MODEL,
    _get_zhipu_api_key,
    acquire_gui_information,
    acquire_gui_information_adb,
    execute_solution_adb,
    execute_solution,
    verify_and_refine,
)


DEFAULT_SAVE_ROOT = r"D:\studytool\HintDroid-main (1)\HintDroid-main\test\runtime_output"


def make_run_dir(save_root: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir = os.path.join(save_root, timestamp)
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


def print_json(title: str, value) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def summarize_global_interface_context(global_interface_context):
    summarized = dict(global_interface_context)
    multimodal = summarized.get("screenshot_multimodal_input")
    if isinstance(multimodal, dict):
        summarized["screenshot_multimodal_input"] = {
            "path": multimodal.get("path"),
            "bytes": multimodal.get("bytes"),
            "mime_type": multimodal.get("mime_type"),
            "base64_available": bool(multimodal.get("base64")),
            "omitted_reason": multimodal.get("omitted_reason"),
        }
    return summarized


def print_runtime_warnings(args) -> None:
    if args.use_zhipuai:
        if not args.zhipu_api_key and not (
            os.getenv("ZHIPUAI_API_KEY") or os.getenv("ZHIPU_API_KEY") or os.getenv("GLM_API_KEY")
        ):
            print("\n===== ZhipuAI 配置提示 =====")
            print("已启用 --use-zhipuai，但没有检测到 API key。请设置 ZHIPUAI_API_KEY 或传入 --zhipu-api-key。")
        if importlib.util.find_spec("zhipuai") is None:
            print("\n===== ZhipuAI 依赖提示 =====")
            print("当前 Python 环境没有安装 zhipuai。模型调用会失败；请先在运行环境中安装 zhipuai。")
    if args.backend in {"auto", "adb"} and not shutil_which("adb"):
        print("\n===== ADB 配置提示 =====")
        print("当前 PATH 中没有检测到 adb。若使用 --backend adb 或 auto fallback，请先配置 Android platform-tools。")


def shutil_which(command: str) -> Optional[str]:
    paths = os.getenv("PATH", "").split(os.pathsep)
    extensions = [""] if os.name != "nt" else os.getenv("PATHEXT", ".EXE;.BAT;.CMD").split(os.pathsep)
    for directory in paths:
        for extension in extensions:
            candidate = os.path.join(directory, command + extension)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def print_uiautomator2_help(error: Exception) -> None:
    print("\n===== uiautomator2 启动失败 =====")
    print("手机已经被 Python 找到，但手机端 uiautomator2/stub/atx-agent 没有正常响应。")
    print(f"原始错误: {error}")
    print("\n脚本将自动尝试 ADB fallback：adb shell uiautomator dump + adb exec-out screencap。")
    print("\n请按顺序在 PowerShell 里检查：")
    print("1. adb devices")
    print("   确认设备是 device 状态，不是 unauthorized/offline。")
    print("2. D:\\studytool\\python\\python.exe -m uiautomator2 init")
    print("   重新安装/初始化手机端 uiautomator2 组件。")
    print("3. adb shell am force-stop com.github.uiautomator")
    print("4. adb shell am force-stop com.github.uiautomator.test")
    print("5. 重新运行本脚本。")
    print("\n如果仍然 502，重启手机或重新插拔 USB 后再执行第 2 步。")


def get_device(serial: str = None):
    if u2 is None:
        print("\n===== uiautomator2 未安装 =====")
        print("当前 Python 环境缺少 uiautomator2。你可以使用 --backend adb，或安装 uiautomator2 后使用该 backend。")
        return None, None
    print("Connect to device...")
    device = u2.connect(serial) if serial else u2.connect()
    print("Device connected.")
    try:
        # Preflight: this triggers the JSON-RPC channel before the pipeline starts.
        info = device.info
    except Exception as exc:
        print_uiautomator2_help(exc)
        return None, None
    return device, info


def main() -> None:
    parser = argparse.ArgumentParser(description="Run CAPTCHAMind pipeline on a connected phone.")
    parser.add_argument(
        "--save-root",
        default=DEFAULT_SAVE_ROOT,
        help="Directory where hierarchy XML and screenshots will be saved.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute generated tap/drag actions on the phone.",
    )
    parser.add_argument(
        "--backend",
        choices=["auto", "uiautomator2", "adb"],
        default="auto",
        help="Runtime backend. auto tries uiautomator2 first and falls back to adb.",
    )
    parser.add_argument(
        "--serial",
        default=None,
        help="Optional Android device serial. If omitted, uiautomator2 auto-connects.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_SOLVING_ATTEMPTS,
        help="Maximum verification attempts for feedback-driven refinement.",
    )
    parser.add_argument(
        "--use-zhipuai",
        action="store_true",
        help="Use ZhipuAI GLM for LLM/MLLM recognition and solving.",
    )
    parser.add_argument(
        "--zhipu-api-key",
        default=None,
        help="Optional ZhipuAI API key. Prefer setting ZHIPUAI_API_KEY in the environment.",
    )
    parser.add_argument(
        "--zhipu-model",
        default=DEFAULT_ZHIPUAI_MODEL,
        help="ZhipuAI model name used for multimodal reasoning.",
    )
    parser.add_argument(
        "--disable-zhipu-thinking",
        action="store_true",
        help="Disable thinking={type: enabled} for ZhipuAI requests.",
    )
    args = parser.parse_args()
    if args.zhipu_api_key:
        os.environ["ZHIPUAI_API_KEY"] = args.zhipu_api_key
    print_runtime_warnings(args)
    use_zhipu = args.use_zhipuai or bool(_get_zhipu_api_key())
    model_config = {
        "provider": "zhipuai" if use_zhipu else "deterministic",
        "enabled": use_zhipu,
        "model": args.zhipu_model,
        "thinking_enabled": not args.disable_zhipu_thinking,
    }

    run_dir = make_run_dir(args.save_root)

    device = None
    backend = args.backend
    if backend in {"auto", "uiautomator2"}:
        device, device_info = get_device(args.serial)
        if device is None and backend == "uiautomator2":
            sys.exit(2)
        if device is not None:
            print_json("Device Info", device_info)
            try:
                data_dict, gui_context, hierarchy_path = acquire_gui_information(
                    device,
                    run_dir,
                    model_config=model_config,
                    capture_label="initial",
                )
                backend = "uiautomator2"
            except Exception as exc:
                print_uiautomator2_help(exc)
                if args.backend == "uiautomator2":
                    sys.exit(2)
                device = None

    if device is None:
        backend = "adb"
        print("\nUsing ADB backend...")
        try:
            data_dict, gui_context, hierarchy_path = acquire_gui_information_adb(
                save_path=run_dir,
                serial=args.serial,
                model_config=model_config,
                capture_label="initial",
            )
        except Exception as exc:
            print("\n===== ADB fallback 也失败 =====")
            print(f"原始错误: {exc}")
            print("请运行 adb devices 确认设备连接，并确认系统 PATH 中可以直接执行 adb。")
            sys.exit(3)
    recognition = gui_context["captcha_recognition"]
    solution = gui_context["captcha_solution"]

    print(f"\nHierarchy saved to: {hierarchy_path}")
    print(f"Runtime output dir: {run_dir}")
    print(f"Backend: {backend}")
    print(f'Screenshot: {gui_context["global_interface_context"].get("screenshot_path")}')
    print_json("Global Interface Context", summarize_global_interface_context(gui_context["global_interface_context"]))
    print_json("CAPTCHA Recognition", recognition)
    print_json(
        "CAPTCHA Solution",
        {
            key: value
            for key, value in solution.items()
            if key not in {"components", "llm_prompt"}
        },
    )
    print_json("Solving Prompt", {"llm_prompt": solution.get("llm_prompt")})

    if not args.execute:
        print("\nDry run complete. Re-run with --execute to perform generated actions.")
        return

    print("\nExecuting generated solution actions...")
    current_context = gui_context
    current_solution = solution
    if current_solution.get("actions"):
        last_feedback = None
        for attempt in range(1, max(1, args.max_attempts) + 1):
            print(f"\nVerification attempt {attempt}/{max(1, args.max_attempts)}...")
            if not current_solution.get("actions"):
                print("No executable actions were generated for this refinement round.")
                break

            if backend == "adb":
                executed_actions = execute_solution_adb(current_solution, serial=args.serial)
                _, after_context, after_hierarchy_path = acquire_gui_information_adb(
                    save_path=run_dir,
                    serial=args.serial,
                    model_config=model_config,
                    capture_label=f"after_attempt_{attempt}",
                )
                feedback = verify_and_refine(current_context, after_context, current_solution)
                feedback["executed_actions"] = executed_actions
                feedback["after_hierarchy_path"] = after_hierarchy_path
            else:
                executed_actions = execute_solution(device, current_solution)
                _, after_context, after_hierarchy_path = acquire_gui_information(
                    device,
                    run_dir,
                    model_config=model_config,
                    capture_label=f"after_attempt_{attempt}",
                )
                feedback = verify_and_refine(current_context, after_context, current_solution)
                feedback["executed_actions"] = executed_actions
                feedback["after_hierarchy_path"] = after_hierarchy_path

            last_feedback = feedback
            print_json(f"Verification Feedback Attempt {attempt}", feedback)
            if feedback.get("verification", {}).get("success"):
                break
            if not feedback.get("refinement", {}).get("retry_recommended"):
                break

            current_context = after_context
            current_solution = after_context.get("captcha_solution", {})
        if last_feedback:
            print_json("Final Verification Feedback", last_feedback)
    else:
        print("No executable actions were generated for this screen.")


if __name__ == "__main__":
    main()


