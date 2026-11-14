"""Live co-editing relay (Yjs protocol over WebSocket).

Each shared document or grid is a room. The server keeps one CRDT document per room, applies every edit,
broadcasts it to everyone in the room, relays presence (who is here, where their cursor is), and saves the
room state to the database a few seconds after the last change, so a room survives restarts.
Edit rights are checked per connection: a user without edit permission receives updates but cannot send them.
"""
from __future__ import annotations

import asyncio
import time

from pycrdt import Doc, YMessageType, YSyncMessageType, create_sync_message, create_update_message, handle_sync_message

from .db import CollabRoom, session


class Room:
    def __init__(self, name: str):
        self.name = name
        self.doc = Doc()
        self.clients: dict = {}          # websocket -> can_edit
        self.dirty = False
        self.last_change = 0.0
        self.loop = asyncio.get_event_loop()
        with session() as s:
            r = s.get(CollabRoom, name)
            if r is not None and r.state:
                try:
                    self.doc.apply_update(r.state)
                except Exception:
                    pass
        self.sub = self.doc.observe(self._on_update)

    def _on_update(self, event):
        msg = create_update_message(event.update)
        self.dirty, self.last_change = True, time.time()
        for ws in list(self.clients):
            self.loop.create_task(_send(ws, msg))

    def save(self):
        if not self.dirty:
            return
        state = self.doc.get_update()
        with session() as s:
            r = s.get(CollabRoom, self.name)
            if r is None:
                s.add(CollabRoom(room=self.name, state=state))
            else:
                r.state = state
        self.dirty = False


ROOMS: dict[str, Room] = {}
_saver_started = False


async def _send(ws, data: bytes):
    try:
        await ws.send_bytes(data)
    except Exception:
        pass


async def _saver():
    while True:
        await asyncio.sleep(3)
        for name, room in list(ROOMS.items()):
            try:
                if room.dirty and time.time() - room.last_change > 1.5:
                    room.save()
                if not room.clients and not room.dirty and time.time() - room.last_change > 300:
                    ROOMS.pop(name, None)
            except Exception:
                pass


def has_state(name: str) -> bool:
    if name in ROOMS and ROOMS[name].doc.get_update() not in (b"", b"\x00\x00"):
        return len(ROOMS[name].doc.get_update()) > 2
    with session() as s:
        r = s.get(CollabRoom, name)
        return bool(r and r.state and len(r.state) > 2)


def claim_seed(name: str, who: str) -> bool:
    """The first person to open an empty room seeds it with the current content; everyone else syncs."""
    if has_state(name):
        return False
    with session() as s:
        r = s.get(CollabRoom, name)
        if r is None:
            s.add(CollabRoom(room=name, state=b"", seeded_by=who))
            return True
        if r.seeded_by and (time.time() - (r.updated_at.timestamp() if r.updated_at else 0)) < 20 and r.seeded_by != who:
            return False
        r.seeded_by = who
        return True


async def serve(ws, name: str, can_edit: bool):
    global _saver_started
    if not _saver_started:
        _saver_started = True
        asyncio.get_event_loop().create_task(_saver())
    room = ROOMS.get(name) or ROOMS.setdefault(name, Room(name))
    room.clients[ws] = can_edit
    try:
        await ws.send_bytes(create_sync_message(room.doc))
        while True:
            msg = await ws.receive_bytes()
            if not msg:
                continue
            kind = msg[0]
            if kind == YMessageType.SYNC:
                sub = msg[1] if len(msg) > 1 else None
                if sub in (YSyncMessageType.SYNC_STEP2, YSyncMessageType.SYNC_UPDATE) and not can_edit:
                    continue
                reply = handle_sync_message(msg[1:], room.doc)
                if reply is not None:
                    await ws.send_bytes(reply)
            elif kind == YMessageType.AWARENESS:
                for other in list(room.clients):
                    if other is not ws:
                        await _send(other, msg)
    except Exception:
        pass
    finally:
        room.clients.pop(ws, None)
        try:
            room.save()
        except Exception:
            pass
