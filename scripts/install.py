# -*- coding: utf-8 -*-
"""把最新 wheel 装进 Spyder 真正在用的那个解释器，并且当场回读校验。

为什么需要它：2026-11-14 排查发现，Release 页明明有 0.8.20、`pip` 也说
"已是最新"，Spyder 里跑的却还是 0.8.13。版本号只写在 METADATA 和
``__init__.py`` 里，装进去的 wheel 与以为装进去的不是同一个时，界面不会
有任何异常，只是新功能永远不出现。

这个脚本把三件事串成一条命令：

1. **挑 wheel** —— ``dist/`` 与 ``latest/`` 下按版本号取最高的那个。固定
   下载链接的文件名版本段固定为 ``0``（为了 URL 永不变），只作兜底；
2. **挑解释器** —— Spyder 独立版自带 ``envs/spyder-runtime/python.exe``，
   插件必须落进 *它的* user site。按「显式参数 → 环境变量 → 独立版固定
   路径 → 当前解释器」找第一个存在的；
3. **回读校验** —— 装完用**目标解释器**重新 import，核对 ``__version__``、
   ``__edition__`` 和几个代表新版能力的符号，对不上就非零退出。

用法::

    python scripts/install.py                 # 装 + 校验
    python scripts/install.py --check         # 只回读当前实际版本，不装
    python scripts/install.py --dry-run       # 只打印将执行的命令
    python scripts/install.py --python X.exe  # 指定解释器
    python scripts/install.py --wheel Y.whl   # 指定 wheel
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WHEEL_RE = re.compile(
    r"^spydercopilotchat-(?P<ver>[0-9][0-9A-Za-z.]*)-py3-none-any\.whl$",
    re.IGNORECASE,
)

# 回读校验要确认的符号：前两个是面板/导航条，后四个是 0.8.18 之后的修复点。
REQUIRED_ATTRS = (
    ("spyder_copilot_chat.widgets.chat_widget", "MessageRail", None),
    ("spyder_copilot_chat.widgets.chat_widget", "CopilotChatPanel", None),
    ("spyder_copilot_chat.widgets.chat_widget", None, "_balance_fences"),
    ("spyder_copilot_chat.widgets.chat_widget", None, "_strip_fence_lines"),
    ("spyder_copilot_chat.widgets.markdown_renderer", None, "parse_blocks"),
    ("spyder_copilot_chat.widgets.chat_widget", "MessageRail", "HOVER_W"),
    ("spyder_copilot_chat.widgets.chat_widget", "CopilotChatPanel", "_flyout_y"),
)

VERIFY_PROBE = r"""
import json
import sys

out = {"version": "", "edition": "", "path": ""}
try:
    import spyder_copilot_chat as m
    out["version"] = getattr(m, "__version__", "")
    out["edition"] = getattr(m, "__edition__", "")
    out["path"] = getattr(m, "__file__", "")
except Exception as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)

checks = __CHECKS__
symbols = {}
try:
    import importlib
    cache = {}
    for module_name, cls_name, attr_name in checks:
        module = cache.get(module_name)
        if module is None:
            module = importlib.import_module(module_name)
            cache[module_name] = module
        if cls_name and not attr_name:
            symbols[cls_name] = hasattr(module, cls_name)
        elif cls_name:
            cls = getattr(module, cls_name, None)
            symbols[cls_name + "." + attr_name] = (
                cls is not None and hasattr(cls, attr_name))
        else:
            symbols[attr_name] = hasattr(module, attr_name)
except Exception as exc:
    out["symbols_error"] = "%s: %s" % (type(exc).__name__, exc)
