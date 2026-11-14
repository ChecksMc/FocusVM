"""Built-in focus profiles shared by settings and both frontends."""

from dataclasses import dataclass
from typing import Literal

type ProfileId = Literal["deep-work", "contest", "research"]


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    network: str


PROFILES: dict[ProfileId, Profile] = {
    "deep-work": Profile("Deep Work", "balanced"),
    "contest": Profile("Contest", "strict"),
    "research": Profile("Research", "balanced"),
}
