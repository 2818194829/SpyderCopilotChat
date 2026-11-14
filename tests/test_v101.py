# -*- coding: utf-8 -*-
"""v0.8.21 回归：安装脚本能挑对 wheel、找对解释器、装完真的回读。

现场：Release 页有 0.8.20，`pip` 也报成功，Spyder 里跑的却是 0.8.13。
两个独立的坑叠在一起：

1. **挑 wheel 靠文件名排序** —— ``0.8.9`` 排在 ``0.8.13`` 后面，
   ``ls | head`` 拿到的不一定是最新那个（发布 workflow 里为此写过注释）；
2. **校验在项目根跑** —— ``python -c "import spyder_copilot_chat"`` 的
   ``sys.path[0]`` 是当前目录，读到的其实是工作树源码，于是"版本对得上"
   这个结论完全不成立。

所以这版把安装做成一条可测试的命令：按版本元组挑 wheel、按优先级找
Spyder 真正在用的解释器、装完用 ``-E`` + 临时工作目录回读核对版本与
七个能力符号。

跑法：``python tests/test_v101.py``（也兼容 pytest）。不需要 Qt。
"""

import importlib.util
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "install.py")

_results = []


def check(name, condition, extra=""):
    _results.append((name, bool(condition), extra))
    if not condition:
        print("FAIL  {0}  {1}".format(name, extra))


def eq(name, got, want):
    check(name, got == want, "got={0!r} want={1!r}".format(got, want))


