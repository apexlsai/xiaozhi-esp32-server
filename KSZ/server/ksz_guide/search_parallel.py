import json
import re
import uuid

WEB_SEARCH = "web_search"
YUANBAO_SEARCH = "yuanbao_search"
REAL_SEARCH = re.compile(
    r"联网|上网|搜索|检索|核实|最新|近期|实时|近况|进展|"
    r"搜一下|搜一搜|搜下|查一下|查一查|查下|元宝|"
    r"最近的|(?:最近).*(?:会议|新闻|政策|进展|活动)|"
    r"这两天|这两日|近两天|发生了什么|有什么大事|"
    r"(?:会议|大会|峰会|论坛|APEC).*(?:何时|什么时候|在哪|哪儿|哪里|举办|主场|开幕)|"
    r"(?:何时|什么时候|在哪|哪儿|哪里).*(?:开|举行|成立|举办|开幕)|"
    r"附近.+(?:好吃|美食|餐厅|饭店)|好吃的|美食推荐|"
    r"(?:有什么|有哪些).*(?:名人|博物馆)|"
    r"公司是(?:谁|做什么|干什么)|什么公司|哪家公司|"
    r"web.?search|search.*(?:web|online)|look.*up|latest|recent.*(?:news|updates)|"
    r"current.*(?:news|policy|price)|検索|調べ|최신|검색",
    re.IGNORECASE,
)
_SKIP_WITH = {"direct_answer", "self_camera_take_photo"}


def _arguments(calls, query):
    for item in calls or []:
        if item.get("name") in {WEB_SEARCH, YUANBAO_SEARCH}:
            arguments = item.get("arguments") or "{}"
            try:
                json.loads(arguments)
                return arguments
            except (TypeError, json.JSONDecodeError):
                break
    query = (query or "").strip()
    return json.dumps({"query": query}, ensure_ascii=False) if query else "{}"


def _append(calls, names, name, arguments):
    if name in names:
        return
    calls.append({"id": str(uuid.uuid4()), "name": name, "arguments": arguments})
    names.add(name)


def attach_web_search(conn, tool_calls_list, query="", depth=0):
    calls = list(tool_calls_list or [])
    names = {item.get("name") for item in calls}
    search_called = bool(names & {WEB_SEARCH, YUANBAO_SEARCH})
    real = bool(REAL_SEARCH.search(query or ""))
    if names & _SKIP_WITH and not search_called:
        return calls
    if not search_called and not (depth == 0 and real):
        return calls
    handler = getattr(conn, "func_handler", None)
    has_tool = getattr(handler, "has_tool", None)
    if not callable(has_tool):
        return calls
    arguments = _arguments(calls, query)
    if has_tool(WEB_SEARCH):
        _append(calls, names, WEB_SEARCH, arguments)
    if has_tool(YUANBAO_SEARCH) and (YUANBAO_SEARCH in names or real):
        _append(calls, names, YUANBAO_SEARCH, arguments)
    return calls
