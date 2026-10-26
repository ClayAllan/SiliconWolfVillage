"""Single-process rooms. Credentials authorize every read; room IDs never do."""
from dataclasses import dataclass, field
import hashlib
import secrets
import threading
import time
from contextlib import nullcontext

from service.room_view import audience_key


def digest(secret):
    return hashlib.sha256(str(secret).encode()).hexdigest()


@dataclass
class Room:
    owner: str
    service: object
    replay: object
    lock: object = field(default_factory=threading.RLock)
    grants: dict = field(default_factory=dict)
    sessions: dict = field(default_factory=dict)
    commands: dict = field(default_factory=dict)


class RoomRegistry:
    def __init__(self, max_rooms=32, max_sessions=128, clock=time.time):
        self._rooms = {}
        self._lock = threading.RLock()
        self.max_rooms, self.max_sessions, self.clock = max_rooms, max_sessions, clock

    def register(self, service=None, replay=None):
        replay = service.display_replay if service is not None else replay
        if replay is None:
            raise ValueError("该对局未启用共享回放；请在新局创建前开启")
        with self._lock:
            if len(self._rooms) >= self.max_rooms:
                raise ValueError("房间数量已达上限，请先释放不再使用的房间")
            room_id, secret = secrets.token_urlsafe(12), secrets.token_urlsafe(32)
            self._rooms[room_id] = Room(digest(secret), service, replay)
            return room_id, secret

    def _room(self, room_id):
        with self._lock:
            room = self._rooms.get(room_id)
        if room is None:
            raise PermissionError("房间不存在或已释放，服务重启后不会自动恢复")
        return room

    @staticmethod
    def _owner(room, secret):
        if not secrets.compare_digest(room.owner, digest(secret)):
            raise PermissionError("需要房主管理凭证")

    def issue(self, room_id, owner, view, seat=None, hours=24):
        key = audience_key(view, seat)
        if not isinstance(hours, (int, float)) or not 0 < hours <= 168:
            raise ValueError("有效期须在 0 到 168 小时之间")
        room = self._room(room_id)
        with room.lock:
            self._owner(room, owner)
            if not room.replay.timelines[key]:
                raise ValueError("此档案没有该视角的展示记录")
            if len(room.grants) >= 64:
                raise ValueError("邀请码数量已达上限")
            grant, secret = secrets.token_urlsafe(8), secrets.token_urlsafe(32)
            room.grants[grant] = {"secret": digest(secret), "audience": key,
                                  "expires": self.clock() + hours * 3600, "revoked": False}
            return grant, secret

    def revoke(self, room_id, owner, grant):
        room = self._room(room_id)
        with room.lock:
            self._owner(room, owner)
            if grant not in room.grants:
                raise ValueError("邀请码不存在")
            room.grants[grant]["revoked"] = True

    def invitations(self, room_id, owner):
        room = self._room(room_id)
        with room.lock:
            self._owner(room, owner)
            return [{"id": key, **{k: v for k, v in row.items() if k != "secret"}}
                    for key, row in room.grants.items()]

    def join(self, room_id, secret, browser_session):
        room = self._room(room_id)
        with room.lock:
            grant = next((key for key, g in room.grants.items() if secrets.compare_digest(g["secret"], digest(secret))
                          and not g["revoked"] and g["expires"] > self.clock()), None)
            if grant is None:
                raise PermissionError("邀请码无效、已撤销或过期")
            room.sessions = {k: v for k, v in room.sessions.items()
                             if not room.grants[v["grant"]]["revoked"] and room.grants[v["grant"]]["expires"] > self.clock()
                             and v["browser"] != browser_session}
            if len(room.sessions) >= self.max_sessions:
                raise PermissionError("观战连接已达上限")
            token = secrets.token_urlsafe(32)
            room.sessions[digest(token)] = {"grant": grant, "browser": browser_session}
            return token

    def leave(self, room_id, token, browser_session):
        room = self._room(room_id)
        with room.lock:
            record = room.sessions.get(digest(token))
            if record and record["browser"] == browser_session:
                room.sessions.pop(digest(token), None)

    def _audience(self, room, token, browser):
        session = room.sessions.get(digest(token))
        if not session or session["browser"] != browser:
            raise PermissionError("观战会话无效")
        grant = room.grants[session["grant"]]
        if grant["revoked"] or grant["expires"] <= self.clock():
            raise PermissionError("观战权限已撤销或过期")
        return grant["audience"]

    def read(self, room_id, token, browser, cursor=None):
        room = self._room(room_id)
        with room.lock:
            audience = self._audience(room, token, browser)
            with room.service._condition if room.service else nullcontext():
                view = room.replay.read(audience, cursor)
                timeline = room.replay.timeline(audience)
                return {"view": view, "timeline": timeline, "issue": room.replay.issue,
                        "running": bool(room.service and room.service._running),
                        "done": bool(room.service is None or room.service._done)}

    def owner_read(self, room_id, owner, audience="omniscient", cursor=None):
        room = self._room(room_id)
        with room.lock:
            self._owner(room, owner)
            with room.service._condition if room.service else nullcontext():
                return {"view": room.replay.read(audience, cursor), "timeline": room.replay.timeline(audience),
                        "issue": room.replay.issue, "running": bool(room.service and room.service._running),
                        "done": bool(room.service is None or room.service._done),
                        "revision": room.service.revision if room.service else None}

    def control(self, room_id, owner, command_id, expected_revision, command):
        room = self._room(room_id)
        with room.lock:
            self._owner(room, owner)
            if command not in {"pause", "play", "step", "request_skip_current", "retry_display_recording"} or not room.service:
                raise ValueError("不支持的对局命令")
            with room.service._condition:
                if command_id in room.commands:
                    previous_command, previous_result = room.commands[command_id]
                    if previous_command != command:
                        raise ValueError("命令编号已用于其他操作")
                    return previous_result
                if expected_revision != room.service.revision:
                    raise ValueError("对局已推进，请刷新后重试")
                result = getattr(room.service, command)()
                room.commands[command_id] = (command, result)
                if len(room.commands) > 1024:
                    room.commands.pop(next(iter(room.commands)))
                return result

    def release(self, room_id, owner):
        room = self._room(room_id)
        with room.lock:
            self._owner(room, owner)
            with room.service._condition if room.service else nullcontext():
                if room.service and not (room.service._done or room.service._closed):
                    raise ValueError("请先结束对局；释放观战房间不会停止后台推演")
                for grant in room.grants.values():
                    grant["revoked"] = True
                with self._lock:
                    self._rooms.pop(room_id, None)


def get_registry():
    # Module-owned singleton survives ordinary Streamlit page reruns.
    return _REGISTRY


_REGISTRY = RoomRegistry()