def _load_script():
    spec = importlib.util.spec_from_file_location("spyder_install_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INST = _load_script()


def _touch(path, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_parse_version():
    eq("0.8.21 解析成三元组", INST.parse_version("0.8.21"), (0, 8, 21))
    eq("固定链接的 0 解析成 (0,)", INST.parse_version("0"), (0,))
    eq("空串解析成 ()", INST.parse_version(""), ())
    eq("非数字解析成 ()", INST.parse_version("dev"), ())
    eq("带后缀取前导数字", INST.parse_version("0.8.21rc1"), (0, 8, 21))
    check("0.8.9 小于 0.8.13（字符串排序会反过来）",
          INST.parse_version("0.8.9") < INST.parse_version("0.8.13"))
    check("0.8.13 小于 0.8.21",
          INST.parse_version("0.8.13") < INST.parse_version("0.8.21"))


def test_version_from_wheel():
    eq("标准 wheel 名取版本",
       INST.version_from_wheel("spydercopilotchat-0.8.21-py3-none-any.whl"),
       (0, 8, 21))
    eq("固定链接 wheel 版本段是 0",
       INST.version_from_wheel("spydercopilotchat-0-py3-none-any.whl"), (0,))
    eq("大小写不敏感",
       INST.version_from_wheel("SpyderCopilotChat-0.8.21-py3-none-any.whl"),
       (0, 8, 21))
    eq("别的包不算候选",
       INST.version_from_wheel("requests-2.31.0-py3-none-any.whl"), ())
    eq("平台 wheel 不算候选",
       INST.version_from_wheel("spydercopilotchat-0.8.21-cp312-cp312-win.whl"),
       ())


def test_pick_wheel():
    with tempfile.TemporaryDirectory() as tmp:
        old = _touch(os.path.join(tmp, "spydercopilotchat-0.8.9-py3-none-any.whl"))
        mid = _touch(os.path.join(tmp, "spydercopilotchat-0.8.13-py3-none-any.whl"))
        new = _touch(os.path.join(tmp, "spydercopilotchat-0.8.21-py3-none-any.whl"))
        latest = _touch(os.path.join(tmp, "spydercopilotchat-0-py3-none-any.whl"))
        picked = INST.pick_wheel([old, mid, latest, new])
        check("最高版本胜出", picked and os.path.basename(picked[0]).startswith(
            "spydercopilotchat-0.8.21"), repr(picked))
        eq("返回的版本元组一致", picked[1], (0, 8, 21))

        eq("固定链接只作兜底", INST.pick_wheel([latest])[1], (0,))

        same_old = _touch(os.path.join(tmp, "a", "spydercopilotchat-0.9.0-py3-none-any.whl"), 1000)
        same_new = _touch(os.path.join(tmp, "b", "spydercopilotchat-0.9.0-py3-none-any.whl"), 2000)
        picked = INST.pick_wheel([same_old, same_new])
        eq("同版本取 mtime 较新的", picked[0], same_new)

        eq("空候选返回 None", INST.pick_wheel([]), None)
        eq("全无效返回 None", INST.pick_wheel([
            os.path.join(tmp, "readme.txt"),
            os.path.join(tmp, "requests-2.31.0-py3-none-any.whl"),
        ]), None)


def test_wheel_candidates():
    with tempfile.TemporaryDirectory() as tmp:
        _touch(os.path.join(tmp, "dist", "spydercopilotchat-0.8.20-py3-none-any.whl"))
        _touch(os.path.join(tmp, "latest", "spydercopilotchat-0-py3-none-any.whl"))
        paths = INST.wheel_candidates(tmp)
        eq("dist 与 latest 都收", len(paths), 2)
        check("去重后路径唯一",
              len({os.path.normcase(os.path.abspath(p)) for p in paths}) == len(paths))
        check("只收 .whl",
              all(p.endswith(".whl") for p in paths))

    real = INST.wheel_candidates()
    check("仓库里能找到 wheel", bool(real), repr(real[:3]))
    picked = INST.pick_wheel(real)
    check("仓库里最高版本不低于 0.8.20",
          picked is not None and picked[1] >= (0, 8, 20), repr(picked))


def test_discover_interpreters():
    with tempfile.TemporaryDirectory() as tmp:
        a = _touch(os.path.join(tmp, "a", "python.exe"))
        b = _touch(os.path.join(tmp, "b", "python.exe"))
        c = _touch(os.path.join(tmp, "c", "python.exe"))

        got = INST.discover_interpreters(explicit=a, env={"SPYDER_COPILOT_PYTHON": b},
                                         roots=(), current=c)
        eq("显式参数最优先、环境变量次之、当前解释器兜底", got, [a, b, c])

        got = INST.discover_interpreters(explicit=os.path.join(tmp, "nope.exe"),
                                         env={"SPYDER_COPILOT_PYTHON": b},
                                         roots=(), current=c)
        eq("显式路径不存在时跳过", got, [b, c])

        got = INST.discover_interpreters(explicit=a, env={"SPYDER_COPILOT_PYTHON": a},
                                         roots=(), current=a)
        eq("同一解释器只留一次", got, [a])

        got = INST.discover_interpreters(explicit=None, env={},
                                         roots=(b,), current=c)
        eq("roots 插在 current 之前", got, [b, c])

        got = INST.discover_interpreters(explicit=None, env={}, roots=(), current=None)
        eq("current 缺省用当前解释器", got, [sys.executable])


def test_launcher_derivation():
    with tempfile.TemporaryDirectory() as tmp:
        scripts = os.path.join(tmp, "envs", "spyder-runtime", "Scripts")
        conda = os.path.join(tmp, "envs", "spyder-runtime")
        _touch(os.path.join(scripts, "spyder.exe"))
        python = _touch(os.path.join(conda, "python.exe"))
        env = {"PATH": scripts + os.pathsep + os.environ.get("PATH", "")}
        got = INST.discover_interpreters(explicit=None, env=env, roots=(), current=None)
        check("从 spyder 启动器反推出同环境的 python",
              python in got, repr(got))

        got = INST.discover_interpreters(explicit=None, env={"PATH": tmp}, roots=(),
                                         current=None)
        check("没有启动器时不臆造路径",
              all("spyder-runtime" not in p.lower() for p in got) or got == [sys.executable],
              repr(got))


def test_build_commands():
    cmd = INST.build_pip_cmd("P.exe", "W.whl")
    check("装到 user site", "--user" in cmd)
    check("不动依赖", "--no-deps" in cmd)
    check("强制覆盖旧版本", "--force-reinstall" in cmd)
    check("允许升级", "--upgrade" in cmd)
    eq("wheel 是最后一个参数", cmd[-1], "W.whl")
    check("no-user 时不带 --user",
          "--user" not in INST.build_pip_cmd("P.exe", "W.whl", user_site=False))

    probe = INST.build_probe_cmd("P.exe")
    eq("探针第一个参数是解释器", probe[0], "P.exe")
    check("探针用 -E 屏蔽 PYTHONPATH", "-E" in probe)
    check("探针注入能力清单", "__CHECKS__" not in probe[-1])
    check("探针带结果标记", "@@VERIFY@@" in probe[-1])
    check("探针检查导航条放大", "HOVER_W" in probe[-1])


def test_required_attrs():
    names = {(cls, attr) for _, cls, attr in INST.REQUIRED_ATTRS}
    check("覆盖导航条类", ("MessageRail", None) in names)
    check("覆盖面板类", ("CopilotChatPanel", None) in names)
    check("覆盖悬停放大", ("MessageRail", "HOVER_W") in names)
    check("覆盖浮层定位", ("CopilotChatPanel", "_flyout_y") in names)
    check("覆盖围栏自平衡", (None, "_balance_fences") in names)
    check("覆盖围栏剥离", (None, "_strip_fence_lines") in names)


def test_verify_ok():
    good = {"version": "0.8.21", "edition": "installer", "path": "x",
            "symbols": {"MessageRail.HOVER_W": True}}
    ok, problems = INST.verify_ok(good, "0.8.21")
    check("全对时通过", ok and not problems, repr(problems))

    ok, problems = INST.verify_ok(dict(good, version="0.8.13"), "0.8.21")
    check("版本不符要拦下来", not ok, repr(problems))
    check("提示里写清期望与实际",
          any("0.8.13" in p and "0.8.21" in p for p in problems), repr(problems))

    ok, problems = INST.verify_ok(dict(good, symbols={"MessageRail.HOVER_W": False}),
                                  "0.8.21")
    check("符号缺失要拦下来", not ok, repr(problems))
    check("点名缺了哪个符号", any("HOVER_W" in p for p in problems), repr(problems))

    ok, problems = INST.verify_ok({"error": "ImportError: no module"}, "0.8.21")
    check("导入失败算失败", not ok, repr(problems))


def test_check_does_not_import_source():
    """回归：在项目根跑回读时，不能读到工作树里的源码。

    ``read_installed`` 用 ``-E`` + 临时目录。装过包时应该读到 site-packages，
    没装过则报导入失败——两种结果都不该指向 ``ROOT`` 下那份源码。
    """
    info = INST.read_installed(sys.executable)
    path = (info.get("path") or "").lower()
    root = (ROOT + os.sep).lower()
    check("回读不会落到项目工作树",
          not path.startswith(root), repr(info.get("path")))
    check("失败时给出原因",
          bool(info.get("error")) or bool(info.get("version")), repr(info))


def test_cli_dry_run():
    proc = subprocess.run(
        [sys.executable, SCRIPT, "--dry-run", "--python", sys.executable],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=tempfile.gettempdir(), timeout=180)
    out = (proc.stdout or "") + (proc.stderr or "")
    eq("dry-run 退出码 0", proc.returncode, 0)
    check("打印了安装命令", "安装命令" in out, out[-400:])
    check("明确说明未执行", "--dry-run，未执行" in out, out[-400:])
    check("没有真的调用 pip", "Successfully installed" not in out, out[-400:])


def test_cli_check_fails_loudly():
    """期望版本对不上时必须非零退出，不能打印一句「已是最新」就算完。"""
    with tempfile.TemporaryDirectory() as tmp:
        fake = _touch(os.path.join(
            tmp, "spydercopilotchat-9.9.9-py3-none-any.whl"))
        proc = subprocess.run(
            [sys.executable, SCRIPT, "--check", "--python", sys.executable,
             "--wheel", fake],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=tempfile.gettempdir(), timeout=180)
        eq("读不到期望版本时退出码 2", proc.returncode, 2)
        out = (proc.stdout or "") + (proc.stderr or "")
        # 装过包的环境会报「版本不符：期望 9.9.9」，没装过则直接报导入失败。
        check("失败原因可见",
              "9.9.9" in out or "回读失败" in out, out[-300:])


def test_script_compiles():
    proc = subprocess.run([sys.executable, "-m", "py_compile", SCRIPT],
                          capture_output=True, text=True, timeout=120)
    eq("脚本语法通过", proc.returncode, 0)
    check("无 stderr", not (proc.stderr or "").strip(), proc.stderr)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                check(name + " 未抛异常", False, repr(exc))
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    failures = [(n, e) for n, ok, e in _results if not ok]
    print("\n{0}/{1} passed".format(passed, total))
    if failures:
        print("failures:")
        for name, extra in failures:
            print("  -", name, extra)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
