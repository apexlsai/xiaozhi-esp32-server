import httpx

from .models import GuideError, mac_address


async def fetch_device_metadata(config, macs):
    base = config.get("url", "").rstrip("/")
    secret = config.get("secret")
    if not base or not secret:
        raise GuideError("metadata_api_unconfigured")
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(2, connect=1)) as client:
            response = await client.post(
                base + "/config/device-metadata",
                json={"deviceIds": macs},
                headers={"Authorization": "Bearer " + secret},
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or payload.get("code") != 0 or not isinstance(payload.get("data"), list):
            raise GuideError("invalid_metadata_response")
        result = {}
        requested = set(macs)
        for item in payload["data"]:
            if not isinstance(item, dict):
                raise GuideError("invalid_device_metadata")
            mac = mac_address(item.get("device_mac"))
            if mac not in requested or mac in result:
                raise GuideError("metadata_identity_mismatch")
            metadata = {}
            for field in ("model", "firmware_version"):
                value = item.get(field)
                if value is not None:
                    if not isinstance(value, str) or len(value) > 100:
                        raise GuideError("invalid_device_metadata")
                    if value.strip():
                        metadata[field] = value.strip()
            result[mac] = metadata
        return result
    except (httpx.HTTPError, ValueError, TypeError) as error:
        raise GuideError("metadata_unavailable") from error
