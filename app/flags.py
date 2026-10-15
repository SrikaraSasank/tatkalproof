"""Runtime feature flags stored in Redis, so the dashboard can flip rate limiting
or the waiting room on and off across every API worker without a restart."""

from dataclasses import asdict, dataclass

from .config import get_settings

FLAGS_KEY = "flags"


@dataclass
class Flags:
    rate_limit_enabled: bool
    rate_limit_per_second: int
    waiting_room_enabled: bool
    waiting_room_admit_per_second: int


def _defaults() -> Flags:
    s = get_settings()
    return Flags(
        rate_limit_enabled=s.rate_limit_enabled,
        rate_limit_per_second=s.rate_limit_per_second,
        waiting_room_enabled=s.waiting_room_enabled,
        waiting_room_admit_per_second=s.waiting_room_admit_per_second,
    )


def _parse(raw: dict[str, str], base: Flags) -> Flags:
    data = asdict(base)
    for name, value in raw.items():
        if name not in data:
            continue
        data[name] = value == "1" if isinstance(data[name], bool) else int(value)
    return Flags(**data)


async def get_flags(r) -> Flags:
    return _parse(await r.hgetall(FLAGS_KEY), _defaults())


async def update_flags(r, changes: dict) -> Flags:
    clean = {k: ("1" if v else "0") if isinstance(v, bool) else str(v) for k, v in changes.items() if v is not None}
    if clean:
        await r.hset(FLAGS_KEY, mapping=clean)
    return await get_flags(r)


async def reset_flags(r) -> Flags:
    await r.delete(FLAGS_KEY)
    return _defaults()
