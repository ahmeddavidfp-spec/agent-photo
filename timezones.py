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
