from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise ValueError("Timezone-aware timestamps are required")
    return result.astimezone(timezone.utc)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Player(Record):
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=100)
    position: Literal["PG", "SG", "SF", "PF", "C", "G", "F", "UNKNOWN"]
    archetypes: list[str] = Field(default_factory=list)


class Team(Record):
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=100)


class Rotation(Record):
    player_id: str
    team_id: str
    known_at: datetime
    expected_minutes: float = Field(ge=0, le=48)
    minutes_cap: float = Field(default=48, ge=0, le=48)
    active_probability: float = Field(default=1, ge=0, le=1)
    starter: bool = False
    status: str = "available"

    _timestamp = field_validator("known_at")(utc)


class Box(Record):
    player_id: str
    team_id: str
    minutes: float = Field(ge=0, le=100)
    pts: int = Field(ge=0)
    fga: int = Field(ge=0)
    fgm: int = Field(ge=0)
    fg3a: int = Field(ge=0)
    fg3m: int = Field(ge=0)
    fta: int = Field(ge=0)
    ftm: int = Field(ge=0)
    oreb: int = Field(ge=0)
    dreb: int = Field(ge=0)
    ast: int = Field(ge=0)
    stl: int = Field(ge=0)
    blk: int = Field(ge=0)
    tov: int = Field(ge=0)
    pf: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def reconcile(self):
        if not (self.fg3m <= self.fg3a <= self.fga and self.fg3m <= self.fgm <= self.fga):
            raise ValueError("Invalid field goal counts")
        if self.fgm - self.fg3m > self.fga - self.fg3a or self.ftm > self.fta:
            raise ValueError("Makes exceed attempts")
        if self.pts != 2 * self.fgm + self.fg3m + self.ftm:
            raise ValueError("Points do not reconcile to field goals and free throws")
        return self


class Game(Record):
    id: str
    home_id: str
    away_id: str
    tipoff: datetime
    completed_at: datetime
    known_at: datetime
    home_score: int = Field(ge=0)
    away_score: int = Field(ge=0)
    overtime_periods: int = Field(default=0, ge=0, le=20)
    minutes_precision_seconds: Literal[0, 60] = 0
    boxes: list[Box] = Field(min_length=10, max_length=40)
    _timestamps = field_validator("tipoff", "completed_at", "known_at")(utc)

    @model_validator(mode="after")
    def reconcile(self):
        if self.home_id == self.away_id or self.home_score == self.away_score:
            raise ValueError("Final game requires distinct teams and a winner")
        if not self.tipoff < self.completed_at <= self.known_at:
            raise ValueError("Require tipoff < completion <= known_at")
        if len({b.player_id for b in self.boxes}) != len(self.boxes):
            raise ValueError("Duplicate player box score")
        if {b.team_id for b in self.boxes} != {self.home_id, self.away_id}:
            raise ValueError("Box score has wrong teams")
        for team_id, score in [(self.home_id, self.home_score), (self.away_id, self.away_score)]:
            boxes = [b for b in self.boxes if b.team_id == team_id]
            opponents = [b for b in self.boxes if b.team_id != team_id]
            if len([b for b in boxes if b.minutes > 0]) < 5:
                raise ValueError("Each team requires at least five participants")
            if any(b.minutes > 48 + 5 * self.overtime_periods + 0.01 for b in boxes):
                raise ValueError("Individual minutes exceed game duration")
            if sum(b.pts for b in boxes) != score:
                raise ValueError("Player points must sum to team score")
            if self.minutes_precision_seconds and any(not b.minutes.is_integer() for b in boxes):
                raise ValueError("Minute-resolution evidence requires integer source minutes")
            # Preserve displayed integer minutes. The source's rounding/truncation
            # convention is unknown, so permit one resolution unit per participant.
            tolerance = max(0.15, sum(b.minutes > 0 for b in boxes) * self.minutes_precision_seconds / 60)
            if abs(sum(b.minutes for b in boxes) - (240 + 25 * self.overtime_periods)) > tolerance:
                raise ValueError("Team minutes must reconcile to regulation plus overtime")
            if sum(b.ast for b in boxes) > sum(b.fgm for b in boxes):
                raise ValueError("Assists exceed made field goals")
            if any(b.ast > sum(other.fgm for other in boxes if other.player_id != b.player_id) for b in boxes):
                raise ValueError("A player's assists exceed teammate made field goals")
            if sum(b.stl for b in boxes) > sum(b.tov for b in opponents):
                raise ValueError("Steals exceed opponent turnovers")
            if sum(b.blk for b in boxes) > sum(b.fga - b.fgm for b in opponents):
                raise ValueError("Blocks exceed opponent missed field goals")
        return self


class Dataset(Record):
    source: str = Field(min_length=1, max_length=200)
    source_url: str = Field(min_length=1, max_length=500)
    retrieved_at: datetime
    synthetic: bool = False
    teams: list[Team] = Field(min_length=2, max_length=100)
    players: list[Player] = Field(min_length=10, max_length=2000)
    rotations: list[Rotation] = Field(default_factory=list, max_length=10000)
    games: list[Game] = Field(default_factory=list, max_length=5000)
    _timestamp = field_validator("retrieved_at")(utc)

    @model_validator(mode="after")
    def references(self):
        teams, players = {t.id for t in self.teams}, {p.id for p in self.players}
        if len(teams) != len(self.teams) or len(players) != len(self.players):
            raise ValueError("Duplicate team/player IDs")
        if len({g.id for g in self.games}) != len(self.games):
            raise ValueError("Duplicate game IDs")
        if len({(r.player_id, r.known_at) for r in self.rotations}) != len(self.rotations):
            raise ValueError("Duplicate rotation snapshot")
        for r in self.rotations:
            if r.team_id not in teams or r.player_id not in players:
                raise ValueError("Unknown rotation entity")
        for g in self.games:
            if not {g.home_id, g.away_id} <= teams or any(b.player_id not in players for b in g.boxes):
                raise ValueError("Unknown game entity")
        if any(g.known_at > self.retrieved_at for g in self.games) or any(r.known_at > self.retrieved_at for r in self.rotations):
            raise ValueError("Record known_at cannot follow retrieval")
        if self.retrieved_at > datetime.now(timezone.utc):
            raise ValueError("Retrieval timestamp cannot be in the future")
        return self


class Scenario(Record):
    player_id: str
    active_probability: float | None = Field(default=None, ge=0, le=1)
    minutes_cap: float | None = Field(default=None, ge=0, le=48)


class ForecastRequest(Record):
    dataset_id: str
    home_id: str
    away_id: str
    tipoff: datetime
    cutoff: datetime
    simulations: int = Field(default=250, ge=20, le=5000)
    seed: int = Field(default=42, ge=0, le=2**32 - 1)
    scenarios: list[Scenario] = Field(default_factory=list, max_length=40)
    _timestamps = field_validator("tipoff", "cutoff")(utc)

    @model_validator(mode="after")
    def pregame(self):
        if self.home_id == self.away_id:
            raise ValueError("Choose different teams")
        if self.cutoff >= self.tipoff or self.cutoff > datetime.now(timezone.utc):
            raise ValueError("Cutoff must precede tipoff and cannot be in the future")
        if len({s.player_id for s in self.scenarios}) != len(self.scenarios):
            raise ValueError("Duplicate scenario player")
        return self
