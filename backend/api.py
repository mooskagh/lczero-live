from sanic import Blueprint
import dataclasses
from dataclasses import asdict
from sanic.response import json
from typing import List, Optional
import db

api = Blueprint("api", url_prefix="/api")


@dataclasses.dataclass
class GameData:
    id: int
    name: str
    isFinished: bool
    isBeingAnalyzed: bool


@dataclasses.dataclass
class GamesResponse:
    games: list[GameData]


@api.get("/games")
async def games(request):
    games = await db.Game.all()
    analyzed_games = set(g.id for g in request.app.ctx.app.get_games_being_analyzed())
    games = await db.Game.filter(
        is_hidden=False, tournament__is_hidden=False
    ).prefetch_related("tournament")
    return json(
        asdict(
            GamesResponse(
                [
                    GameData(
                        id=game.id,
                        name=f"{game.game_name} ({game.round_name}) --- "
                        f"{game.tournament.name}",
                        isFinished=game.is_finished,
                        isBeingAnalyzed=game.id in analyzed_games,
                    )
                    for game in games
                ]
            )
        )
    )


@dataclasses.dataclass
class GamePositionResponse:
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


@dataclasses.dataclass
class PlayerResponse:
    name: str
    rating: int
    fideId: Optional[int]
    fed: Optional[str]


@dataclasses.dataclass
class GameResponse:
    gameId: int
    player1: PlayerResponse
    player2: PlayerResponse
    feedUrl: str
    positions: list[GamePositionResponse]


@api.get("/game/<game_id:int>")
async def game(request, game_id):
    game = await db.Game.get(id=game_id)
    positions: List[db.GamePosition] = (
        await db.GamePosition.filter(game=game).order_by("ply_number").all()
    )
    positions = (
        await db.GamePosition.filter(game=game)
        .order_by("ply_number")
        .prefetch_related("thinkings")
        .all()
    )

    positions_with_thinking = []
    for pos in positions:
        best_thinking = max(pos.thinkings, key=lambda t: t.nodes, default=None)
        positions_with_thinking.append((pos, best_thinking))

    return json(
        asdict(
            GameResponse(
                gameId=game.id,
                player1=PlayerResponse(
                    name=game.player1_name,
                    rating=game.player1_rating,
                    fideId=game.player1_fide_id,
                    fed=game.player1_fed,
                ),
                player2=PlayerResponse(
                    name=game.player2_name,
                    rating=game.player2_rating,
                    fideId=game.player2_fide_id,
                    fed=game.player2_fed,
                ),
                feedUrl="https://lichess.org/broadcast/-/-/"
                f"{game.lichess_round_id}/{game.lichess_id}",
                positions=[
                    GamePositionResponse(
                        ply=pos.ply_number,
                        thinkingId=None,
                        moveUci=pos.move_uci,
                        moveSan=pos.move_san,
                        fen=pos.fen,
                        whiteClock=pos.white_clock,
                        blackClock=pos.black_clock,
                        scoreQ=None,
                        scoreW=None,
                        scoreD=None,
                        scoreB=None,
                    )
                    for pos in positions
                ],
            )
        )
    )
