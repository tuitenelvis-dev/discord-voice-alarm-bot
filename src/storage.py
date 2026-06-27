from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any


DEFAULT_STATE: dict[str, Any] = {
    "stay_channels": {},
    "alarms": [],
}


class JsonStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] = deepcopy(DEFAULT_STATE)

    async def load(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            await self.save()
            return

        async with self._lock:
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                loaded = deepcopy(DEFAULT_STATE)

            state = deepcopy(DEFAULT_STATE)
            state.update(loaded)
            self._state = state

    async def save(self) -> None:
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return deepcopy(self._state)

    async def set_stay_channel(self, guild_id: int, channel_id: int) -> None:
        async with self._lock:
            self._state["stay_channels"][str(guild_id)] = channel_id
        await self.save()

    async def remove_stay_channel(self, guild_id: int) -> None:
        async with self._lock:
            self._state["stay_channels"].pop(str(guild_id), None)
        await self.save()

    async def add_alarm(self, alarm: dict[str, Any]) -> None:
        async with self._lock:
            self._state["alarms"].append(alarm)
        await self.save()

    async def remove_alarm(self, alarm_id: str) -> bool:
        async with self._lock:
            before = len(self._state["alarms"])
            self._state["alarms"] = [
                alarm for alarm in self._state["alarms"] if alarm["id"] != alarm_id
            ]
            removed = len(self._state["alarms"]) != before
        await self.save()
        return removed

    async def remove_alarms(self, alarm_ids: set[str]) -> None:
        async with self._lock:
            self._state["alarms"] = [
                alarm for alarm in self._state["alarms"] if alarm["id"] not in alarm_ids
            ]
        await self.save()
