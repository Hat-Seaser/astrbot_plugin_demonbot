"""歌词识别辅助。

出于版权限制，本模块默认只做“识别歌曲”而不自动输出受版权保护歌曲的下一句。
用户可以自行维护 data/plugin_data/demonbot/music_clues.json；如果其中的内容是用户有权使用的歌词，
可以保存 next_reply 作为自有语料的固定回复。
"""
from __future__ import annotations
import json
from pathlib import Path
import re

def _load(path:Path):
    try:
        if path.is_file():
            data=json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data,list) else []
    except Exception:
        pass
    return []

def match_lyric_clue(text:str,path:Path):
    t=re.sub(r"[\s，。！？!?、,]+","",text or "")
    if len(t)<6:
        return None
    for item in _load(path):
        if not isinstance(item,dict):
            continue
        clue=re.sub(r"[\s，。！？!?、,]+","",str(item.get("clue", "")))
        if clue and clue in t:
            title=str(item.get("title","")).strip()
            reply=str(item.get("next_reply","")).strip()
            if reply:
                return reply
            if title:
                return f"这句我认出来了，是《{title}》的歌词，我脑子里已经开始响前奏了。"
    return None
