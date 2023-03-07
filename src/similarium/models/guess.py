from __future__ import annotations

import random
from typing import TYPE_CHECKING, Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio.session import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import relationship, selectinload

from similarium.config import config
from similarium.db import Base
from similarium.logging import logger
from similarium.utils import CELEBRATE_EMOJIS, timestamp_ms

if TYPE_CHECKING:
    from similarium.models import Game


class Guess(Base):
    __tablename__ = "guess"

    id = sa.Column(sa.Integer, primary_key=True)
    game_id = sa.Column(sa.Integer, sa.ForeignKey("game.id"), nullable=False)
    game = relationship("Game", back_populates="guesses", lazy="joined")

    # Milliseconds since start of previous day UTC, only used for ordering
    # guesses in a game. Previous day is used to deal with time zones
    updated = sa.Column(sa.BigInteger, nullable=False)

    user_id = sa.Column(sa.Text, sa.ForeignKey("user.id"), nullable=False)
    user = relationship("User", lazy="joined", foreign_keys=[user_id])

    latest_guess_user_id = sa.Column(sa.Text, sa.ForeignKey("user.id"), nullable=False)
    latest_guess_user = relationship(
        "User", lazy="joined", foreign_keys=[latest_guess_user_id]
    )

    word = sa.Column(sa.Text, nullable=False)
    percentile = sa.Column(sa.Integer, nullable=False)
    similarity = sa.Column(sa.Float, nullable=False)
    idx = sa.Column(sa.Integer, nullable=False)

    @classmethod
    async def new(
        cls,
        *,
        game: Game,
        user_id: str,
        word: str,
        percentile: int,
        similarity: float,
        session: AsyncSession,
    ) -> Guess:
        logger.debug(
            f"Creating new Guess: {game=} {user_id=} "
            f"{word=} {percentile=} {similarity=}"
        )

        # XXX: Race condition??
        stmt = select(sa.func.count(word)).where(cls.game_id == game.id)
        result = await session.execute(stmt)
        count = result.scalars().one()

        return cls(
            game_id=game.id,
            updated=timestamp_ms(),
            user_id=user_id,
            latest_guess_user_id=user_id,
            word=word,
            percentile=percentile,
            similarity=similarity,
            idx=count + 1,
        )

    @classmethod
    async def get(
        cls, *, session: AsyncSession, word: str, game_id: int
    ) -> Optional[Guess]:
        logger.debug(f"Getting guess {word=} {game_id=}")
        stmt = (
            select(cls)
            .where(
                cls.word == word,
                cls.game_id == game_id,
            )
            .options(selectinload(cls.game))
        )

        result = await session.execute(stmt)

        return result.scalars().one_or_none()

    @property
    def is_secret(self) -> bool:
        return self.word == self.game.secret

    async def get_celebration(self, *, session: AsyncSession) -> Optional[str]:
        """Get a celebration for the guess

        Some guesses warrant celebrations! Celebrations are posted for the
        first guess that is in:

            * Top 1000
            * Top 100
            * Top 10

        If a single guess is the first word in all these categories, only the
        highest is celebrated
        """
        if not self.percentile:
            # Nothing worth celebrating
            return None
        celebrate_emoji = random.choice(CELEBRATE_EMOJIS)

        # Get highest guess that is not current guess
        stmt = (
            select(Guess)
            .where(Guess.game_id == self.game_id, Guess.word != self.word)
            .order_by(Guess.percentile.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        other = result.scalar()
        if other is None:
            # Current guess is highest, as it's the first! Let's celebrate the
            # first guess being green!
            return f"First guess by <@{self.user_id}> was immediately in the green! {celebrate_emoji}"
        if other.percentile > self.percentile:
            # We're not the new highest, so lets bail
            return None

        if other.percentile == 0:
            # It's the first green at all
            if self.percentile < 900:
                # We're not in the top 100 yet, just a basic celebration
                return f"<@{self.user_id}> just got the first green guess! {celebrate_emoji}"
            if self.percentile < 990:
                # Almost to top 10, but not so far!
                return f"<@{self.user_id}> had a great first green guess! {celebrate_emoji}"
            if self.percentile < 1000:
                # In top 10!
                return f"<@{self.user_id}> had a fantastic first green guess in the top 10! {celebrate_emoji}"
        elif other.percentile < 900:
            # Other guess was not in top 100
            if self.percentile < 900:
                # Neither are we!
                return None
            if self.percentile < 990:
                # We breached top 100, but not top 10
                return f"<@{self.user_id}> just got a little bit closer to the secret {celebrate_emoji}"
            if self.percentile < 1000:
                # Straight to top ten!
                return f"<@{self.user_id}> had an amazing guess that's not far from the secret! {celebrate_emoji}"
        elif other.percentile < 990:
            # Other guess was not in top 10
            if self.percentile < 990:
                # Neither was ours!
                return None
            if self.percentile < 1000:
                return f"Getting closer! A good guess by <@{self.user_id}>! {celebrate_emoji}"

        return None

    def __repr__(self) -> str:
        if self.percentile:
            percentile_repr = f"{self.percentile}/{config.rules.similarity_count}"
        else:
            percentile_repr = "cold"
        return f"<Guess {self.idx}. (id={self.id} {self.word}: {percentile_repr})>"
