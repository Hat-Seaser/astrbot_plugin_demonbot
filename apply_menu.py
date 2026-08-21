"""给当前 GitHub v2.6.x 的 main.py 自动加入精简菜单。

用法：
1. 把 command_menu.py 和本文件放进插件目录；
2. 在插件目录执行：python3 apply_menu_update.py main.py；
3. 脚本会先生成 main.py.menu_backup；
4. 重载插件。
"""
from pathlib import Path
import re
import shutil
import sys

path = Path(sys.argv[1] if len(sys.argv) > 1 else "main.py")
text = path.read_text(encoding="utf-8")
backup = path.with_name(path.name + ".menu_backup")
shutil.copy2(path, backup)

# 1) 增加菜单模块导入
if "from . import command_menu" not in text:
    marker = "try:\n    from . import stickers\n"
    if marker in text:
        text = text.replace(marker, "from . import command_menu\n\n" + marker, 1)
    else:
        raise SystemExit("找不到 stickers 导入位置，未修改 main.py。")

# 2) COMMAND_TABLE 中增加 /其他指令，并把发送日志从主菜单移走。
old = '''        ("帮助",     ["help", "菜单", "指令"], "",            "看这张指令表",                     "基础", False),
        ("状态",     ["恶魔状态"],            "",            "运行状态总览",                     "基础", False),
        ("自检",     ["诊断"],                "",            "逐项体检，插件出问题先发这个",       "基础", False),
        ("版本",     ["ver"],                 "",            "看插件版本和已加载的模块",          "基础", False),
        ("发送日志", ["日志", "运行日志"],     "500/全部",     "私聊把运行日志发给你（仅管理员，仅私聊）", "基础", True),
'''
new = '''        ("帮助",     ["help", "菜单", "指令"], "",            "看常用指令",                     "基础", False),
        ("其他指令", ["其它指令", "更多指令", "全部指令"], "", "查看不常用、诊断和管理员指令", "基础", False),
        ("状态",     ["恶魔状态"],            "",            "运行状态总览",                     "基础", False),
        ("自检",     ["诊断"],                "",            "逐项体检，插件出问题先发这个",       "基础", False),
        ("版本",     ["ver"],                 "",            "看插件版本和已加载的模块",          "基础", False),
'''
if old not in text:
    raise SystemExit("没有找到 v2.6 COMMAND_TABLE 的基础指令段，说明你的 main.py 版本结构不同；已恢复备份。")
text = text.replace(old, new, 1)

# 3) 用集中式菜单替换 _cmd_help，并新增 _cmd_other_help。
pattern = re.compile(r'    def _cmd_help\(self\) -> str:\n.*?\n    def _cmd_status\(self, group_id: str\) -> str:', re.S)
replacement = '''    def _cmd_help(self) -> str:
        """常用指令菜单。具体文字统一放在 command_menu.py。"""
        prefix = (self._cfg("commands", "prefixes", default=None) or ["/"])[0]
        return command_menu.render_main(self.PLUGIN_VERSION, prefix)

    def _cmd_other_help(self) -> str:
        """不常用/管理/诊断指令菜单。具体文字统一放在 command_menu.py。"""
        prefix = (self._cfg("commands", "prefixes", default=None) or ["/"])[0]
        return command_menu.render_other(self.PLUGIN_VERSION, prefix)

    def _cmd_status(self, group_id: str) -> str:'''
text, n = pattern.subn(replacement, text, count=1)
if n != 1:
    raise SystemExit("没有找到 _cmd_help，已恢复备份。")

# 4) dispatch 增加 /其他指令。
needle = '''        if name == "帮助":
            return self._cmd_help()
        if name == "状态":'''
repl = '''        if name == "帮助":
            return self._cmd_help()
        if name == "其他指令":
            return self._cmd_other_help()
        if name == "状态":'''
if needle not in text:
    raise SystemExit("没有找到帮助指令分发位置，已恢复备份。")
text = text.replace(needle, repl, 1)

path.write_text(text, encoding="utf-8")
print(f"已修改：{path}")
print(f"备份：{backup}")
print("新增：/其他指令")
print("菜单文字集中在：command_menu.py")
