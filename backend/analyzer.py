from typing import Awaitable, Callable, List, Optional

import anyio
import chess
import chess.engine
import chess.pgn
import db
from anyio.streams.memory import MemoryObjectReceiveStream
from pgn_feed import PgnFeed
from sanic.log import logger
from ws_notifier import WebsocketNotifier


def get_leaf_board(pgn: chess.pgn.Game) -> chess.Board:
    board = pgn.board()
    for move in pgn.mainline_moves():
        board.push(move)
    return board


def make_pv_san_string(board: chess.Board, pv: List[chess.Move]) -> str:
    board = board.copy()
    res: str = ""
    if board.turn == chess.BLACK:
        res = f"{board.fullmove_number}…"
    for move in pv:
        if board.turn == chess.WHITE:
            if res:
                res += " "
            res += f"{board.fullmove_number}."
        res += " " + board.san(move)
        board.push(move)
    return res


class Analyzer:
    _config: dict
    _game: Optional[db.Game]
    _current_position: Optional[db.GamePosition]
    _engine: chess.engine.Protocol
    _get_next_task_callback: Callable[[], Awaitable[db.Game]]
    ws_notifier: WebsocketNotifier

    def __init__(
        self, uci_config: dict, next_task_callback: Callable[[], Awaitable[db.Game]]
    ):
        self._config = uci_config
        self._game = None
        self._get_next_task_callback = next_task_callback
        self._current_position = None
        self.ws_notifier = WebsocketNotifier()

    # returns the last ply number.
    async def _update_game_db(
        self, pgn: chess.pgn.Game, game: db.Game
    ) -> db.GamePosition:
        board = pgn.board()
        white_clock: Optional[int] = None
        black_clock: Optional[int] = None

        added_game_positions: list[db.GamePosition] = []

        async def create_pos(
            ply: int,
            move_uci: Optional[str],
            move_san: Optional[str],
        ) -> db.GamePosition:
            res, created = await db.GamePosition.get_or_create(
                game=game,
                ply_number=ply,
                defaults={
                    "fen": board.fen(),
                    "move_uci": move_uci,
                    "move_san": move_san,
                    "white_clock": white_clock,
                    "black_clock": black_clock,
                },
            )
            if created:
                added_game_positions.append(res)

            return res

        last_pos: db.GamePosition = await create_pos(
            ply=0,
            move_uci=None,
            move_san=None,
        )

        for ply, node in enumerate(pgn.mainline(), start=1):
            clock = node.clock()
            if clock:
                if board.turn == chess.WHITE:
                    white_clock = int(clock)
                else:
                    black_clock = int(clock)
            san = board.san(node.move)
            board.push(move=node.move)
            last_pos = await create_pos(
                ply=ply,
                move_uci=node.move.uci(),
                move_san=san,
            )
        await self.ws_notifier.notify_move_observers(positions=added_game_positions)
        return last_pos

    def get_game(self) -> Optional[db.Game]:
        return self._game

    def get_thinking_id(self) -> Optional[int]:
        return self.ws_notifier.get_thinking_update_id()

    async def run(self):
        _, self._engine = await chess.engine.popen_uci(self._config["command"])
        while True:
            self._game = await self._get_next_task_callback()
            await self._run_single_game(self._game)

    async def _run_single_game(self, game: db.Game):
        pgn_send_queue, pgn_recv_queue = anyio.create_memory_object_stream[
            chess.pgn.Game
        ]()
        filters: List[tuple[str, str]] = [
            (f.key, f.value) for f in await db.GameFilter.filter(game=game)
        ]
        url = (
            "https://lichess.org/api/stream/broadcast/round/"
            f"{game.lichess_round_id}.pgn"
        )
        async with anyio.create_task_group() as game_tg:
            game_tg.start_soon(PgnFeed.run, pgn_send_queue, url, filters)
            game_tg.start_soon(self._uci_worker, pgn_recv_queue, game)

    async def _uci_worker(
        self,
        pgn_recv_stream: MemoryObjectReceiveStream[chess.pgn.Game],
        game: db.Game,
    ):
        with pgn_recv_stream:
            try:
                pgn: chess.pgn.Game = await pgn_recv_stream.receive()
                while True:
                    last_pos: db.GamePosition = await self._update_game_db(pgn, game)
                    logger.info(f"Processing position {last_pos.fen}")
                    if self._current_position == last_pos:
                        continue
                    self._current_position = last_pos
                    async with anyio.create_task_group() as tg:
                        tg.start_soon(
                            self._uci_worker_think, get_leaf_board(pgn), last_pos
                        )
                        pgn = await pgn_recv_stream.receive()
                        tg.cancel_scope.cancel()
            except* anyio.EndOfStream:
                logger.debug("Game is finished.")
                await db.Game.filter(id=game.id).update(is_finished=True)
                self._game = None

    async def _uci_worker_think(self, board: chess.Board, pos: db.GamePosition):
        try:
            with await self._engine.analysis(
                board=board, multipv=self._config["max_multipv"]
            ) as analysis:
                thinking = await db.GamePositionThinking.create(
                    position=pos,
                    nodes=0,
                    q_score=0,
                    white_score=0,
                    draw_score=0,
                    black_score=0,
                )
                logger.debug(f"Starting thinking:\n{board}, {thinking.id}")
                await self.ws_notifier.set_thinking_update_id(thinking.id)
                await self.ws_notifier.notify_move_observers(
                    positions=[pos], thinkings=[thinking]
                )
                multipv = min(self._config["max_multipv"], board.legal_moves.count())
                info_bundle: list[chess.engine.InfoDict] = []
                async for info in analysis:
                    if "multipv" not in info:
                        logger.debug(f"Got info without multipv: {info}")
                        continue
                    if info["multipv"] != len(info_bundle) + 1:
                        logger.debug(f"Got info for wrong multipv: {info}")
                        info_bundle = []
                        continue
                    info_bundle.append(info)
                    if len(info_bundle) == multipv:
                        await self._process_info_bundle(
                            info_bundle, board, pos, thinking
                        )
                        info_bundle = []
        except AssertionError as e:
            logger.error(f"Assertion error: {e}")

    async def _process_info_bundle(
        self,
        info_bundle: list[chess.engine.InfoDict],
        board: chess.Board,
        pos: db.GamePosition,
        thinking: db.GamePositionThinking,
    ):
        total_n = sum(info.get("nodes", 0) for info in info_bundle)
        logger.debug(f"Total nodes: {total_n}")
        # logger.debug(info_bundle[0])
        evaluation: db.GamePositionEvaluation = await db.GamePositionEvaluation.create(
            thinking=thinking,
            nodes=total_n,
            time=int(info_bundle[0].get("time", 0) * 1000),
            depth=info_bundle[0].get("depth", 0),
            seldepth=info_bundle[0].get("seldepth", 0),
        )

        def make_eval_move(info: chess.engine.InfoDict):
            pv: List[chess.Move] = info.get("pv", [])
            assert len(pv) > 0
            move: chess.Move = pv[0]
            score: chess.engine.Score = info.get(
                "score", chess.engine.PovScore(chess.engine.Cp(0), chess.WHITE)
            ).white()
            wdl: chess.engine.Wdl = info.get(
                "wdl", chess.engine.PovWdl(chess.engine.Wdl(0, 1000, 0), chess.WHITE)
            ).white()
            return db.GamePositionEvaluationMove(
                evaluation=evaluation,
                nodes=info.get("nodes", 0),
                move_uci=move.uci(),
                move_opp_uci=pv[1].uci() if len(pv) > 1 else None,
                move_san=board.san(move),
                q_score=score.score(mate_score=20000),
                pv_san=make_pv_san_string(board, pv),
                mate_score=score.mate() if score.is_mate() else None,
                white_score=wdl.wins,
                draw_score=wdl.draws,
                black_score=wdl.losses,
                moves_left=info.get("movesleft", None),
            )

        moves: List[db.GamePositionEvaluationMove] = [
            make_eval_move(info) for info in info_bundle
        ]
        await db.GamePositionEvaluationMove.bulk_create(moves)
        thinking.nodes = total_n
        thinking.q_score = moves[0].q_score
        thinking.white_score = moves[0].white_score
        thinking.draw_score = moves[0].draw_score
        thinking.black_score = moves[0].black_score
        thinking.moves_left = moves[0].moves_left
        await thinking.save()
        await self.ws_notifier.notify_move_observers(
            positions=[pos], thinkings=[thinking]
        )
        await self.ws_notifier.notify_thinking_observers(
            thinkings=[evaluation], moves=[moves]
        )
