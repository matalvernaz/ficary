"""Soundscape data model + JSON (de)serialization.

A soundscape is a set of looping ambient sounds, each with a volume and an
optional 3D position, plus a reverb room size and the master volume the
reader fades up to. One JSON file per soundscape in ``soundscapes_dir()``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

SCHEMA_VERSION = 1


def _clamp01(v) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, v))


@dataclass
class Sound:
    source: str            # filename under sounds_dir(), or "bundled:<name>"
    volume: float = 0.7    # 0..1
    positional: bool = False
    azimuth: float = 0.0   # degrees, 0 = front, 90 = right
    elevation: float = 0.0
    distance: float = 1.0


@dataclass
class Soundscape:
    name: str
    sounds: list[Sound] = field(default_factory=list)
    reverb_room_size: float = 0.0  # 0 = dry .. 1 = large hall
    master_volume: float = 0.8
    version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "_comment": "Ficary soundscape. Edit in the Soundscape editor or by hand.",
            "version": self.version,
            "name": self.name,
            "master_volume": self.master_volume,
            "reverb_room_size": self.reverb_room_size,
            "sounds": [asdict(s) for s in self.sounds],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Soundscape":
        if not isinstance(data, dict):
            raise ValueError("soundscape JSON must be an object")
        sounds: list[Sound] = []
        for raw in data.get("sounds", []) or []:
            if not isinstance(raw, dict) or not raw.get("source"):
                continue
            sounds.append(Sound(
                source=str(raw["source"]),
                volume=_clamp01(raw.get("volume", 0.7)),
                positional=bool(raw.get("positional", False)),
                azimuth=float(raw.get("azimuth", 0.0) or 0.0),
                elevation=float(raw.get("elevation", 0.0) or 0.0),
                distance=float(raw.get("distance", 1.0) or 1.0),
            ))
        return cls(
            name=str(data.get("name") or "Untitled"),
            sounds=sounds,
            reverb_room_size=_clamp01(data.get("reverb_room_size", 0.0)),
            master_volume=_clamp01(data.get("master_volume", 0.8)),
            version=int(data.get("version", SCHEMA_VERSION) or SCHEMA_VERSION),
        )
