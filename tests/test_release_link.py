# -*- coding: utf-8 -*-
"""v0.8.22 回归：README 的一行安装命令必须真的能取到最新 Release。

现场：按 README 装完 0.8.21，重启后界面又退回 0.8.13。旧命令指向
jsDelivr 的 ``@main/latest/`` 固定名 wheel，而 CDN 缓存最多滞后约 12 小时，
于是把旧包原样盖了回来，``pip`` 还打印 ``Successfully installed``。

这里的断言盯的不是某一行文字好不好看，而是三件会各自独立失效的事：

1. **URL 指向哪** —— 必须是 ``releases/latest/download/``，不能退回任何 CDN；
2. **那个文件名在不在** —— latest 路径要求文件名逐字固定，所以发布 workflow
   必须每次把带版本号的 wheel 另存一份固定名再上传；读的是 workflow 原文，
   不是文档里怎么写的；
3. **文件名合不合法** —— pip 只收 ``名称-版本-py3-none-any.whl`` 这种形状，
   版本段写成别的东西（比如 ``latest``）会在 pip 解析文件名时就炸掉。

跑法：``python tests/test_release_link.py``（也兼容 pytest）。不需要 Qt。
"""

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "README.md")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "release.yml")

_results = []

ASSET = "spydercopilotchat-0-py3-none-any.whl"
LATEST_PREFIX = "https://github.com/2818194829/SpyderCopilotChat/releases/latest/download/"


def check(name, condition, extra=""):
    _results.append((name, bool(condition), extra))
    if not condition:
        print("FAIL  {0}  {1}".format(name, extra))


def eq(name, got, want):
    check(name, got == want, "got={0!r} want={1!r}".format(got, want))


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _bash_blocks(text):
    """抓出所有 ```bash 围栏里的命令，按出现顺序返回。"""
    blocks, buf, inside = [], [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if inside:
                blocks.append("\n".join(buf))
                buf, inside = [], False
            else:
                inside = stripped[3:].strip().lower() in ("bash", "shell", "powershell", "sh")
            continue
        if inside:
            buf.append(line)
    if inside and buf:
        blocks.append("\n".join(buf))
    return blocks


def _install_section():
    text = _read(README)
    start = text.find("## 安装")
    end = text.find("## 卸载")
    check("README 有安装章节", start >= 0)
    check("安装章节在卸载之前", 0 <= start < end)
    return text[start:end]


def _primary_command():
    blocks = _bash_blocks(_install_section())
    check("安装章节至少有一条命令", bool(blocks))
    return blocks[0] if blocks else ""


def test_primary_command_hits_latest_release():
    cmd = _primary_command()
    check("第一条命令指向 releases/latest/download", LATEST_PREFIX in cmd, cmd[:120])
    check("第一条命令带着固定名 wheel", ASSET in cmd, cmd[:160])
    check("第一条命令不写死版本号",
          not re.search(r"spydercopilotchat-\d+\.\d+", cmd), cmd[:160])


def test_primary_command_has_all_flags():
    cmd = _primary_command()
    for flag in ("--user", "--no-deps", "--upgrade", "--force-reinstall",
                 "--no-cache-dir"):
        check("命令带 " + flag, flag in cmd, cmd)


def test_no_cdn_left():
    text = _read(README)
    check("README 不再指向 jsDelivr", "jsdelivr" not in text.lower())
    check("README 没有再提 @main/latest 路径", "@main/latest" not in text)


def test_workflow_ships_fixed_name_asset():
    text = _read(WORKFLOW)
    check("workflow 把 wheel 复制成固定名",
          'cp "$WHEEL" dist/' + ASSET in text)
    check("固定名副本在发布前生成",
          text.find("cp \"$WHEEL\" dist/") < text.find("action-gh-release"))
    check("发布步骤上传 dist 下的 wheel", "dist/*.whl" in text)
    check("找不到 wheel 时直接失败", 'test -f "$WHEEL"' in text)


def test_readme_and_workflow_use_same_filename():
    cmd = _primary_command()
    match = re.search(r"download/([\w.\-]+\.whl)", cmd)
    check("命令里能解析出文件名", bool(match), cmd[:160])
    eq("README 与 workflow 的文件名一致", match.group(1) if match else "", ASSET)


def test_filename_is_a_legal_wheel_name():
    pattern = r"^(?P<name>[A-Za-z0-9_.]+)-(?P<ver>\d[\w.]*)-(?P<py>py3|py2\.py3)-(?P<abi>none)-(?P<plat>any)\.whl$"
    match = re.match(pattern, ASSET)
    check("固定名符合 wheel 命名规则", bool(match), ASSET)
    if match:
        eq("分发包名可归一化", match.group("name").lower().replace("_", "-"),
           "spydercopilotchat")
        eq("版本段是合法 PEP 440（0）", match.group("ver"), "0")
        eq("Python 标签", match.group("py"), "py3")


def test_fallback_command_uses_same_asset():
    blocks = _bash_blocks(_install_section())
    proxied = [b for b in blocks if "gh-proxy" in b or "ghfast" in b]
    check("给了连不上 github.com 时的加速写法", bool(proxied))
    if proxied:
        check("加速写法指向同一个文件", ASSET in proxied[0], proxied[0][:160])
        check("加速写法也带 --no-cache-dir", "--no-cache-dir" in proxied[0])
        check("加速写法只加了一层前缀",
              "gh-proxy.com/https://github.com/" in proxied[0].replace(" ", ""))


def test_version_consistency():
    init = _read(os.path.join(ROOT, "spyder_copilot_chat", "__init__.py"))
    version = re.search(r'__version__\s*=\s*"([^"]+)"', init).group(1)
    pyproject = _read(os.path.join(ROOT, "pyproject.toml"))
    declared = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M).group(1)
    eq("__init__ 与 pyproject 版本一致", version, declared)
    badge = re.search(r"badge/version-([\d.]+)-blue", _read(README))
    check("README 徽章能解析", bool(badge))
    eq("README 徽章与包版本一致", badge.group(1) if badge else "", version)
    check("README 正文里的当前版本号也是它",
          "当前 " + version in _read(README))


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
