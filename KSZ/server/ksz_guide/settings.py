import os
from dataclasses import dataclass
from pathlib import Path
from string import Formatter

from ruamel.yaml import YAML


_DEFAULT_TEMPLATES_PATH = Path(__file__).with_name("guide-templates.yaml")
_YAML = YAML(typ="safe")


def _load_templates(path=None):
    with open(path or _DEFAULT_TEMPLATES_PATH, encoding="utf-8") as file:
        data = _YAML.load(file) or {}
    return {str(key).strip(): str(value).strip() for key, value in data.items()}


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
    announce_on_first_beacon: bool = True
    interrupt_on_beacon_change: bool = True
    min_announce_interval_ms: int = 15000
    same_beacon_cooldown_ms: int = 60000
    position_ttl_ms: int = 15000
    pending_ttl_ms: int = 15000
    user_quiet_ms: int = 15000
    location_templates: dict = None

    def __post_init__(self):
        if self.location_templates is None:
            object.__setattr__(self, "location_templates", _load_templates())

    def location_template(self, language):
        family = language.split("-", 1)[0].lower()
        return self.location_templates.get(language) or self.location_templates.get(family) or self.location_templates["zh-CN"]

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        templates = _load_templates()
        for key, value in list(templates.items()):
            env_name = f"KSZ_GUIDE_LOCATION_TEMPLATE_{key.upper().replace('-', '_')}"
            override = _location_template(env, env_name, value)
            if override != value:
                templates[key] = override
        defaults = cls(location_templates=templates)
        return cls(
            auto_announce_enabled=_boolean(env, "KSZ_GUIDE_AUTO_ANNOUNCE_ENABLED", defaults.auto_announce_enabled),
            announce_on_first_beacon=_boolean(env, "KSZ_GUIDE_ANNOUNCE_ON_FIRST_BEACON", defaults.announce_on_first_beacon),
            interrupt_on_beacon_change=_boolean(env, "KSZ_GUIDE_INTERRUPT_ON_BEACON_CHANGE", defaults.interrupt_on_beacon_change),
            min_announce_interval_ms=_milliseconds(env, "KSZ_GUIDE_MIN_ANNOUNCE_INTERVAL_MS", defaults.min_announce_interval_ms),
            same_beacon_cooldown_ms=_milliseconds(env, "KSZ_GUIDE_SAME_BEACON_COOLDOWN_MS", defaults.same_beacon_cooldown_ms),
            position_ttl_ms=_milliseconds(env, "KSZ_GUIDE_POSITION_TTL_MS", defaults.position_ttl_ms, 1),
            pending_ttl_ms=_milliseconds(env, "KSZ_GUIDE_PENDING_TTL_MS", defaults.pending_ttl_ms, 1),
            user_quiet_ms=_milliseconds(env, "KSZ_GUIDE_USER_QUIET_MS", defaults.user_quiet_ms),
            location_templates=templates,
        )
