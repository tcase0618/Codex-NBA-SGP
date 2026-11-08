"""Exact, timestamped user-entered FanDuel markets; never authenticated offers."""
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .schemas import Record, utc
from .sgp import Leg


class OfferedLeg(Leg):
    team_id: str = Field(min_length=1)
    decimal_odds: float = Field(gt=1, le=1000000)

    @field_validator("decimal_odds", mode="before")
    @classmethod
    def no_bool(cls, value):
        if isinstance(value, bool):
            raise ValueError("A boolean is not an offered price")
        return value


class MarketSnapshot(Record):
    game_id: str = Field(min_length=1)
    home_id: str = Field(min_length=1)
    away_id: str = Field(min_length=1)
    tipoff: datetime
    observed_at: datetime
    sportsbook: Literal["fanduel"] = "fanduel"
    source_reference: str = Field(min_length=5, max_length=1000)
    legs: list[OfferedLeg] = Field(min_length=1, max_length=200)
    _timestamps = field_validator("tipoff", "observed_at")(utc)

    @model_validator(mode="after")
    def fresh(self):
        now = datetime.now(timezone.utc)
        if not self.observed_at <= now < self.tipoff:
            raise ValueError("A market snapshot must be observed before now and before future tipoff")
        if (now - self.observed_at).total_seconds() > 300:
            raise ValueError("Offered line snapshot is older than five minutes")
        if self.home_id == self.away_id:
            raise ValueError("Offered markets require distinct teams")
        if not self.source_reference.strip():
            raise ValueError("An actual source reference is required")
        from .sgp import leg_key
        keys = [leg_key(Leg(**{k: getattr(p, k) for k in ("player_id", "stat", "side", "line")})) for p in self.legs]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate market selections")
        if any(p.team_id not in {self.home_id, self.away_id} for p in self.legs):
            raise ValueError("A market player is assigned to an unrelated team")
        return self

    def offered_rows(self):
        stamp = datetime.now(timezone.utc).isoformat()
        from .store import digest
        receipt_hash = digest(self.model_dump(mode="json"))
        return [{**p.model_dump(mode="json"), "home_id": self.home_id, "away_id": self.away_id,
                 "tipoff": self.tipoff.isoformat(), "game_id": self.game_id,
                 "sportsbook": self.sportsbook, "observed_at": self.observed_at.isoformat(),
                 "retrieved_at": stamp, "source_reference": self.source_reference,
                 "source_sha256": receipt_hash,
                 "source": {"provider": "USER_ENTERED_FANDUEL_SNAPSHOT", "reference": self.source_reference,
                    "observed_at": self.observed_at.isoformat(), "retrieved_at": stamp},
                 "authenticity": "CALLER_ASSERTION_UNVERIFIED"} for p in self.legs]
