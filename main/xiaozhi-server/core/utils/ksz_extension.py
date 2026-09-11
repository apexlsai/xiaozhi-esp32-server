import os

try:
    from ksz_guide import integration as guide
except ModuleNotFoundError as error:
    if error.name != "ksz_guide":
        raise
    if os.environ.get("KSZ_GUIDE_ENABLED", "0").lower() in {"1", "true", "yes"}:
        raise RuntimeError("KSZ_GUIDE_ENABLED requires the ksz_guide package") from error
    guide = None

track_chat = guide.track_chat if guide else lambda function: function
