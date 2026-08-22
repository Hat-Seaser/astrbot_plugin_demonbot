# v2.9.5

- 修复 on_poke 使用 ALL 事件导致普通消息被 stop_event 拦截的问题。
- 只有明确识别为 OneBot/NapCat Poke 通知时才处理戳一戳。
- 戳到其他人的 Poke 不回复，也不阻断后续消息。
- 保留 v2.9.4 菜单与诗词功能。
