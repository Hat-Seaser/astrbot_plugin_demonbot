# 焦糖 v2.7.0 自定义位置

- `responses.py`：讨饭、TTS、识图、安慰等固定话术。
- `persona_template.md`：首次启动时生成 data/plugin_data/demonbot/persona.md 的默认人格模板。
- `life.py`：随机作息、周末偶发加班、随机事件。
- `quotes.py`：文案接口与本地兜底文案。
- `command_menu.py`：一级/二级菜单文字。
- `main.py`：指令实际执行逻辑；历史固定回复仍有一部分在这里。

长期人格档案实际文件：`/root/astrbot/data/plugin_data/demonbot/persona.md`

指令：
- `/发送人格`：发送当前人格文件。
- `/添加人格 内容`：追加人格内容。
- `/替换人格 内容`：整份覆盖。
- `/记住信息 内容`：追加一条长期自我信息。
\n\n## v2.9.0 维护说明\n- 长期人格事实源：`data/plugin_data/demonbot/persona.md`\n- 模板：插件目录 `persona_template.md`\n- 菜单：`command_menu.py`\n- 固定回复：`responses.py`\n- `/token` 已移除：插件无法读取 DeepSeek 平台后台账单，因此不再显示可能与平台实际用量不一致的本地估算。\n- 普通 Pixiv：默认月榜优先，并提高最低收藏门槛，按标签命中、收藏、榜单名次、分辨率综合排序。\n