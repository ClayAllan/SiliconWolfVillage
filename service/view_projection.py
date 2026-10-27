"""界面信息投影：单一视角不向展示层传递其他席位身份和推理。"""
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass

from engine import config
from service.live_feed import project_events


def project_api_health(health, viewer_seat=None):
    result = deepcopy(health or {})
    if viewer_seat is None:
        return result
    for row in [result.get("last_diagnostic"), *(result.get("diagnostics") or [])]:
        if row and row.get("seat") not in (None, viewer_seat):
            for key in ("seat", "response_kind", "model", "returned_model", "request_id"):
                row.pop(key, None)
    return result


def faction_counts(engine):
    alive = engine.alive_seats()
    return {
        "狼人": sum(engine.roles[s].camp == "狼人" for s in alive),
        "神职": sum(engine.roles[s].is_god for s in alive),
        "平民": sum(engine.players[s]["role"] == "平民" for s in alive),
        "出局": len(engine.players) - len(alive),
    }


@dataclass
class SpectatorView:
    game_id: str
    board_name: str
    day: int
    phase: str
    sheriff: int | None
    winner: str | None
    current_speaker: int | None
    players: dict
    public_log: list
    reasoning_log: dict
    skill_states: dict
    war_board: dict | None
    wolves: list
    role_seats: dict
    counts: dict | None
    initial_counts: dict
    viewer_seat: int | None
    feed_issues: tuple = ()
    human_action: dict | None = None

    def alive_seats(self):
        return [seat for seat, player in self.players.items() if player["alive"]]

    @property
    def limited(self):
        return self.viewer_seat is not None


def project_view(engine, viewer_seat=None):
    if viewer_seat is not None and viewer_seat not in engine.players:
        raise ValueError("无效的代入席位")
    is_wolf_viewer = (
        viewer_seat is not None
        and viewer_seat in engine.roles
        and getattr(engine.roles[viewer_seat], "camp", "") == "狼人"
    )
    if viewer_seat is None:
        visible = set(engine.players)
    elif is_wolf_viewer:
        visible = {viewer_seat} | set(engine.wolves)
    else:
        visible = {viewer_seat}

    # A player's true alive flag changes before a night death is publicly announced.
    # Limited views must use the published liveness ledger, not internal engine state.
    players = {
        seat: {"role": player["role"] if seat in visible else "未知",
               "alive": (player["alive"] if viewer_seat is None
                         else engine.published_alive.get(seat, True)),
               "style": player.get("style", "")}
        for seat, player in engine.players.items()
    }
    logs = {
        seat: [{key: deepcopy(row.get(key, "")) for key in ("label", "reasoning", "speech")}
               for row in engine.reasoning_log.get(seat, [])] if (seat in visible if viewer_seat is None else seat == viewer_seat) else []
        for seat in engine.players
    }
    initial = Counter()
    for player in engine.players.values():
        cls = config.ROLE_CLASSES[player["role"]]
        initial["狼人" if cls.camp == "狼人" else "神职" if cls.is_god else "平民"] += 1
    current = engine.current_speaker
    # 夜间其他人的行动座位也不暴露，防止用行动顺序反推角色；狼人在狼刀阶段可感知狼队行动。
    if viewer_seat is not None and ("夜" in engine.phase or engine.day == 0):
        if not (is_wolf_viewer and current in engine.wolves):
            if current != viewer_seat:
                current = None
    war_board_allowed = (viewer_seat is None or is_wolf_viewer)
    wolves_allowed = (viewer_seat is None or is_wolf_viewer)
    public_rows, feed_issues = project_events(engine.public_log, engine.game_id, engine.winner,
                                            limited=viewer_seat is not None)
    return SpectatorView(
        game_id=engine.game_id, board_name=engine.board_name, day=engine.day,
        phase=engine.phase, sheriff=engine.sheriff, winner=engine.winner,
        current_speaker=current, players=players,
        public_log=deepcopy(public_rows),
        reasoning_log=logs,
        skill_states={seat: deepcopy(engine.roles[seat].skill_state()) for seat in (visible if viewer_seat is None else {viewer_seat})},
        war_board=deepcopy(engine.war_board) if war_board_allowed else None,
        wolves=list(engine.wolves) if wolves_allowed else [],
        role_seats=dict(engine.role_seats) if viewer_seat is None else {},
        counts=faction_counts(engine) if viewer_seat is None else None,
        initial_counts=dict(initial), viewer_seat=viewer_seat, feed_issues=feed_issues,
        human_action=(deepcopy(engine.pending_human_action)
                      if viewer_seat is not None
                      and viewer_seat == getattr(engine, "human_seat", None)
                      else None),
    )
