from datetime import UTC, datetime
from pathlib import Path

from trader.config import AppConfig, StrategyConfig
from trader.errors import SafetyError


def cycle_slot(
    now: datetime, schedule: StrategyConfig | None = None, strategy: str = "default"
) -> str:
    schedule = schedule or StrategyConfig()
    period = schedule.cadence_minutes * 60
    boundary = datetime.fromtimestamp(int(now.timestamp()) // period * period, UTC).isoformat()
    # Retain the historical key for the default baseline, including duplicate-cycle protection.
    return boundary if strategy == "default" else f"{strategy}:{boundary}"


def check_schedule(now: datetime, schedule: StrategyConfig | None = None) -> None:
    schedule = schedule or StrategyConfig()
    age = now.timestamp() % (schedule.cadence_minutes * 60) - schedule.offset_minutes * 60
    if not 0 <= age <= 900:
        raise SafetyError("OUTSIDE_SCHEDULE_WINDOW")


def render_timers(cfg: AppConfig, directory: Path) -> list[str]:
    """Generate reviewable units; installation/enabling remains an explicit operator action."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name in cfg.strategy_names:
        schedule = cfg.strategies[name]
        times = []
        for minute in range(schedule.offset_minutes, 1440, schedule.cadence_minutes):
            times.append(f"OnCalendar=*-*-* {minute // 60:02}:{minute % 60:02}:00 UTC")
        unit = f"crypto-trader@{name}.timer"
        text = (
            f"[Unit]\nDescription=Trading decisions for {name}\n\n[Timer]\n"
            + "\n".join(times)
            + f"\nAccuracySec=30s\nRandomizedDelaySec=0\nPersistent=false\n"
            f"Unit=crypto-trader@{name}.service\n\n[Install]\nWantedBy=timers.target\n"
        )
        (directory / unit).write_text(text, encoding="utf-8")
        written.append(str(directory / unit))
    return written
