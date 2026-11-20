def request_venue_code(extra_body):
    values = extra_body or {}
    context = values.get("ksz_prompt_context") or {}
    state = context.get("state") or {}
    venue = state.get("venue_context", values.get("venue_context"))
    if venue is None:
        return None
    code = venue.get("code") if isinstance(venue, dict) else None
    if not isinstance(code, str) or not code.strip():
        raise ValueError("绑定场馆缺少有效的 venue_context.code")
    return code.strip()
