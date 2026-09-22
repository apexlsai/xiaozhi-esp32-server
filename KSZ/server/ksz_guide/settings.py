import os
from dataclasses import dataclass
from string import Formatter


def _boolean(env, name, default):
    value = env.get(name, str(int(default))).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean (0 or 1)")


def _milliseconds(env, name, default, minimum=0):
    try:
        value = int(env.get(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be an integer number of milliseconds") from None
    if not minimum <= value <= 86400000:
        raise ValueError(f"{name} must be between {minimum} and 86400000 milliseconds")
    return value


def _location_template(env, name, default):
    value = (env.get(name) or default).strip() or default
    try:
        fields = [(field, spec, conversion) for _, field, spec, conversion in Formatter().parse(value)
                  if field is not None]
    except ValueError:
        fields = []
    if fields != [("location", "", None)]:
        raise ValueError(f"{name} must contain exactly one {{location}} placeholder without formatting")
    return value


@dataclass(frozen=True)
class GuideSettings:
    auto_announce_enabled: bool = True
    announce_on_first_beacon: bool = False
    interrupt_on_beacon_change: bool = True
    min_announce_interval_ms: int = 15000
    position_ttl_ms: int = 15000
    pending_ttl_ms: int = 15000
    user_quiet_ms: int = 15000
    location_template_zh_cn: str = "您现在位于{location}。"
    location_template_en: str = "You are now at {location}."
    location_template_ja: str = "現在、{location}にいます。"
    location_template_ko: str = "현재 {location}에 계십니다."

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        defaults = cls()
        return cls(
            auto_announce_enabled=_boolean(env, "KSZ_GUIDE_AUTO_ANNOUNCE_ENABLED", defaults.auto_announce_enabled),
            announce_on_first_beacon=_boolean(env, "KSZ_GUIDE_ANNOUNCE_ON_FIRST_BEACON", defaults.announce_on_first_beacon),
            interrupt_on_beacon_change=_boolean(env, "KSZ_GUIDE_INTERRUPT_ON_BEACON_CHANGE", defaults.interrupt_on_beacon_change),
            min_announce_interval_ms=_milliseconds(env, "KSZ_GUIDE_MIN_ANNOUNCE_INTERVAL_MS", defaults.min_announce_interval_ms),
            position_ttl_ms=_milliseconds(env, "KSZ_GUIDE_POSITION_TTL_MS", defaults.position_ttl_ms, 1),
            pending_ttl_ms=_milliseconds(env, "KSZ_GUIDE_PENDING_TTL_MS", defaults.pending_ttl_ms, 1),
            user_quiet_ms=_milliseconds(env, "KSZ_GUIDE_USER_QUIET_MS", defaults.user_quiet_ms),
            location_template_zh_cn=_location_template(env, "KSZ_GUIDE_LOCATION_TEMPLATE_ZH_CN", defaults.location_template_zh_cn),
            location_template_en=_location_template(env, "KSZ_GUIDE_LOCATION_TEMPLATE_EN", defaults.location_template_en),
            location_template_ja=_location_template(env, "KSZ_GUIDE_LOCATION_TEMPLATE_JA", defaults.location_template_ja),
            location_template_ko=_location_template(env, "KSZ_GUIDE_LOCATION_TEMPLATE_KO", defaults.location_template_ko),
        )
