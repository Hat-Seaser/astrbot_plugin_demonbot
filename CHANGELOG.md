# v2.9.6

- 修复 `_known_groups`、`_last_owner_message_at`、`_last_poetry_push_at` 未初始化导致的事件处理崩溃。
- 修复私聊指令日志使用未定义 `sender` 导致的 NameError。
- 修复 Poke 事件过滤：非 Poke / 戳别人时不再调用 `stop_event()`，不再吞掉普通聊天和命令。
