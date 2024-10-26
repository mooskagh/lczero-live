from typing import Optional, TypedDict

import anyio
import db
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream


class GamePositionUpdate(TypedDict):
    ply: int  # 0 for startpos
    thinkingId: Optional[int]
    moveUci: Optional[str]
    moveSan: Optional[str]
    fen: str
    whiteClock: Optional[int]
    blackClock: Optional[int]
    scoreQ: Optional[int]
    scoreW: Optional[int]
    scoreD: Optional[int]
    scoreB: Optional[int]
    movesLeft: Optional[int]


class GamePositionUpdateFrame(TypedDict, total=False):
    positions: list[GamePositionUpdate]


def make_moves_update_frame(
    positions: list[db.GamePosition],
    thinkings: Optional[list[Optional[db.GamePositionThinking]]] = None,
) -> GamePositionUpdateFrame:
    if thinkings is None:
        thinkings = [None for x in range(len(positions))]
    moves_websocket_frame = GamePositionUpdateFrame(positions=[])

    for pos, thinking in zip(positions, thinkings):
        moves_websocket_frame.setdefault("positions", []).append(
            GamePositionUpdate(
                ply=pos.ply_number,
                thinkingId=thinking.id if thinking else None,
                moveUci=pos.move_uci,
                moveSan=pos.move_san,
                fen=pos.fen,
                whiteClock=pos.white_clock,
                blackClock=pos.black_clock,
                scoreQ=thinking.q_score if thinking else None,
                scoreW=thinking.white_score if thinking else None,
                scoreD=thinking.draw_score if thinking else None,
                scoreB=thinking.black_score if thinking else None,
                movesLeft=thinking.moves_left if thinking else None,
            )
        )

    return moves_websocket_frame


class GameThinkingMoveUpdate(TypedDict):
    nodes: int
    moveUci: str
    moveOppUci: Optional[str]
    moveSan: str
    pvSan: str
    scoreQ: int
    scoreW: int
    scoreD: int
    scoreB: int
    mateScore: Optional[int]
    movesLeft: Optional[int]


class GameThinkingUpdate(TypedDict):
    updateId: int
    nodes: int
    time: int
    depth: int
    seldepth: int
    moves: list[GameThinkingMoveUpdate]


class GameThinkingUpdateFrame(TypedDict):
    thinkings: list[GameThinkingUpdate]


def make_thinking_update_frame(
    thinkings: list[db.GamePositionEvaluation],
    moves: list[list[db.GamePositionEvaluationMove]],
) -> GameThinkingUpdateFrame:
    thinking_websocket_frame = GameThinkingUpdateFrame(thinkings=[])
    for thinking, thinking_moves in zip(thinkings, moves):
        moves_update: list[GameThinkingMoveUpdate] = []
        for move in thinking_moves:
            moves_update.append(
                GameThinkingMoveUpdate(
                    nodes=move.nodes,
                    moveUci=move.move_uci,
                    moveOppUci=move.move_opp_uci,
                    moveSan=move.move_san,
                    pvSan=move.pv_san,
                    scoreQ=move.q_score,
                    scoreW=move.white_score,
                    scoreD=move.draw_score,
                    scoreB=move.black_score,
                    mateScore=move.mate_score,
                    movesLeft=move.moves_left,
                )
            )
        thinking_websocket_frame.setdefault("thinkings", []).append(
            GameThinkingUpdate(
                updateId=thinking.id,
                nodes=thinking.nodes,
                time=thinking.time,
                depth=thinking.depth,
                seldepth=thinking.seldepth,
                moves=moves_update,
            )
        )

    return thinking_websocket_frame


class WebsocketNotifier:
    _move_observers: set[MemoryObjectSendStream[GamePositionUpdateFrame]]
    _thinking_observers: set[MemoryObjectSendStream[GameThinkingUpdateFrame]]
    _current_thinking_update_id: Optional[int] = None

    def __init__(self):
        self._move_observers = set()
        self._thinking_observers = set()

    def get_thinking_update_id(self) -> Optional[int]:
        return self._current_thinking_update_id

    async def set_thinking_update_id(self, thinking_id: int):
        if self._current_thinking_update_id != thinking_id:
            self._current_thinking_update_id = thinking_id
            async with anyio.create_task_group() as tg:
                for observer in self._thinking_observers:
                    tg.start_soon(observer.aclose)
            self._thinking_observers.clear()

    def add_thinking_observer(
        self,
    ) -> MemoryObjectReceiveStream[GameThinkingUpdateFrame]:
        send_stream, recv_stream = anyio.create_memory_object_stream[
            GameThinkingUpdateFrame
        ]()
        self._thinking_observers.add(send_stream)
        return recv_stream

    async def notify_thinking_observers(
        self,
        thinkings: list[db.GamePositionEvaluation],
        moves: list[list[db.GamePositionEvaluationMove]],
    ):
        if not thinkings:
            return

        thinkings_websocket_frame = make_thinking_update_frame(
            thinkings=thinkings, moves=moves
        )

        new_observers = set()
        for observer in self._thinking_observers:
            try:
                await observer.send(thinkings_websocket_frame)
                new_observers.add(observer)
            except anyio.EndOfStream:
                await observer.aclose()
            except anyio.BrokenResourceError:
                await observer.aclose()
        self._thinking_observers = new_observers

    def add_moves_observer(
        self,
    ) -> MemoryObjectReceiveStream[GamePositionUpdateFrame]:
        send_stream, recv_stream = anyio.create_memory_object_stream[
            GamePositionUpdateFrame
        ]()
        self._move_observers.add(send_stream)
        return recv_stream

    async def notify_move_observers(
        self,
        positions: list[db.GamePosition],
        thinkings: Optional[list[Optional[db.GamePositionThinking]]] = None,
    ):
        if not positions:
            return

        moves_websocket_frame = make_moves_update_frame(
            positions=positions, thinkings=thinkings
        )

        new_observers = set()
        for observer in self._move_observers:
            try:
                await observer.send(moves_websocket_frame)
                new_observers.add(observer)
            except anyio.EndOfStream:
                await observer.aclose()
            except anyio.BrokenResourceError:
                await observer.aclose()
        self._move_observers = new_observers
