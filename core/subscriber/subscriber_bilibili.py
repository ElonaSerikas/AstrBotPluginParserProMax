"""Bilibili subscriber adapter"""
from typing import Optional, Any
from .base import BaseSubscriber, SubUpdate, SubUserInfo


class BilibiliSubscriber(BaseSubscriber):
    platform = "bilibili"

    def __init__(self, bili_client=None):
        self._client = bili_client

    async def fetch_updates(self, uid: str) -> list[SubUpdate]:
        if not self._client:
            return []
        dyn = await self._client.get_latest_dynamics(int(uid))
        if not dyn:
            return []
        # Extract basic info from dynamics
        items = dyn.get("items", []) if isinstance(dyn, dict) else []
        updates = []
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            dyn_id = str(item.get("id_str", ""))
            modules = item.get("modules", {})
            desc = ""
            for mod_key in ("module_dynamic", "module_desc"):
                mod = modules.get(mod_key, {})
                if isinstance(mod, dict):
                    desc = mod.get("text", "") or desc
            updates.append(SubUpdate(
                id=dyn_id,
                platform="bilibili",
                uid=uid,
                type="dynamic",
                text=desc[:200],
                url=f"https://t.bilibili.com/{dyn_id}" if dyn_id else "",
            ))
        return updates

    async def get_user_info(self, uid: str) -> Optional[SubUserInfo]:
        if not self._client:
            return None
        try:
            info, _ = await self._client.get_user_info(int(uid))
            if info:
                return SubUserInfo(
                    platform="bilibili",
                    uid=uid,
                    name=str(info.get("name", "")),
                    avatar=str(info.get("face", "")),
                )
        except Exception:
            pass
        return None
