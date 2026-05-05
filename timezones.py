"""Utilitaires fuseau horaire : on utilise zoneinfo (gère automatiquement DST)."""
import datetime as dt

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

from settings import LOCAL_TZ

TZ = ZoneInfo(LOCAL_TZ)
UTC = ZoneInfo("UTC")


def now_local() -> dt.datetime:
    return dt.datetime.now(tz=TZ)


def now_utc() -> dt.datetime:
    return dt.datetime.now(tz=UTC)


def to_utc(local_dt: dt.datetime) -> dt.datetime:
    """Convertit un datetime local (Europe/Brussels) vers UTC."""
    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=TZ)
    return local_dt.astimezone(UTC)


def from_utc_str(value: str) -> dt.datetime:
    """Reparse une string UTC stockée en base."""
    return dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


DEFAULT_SLOTS = [9, 12, 18, 20]


def next_slot_for_hour(hour_local: int) -> dt.datetime:
    """Prochain datetime local à hour_local:00 (aujourd'hui si futur, sinon demain)."""
    now = now_local()
    target = now.replace(hour=hour_local, minute=0, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return target