out["symbols"] = symbols
sys.stdout.write("@@VERIFY@@" + json.dumps(out) + "\n")
"""


def parse_version(text):
    """``0.8.21`` -> ``(0, 8, 21)``；非数字段返回空元组。

    固定链接 wheel 的版本段是 ``0``，解析成 ``(0,)`` —— 天然排在所有真实
    版本之后，只在没有别的候选时才被选中。
    """
    if not text:
        return ()
    parts = []
    for chunk in str(text).split("."):
        digits = re.match(r"^\d+", chunk)
        if not digits:
            return ()
        parts.append(int(digits.group()))
    return tuple(parts)


def version_from_wheel(path):
    """从 wheel 文件名取版本元组；不是本项目的 wheel 返回空元组。"""
    match = WHEEL_RE.match(os.path.basename(path))
    if not match:
        return ()
    return parse_version(match.group("ver"))


def pick_wheel(candidates):
    """在候选路径里挑版本最高的 wheel，返回 ``(路径, 版本元组)``。

    版本相同按修改时间取新的（同一版本重构建过的情况）。文件名排序不可靠：
    ``0.8.9`` 排在 ``0.8.13`` 后面，这正是之前装错版本的直接原因之一。
    """

    best = None
    for path in candidates:
        version = version_from_wheel(path)
        if not version:
            continue
        key = (version, os.path.getmtime(path) if os.path.exists(path) else 0)
        if best is None or key > best[0]:
            best = (key, path, version)
    if best is None:
        return None
    return best[1], best[2]


def wheel_candidates(root=ROOT):
    """``dist/*.whl`` + ``latest/*.whl``，按路径去重。"""
    paths = []
    for folder in ("dist", "latest"):
        paths.extend(glob.glob(os.path.join(root, folder, "*.whl")))
    seen = set()
    unique = []
    for path in paths:
        real = os.path.normcase(os.path.abspath(path))
        if real in seen:
            continue
        seen.add(real)
        unique.append(path)
    return unique


def _default_roots(env):
    """Windows 上 Spyder 独立版的常见安装位置（含非系统盘）。"""
    roots = []
    local = env.get("LOCALAPPDATA") or ""
    if local:
        roots.append(os.path.join(local, "Programs", "Spyder",
                                  "envs", "spyder-runtime", "python.exe"))
        roots.append(os.path.join(local, "Spyder", "envs",
                                  "spyder-runtime", "python.exe"))
    program_data = env.get("PROGRAMDATA") or ""
    if program_data:
        roots.append(os.path.join(program_data, "spyder-6", "envs",
                                  "spyder-runtime", "python.exe"))
    # Spyder 允许装到任意盘：D:\ProgramData\spyder-6 这类很常见。
    for drive in ("C", "D", "E", "F", "G"):
        roots.append("%s:\\ProgramData\\spyder-6\\envs\\spyder-runtime\\python.exe"
                     % drive)
    return roots


def _from_launcher(env):
    """``spyder`` 启动器在 PATH 上时，由它反推同环境的 python.exe。"""
    launcher = shutil.which("spyder", path=env.get("PATH"))
    if not launcher:
        return []
    scripts = os.path.dirname(launcher)
    if os.path.basename(scripts).lower() != "scripts":
        return []
    candidate = os.path.join(os.path.dirname(scripts), "python.exe")
    return [candidate]


def discover_interpreters(explicit=None, env=None, roots=None, current=None):
    """按优先级返回存在的解释器路径（去重、保序）。

    ``roots=None`` 用默认候选；传 ``()`` 表示不查默认路径（测试用）。
    """
    env = os.environ if env is None else env
    current = sys.executable if current is None else current
    ordered = []

    if explicit:
        ordered.append(explicit)
    ordered.append(env.get("SPYDER_COPILOT_PYTHON") or "")
    ordered.extend(_from_launcher(env))
    ordered.extend(_default_roots(env) if roots is None else roots)
    ordered.append(current)

    result = []
    seen = set()
    for path in ordered:
        if not path:
            continue
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        if os.path.isfile(path):
            result.append(os.path.abspath(path))
    return result


def build_pip_cmd(python, wheel, user_site=True):
    cmd = [python, "-m", "pip", "install", "--no-deps",
           "--upgrade", "--force-reinstall"]
    if user_site:
        cmd.append("--user")
    cmd.append(wheel)
    return cmd


def build_probe_cmd(python):
    """回读用的命令。

    ``-E`` 忽略 ``PYTHONPATH`` 等环境变量；再配合临时工作目录，避免
    "在项目根跑一下就 import 到源码" —— 那样读到的版本号和装进去的
    wheel 根本不是一回事，校验等于没做。
    """
    probe = VERIFY_PROBE.replace("__CHECKS__", repr(tuple(REQUIRED_ATTRS)))
    return [python, "-E", "-c", probe]


def run(cmd, timeout=600, cwd=None):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          timeout=timeout, cwd=cwd)


def read_installed(python):
    """用目标解释器回读实际生效的版本与符号。

    故意在临时目录里跑：在项目根执行会先 import 到工作树里的源码，
    读出来的版本与装进 site-packages 的 wheel 无关。
    """
    try:
        proc = run(build_probe_cmd(python), timeout=180,
                   cwd=tempfile.gettempdir())
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": "无法启动 %s：%s" % (python, exc)}
    for line in (proc.stdout or "").splitlines():
        if line.startswith("@@VERIFY@@"):
            try:
                return json.loads(line[len("@@VERIFY@@"):])
            except ValueError:
                break
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return {"error": "探测无输出" + ("：" + detail[-1] if detail else "")}


def verify_ok(info, expected=None):
    """返回 (是否通过, 问题列表)。"""
    problems = []
    if info.get("error"):
        problems.append(info["error"])
        return False, problems
    version = info.get("version") or ""
    if not version:
        problems.append("未读到 __version__")
    elif expected and version != expected:
        problems.append("版本不符：期望 %s，实际 %s" % (expected, version))
    if info.get("symbols_error"):
        problems.append("符号检查失败（缺 qtpy/Qt？）：" + info["symbols_error"])
    missing = [name for name, ok in (info.get("symbols") or {}).items() if not ok]
    if missing:
        problems.append("缺少符号：" + ", ".join(sorted(missing)))
    return not problems, problems


def _ensure_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _fmt(cmd):
    return " ".join('"%s"' % part if " " in part else part for part in cmd)


def _print_info(info):
    if info.get("error"):
        print("  回读失败：%s" % info["error"])
        return
    print("  版本：%s（%s）" % (info.get("version") or "?", info.get("edition") or "?"))
    print("  路径：%s" % (info.get("path") or "?"))
    symbols = info.get("symbols") or {}
    if symbols:
        state = "全部就位" if all(symbols.values()) else "有缺失"
        print("  能力符号：%d 项，%s" % (len(symbols), state))


def main(argv=None):
    _ensure_utf8()
    parser = argparse.ArgumentParser(
        description="把最新 wheel 装进 Spyder 实际使用的解释器并回读校验")
    parser.add_argument("--check", action="store_true",
                        help="只回读当前版本，不安装")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将执行的命令")
    parser.add_argument("--python", help="指定解释器（默认自动探测）")
    parser.add_argument("--wheel", help="指定 wheel（默认取 dist/latest 里最高版本）")
    parser.add_argument("--no-user", action="store_true",
                        help="不加 --user，直接装进目标环境")
    args = parser.parse_args(argv)

    chosen_wheel = None
    expected = None
    if args.wheel:
        chosen_wheel = os.path.abspath(args.wheel)
        expected = ".".join(str(part) for part in version_from_wheel(chosen_wheel))
    else:
        picked = pick_wheel(wheel_candidates())
        if picked:
            chosen_wheel, version = picked
            expected = ".".join(str(part) for part in version)

    interpreters = discover_interpreters(explicit=args.python)
    if not interpreters:
        print("没有找到可用的解释器；用 --python 指定 Spyder 的 "
              "envs\\spyder-runtime\\python.exe")
        return 1
    target = interpreters[0]

    print("目标解释器：%s" % target)
    if len(interpreters) > 1:
        print("  其他候选：%s" % "；".join(interpreters[1:]))

    if args.check:
        print("回读当前实际生效的版本：")
        info = read_installed(target)
        _print_info(info)
        ok, problems = verify_ok(info, expected)
        for problem in problems:
            print("  ! " + problem)
        return 0 if ok else 2

    if not chosen_wheel:
        print("dist/ 与 latest/ 里都没有可用 wheel，先构建：python -m build --wheel")
        return 1
    print("选用 wheel：%s" % chosen_wheel)

    cmd = build_pip_cmd(target, chosen_wheel, user_site=not args.no_user)
    print("安装命令：%s" % _fmt(cmd))
    if args.dry_run:
        print("（--dry-run，未执行）")
        return 0

    proc = run(cmd)
    tail = [line for line in (proc.stdout or "").splitlines() if line.strip()][-3:]
    for line in tail:
        print("  " + line)
    if proc.returncode != 0 and not args.no_user:
        print("带 --user 失败，改为直接装进目标环境重试……")
        cmd = build_pip_cmd(target, chosen_wheel, user_site=False)
        proc = run(cmd)
        tail = [line for line in (proc.stdout or "").splitlines() if line.strip()][-3:]
        for line in tail:
            print("  " + line)
    if proc.returncode != 0:
        print("安装失败（exit %d）：" % proc.returncode)
        print((proc.stderr or "").strip()[-600:])
        return 1

    print("回读校验：")
    info = read_installed(target)
    _print_info(info)
    ok, problems = verify_ok(info, expected)
    if not ok:
        for problem in problems:
            print("  ! " + problem)
        print("装是装完了，但目标解释器读到的不是期望版本 —— "
              "检查是否有第二个同名包抢占 sys.path。")
        return 2
    print("OK：%s 已就位。完全退出 Spyder（含托盘图标）再启动才会加载。"
          % (info.get("version") or ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
