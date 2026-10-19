import os

try:
    from ksz_guide import integration as guide
except ModuleNotFoundError as error:
    if error.name != "ksz_guide":
        raise
    if any(os.environ.get(name, "0").lower() in {"1", "true", "yes"}
           for name in ("KSZ_GUIDE_ENABLED", "KSZ_GUIDE_PRESENCE_ENABLED")):
        raise RuntimeError("KSZ guide or presence requires the ksz_guide package") from error
    guide = None

track_chat = guide.track_chat if guide else lambda function: function
