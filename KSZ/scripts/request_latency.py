# /// script
# requires-python = ">=3.10"
# dependencies = ["paho-mqtt>=2.1,<3", "cryptography>=44,<49"]
# ///

import argparse
import datetime as dt
import io
import json
import re
import subprocess
import sys
from pathlib import Path


DEFAULT_LOG_DIR = Path("KSZ/logs")


def timestamp(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def normalize(text):
    return re.sub(r"[\s，。？！,.?!]", "", text)


def read_lines(text):
    result = []
    for line in text.splitlines():
        try:
            result.append((timestamp(line.split(" ", 1)[0]), line))
        except ValueError:
            continue
    return sorted(result, key=lambda row: row[0])


def session_tag(line):
    match = re.search(r"\[([^\]]+)\]\[core\.", line)
    return match.group(1) if match else None


def analyze(server, mqtt, agent, query, since=None, device=None):
    server_lines = read_lines(server)
    candidates = [(t, line) for t, line in server_lines
                  if "大模型收到用户消息: " in line
                  and normalize(line.split("大模型收到用户消息: ", 1)[1]) == normalize(query)
                  and (since is None or t >= timestamp(since))]
    if not candidates:
        raise ValueError("未找到请求；检查问题文字、--since 和服务端日志时间戳")
    start, selected = candidates[-1]
    actual_query = selected.split("大模型收到用户消息: ", 1)[1]
    tag = session_tag(selected)
    end = next((t for t, line in server_lines if t > start
                and "大模型收到用户消息: " in line and session_tag(line) == tag), start + 120)
    window = [(t, line) for t, line in server_lines if start <= t < end]
    warnings = ["内部日志用服务端时间；客户端指标用 monotonic，不跨机器相减。",
                "模型全程与音频生成/发送可重叠，不能把所有耗时直接相加。",
                "历史的发送第一段语音埋点可能携带空音频，只代表首句/字幕调度，不证明首音频已发送。"]
    sentence_at = next((t for t, line in window
                        if "发送第一段语音: " in line and session_tag(line) == tag), None)
    concurrent = any("大模型收到用户消息: " in line and session_tag(line) != tag
                     for _, line in window)
    if concurrent:
        warnings.append("检测到并发会话：全局 ASR/工具日志无法可靠归属，已跳过这些指标。")
    stages = {}
    if sentence_at is not None:
        stages["chat_to_first_sentence_ms"] = round((sentence_at - start) * 1000, 2)
    recognized = [(t, line) for t, line in server_lines
                  if start - 5 <= t <= start and "识别文本: " in line
                  and normalize(line.split("识别文本: ", 1)[1]) == normalize(actual_query)]
    if recognized and not concurrent:
        stt, _ = recognized[-1]
        inputs = [(t, line) for t, line in server_lines
                  if stt - 15 <= t <= stt and "ASR输入音频:" in line]
        if inputs:
            asr_start, asr_line = inputs[-1]
            stages["asr_ms"] = round((stt - asr_start) * 1000, 2)
            duration = re.search(r"时长=([\d.]+)秒", asr_line)
            if duration:
                stages["input_audio_ms"] = float(duration.group(1)) * 1000
            if sentence_at is not None:
                stages["asr_input_to_first_sentence_ms"] = round((sentence_at - asr_start) * 1000, 2)
            udp = [(t, line) for t, line in read_lines(mqtt)
                   if asr_start - 3 <= t <= asr_start and "上行音频统计:" in line
                   and (device is None or device.lower().replace(":", "_") in line.lower())]
            if udp:
                udp_at, udp_line = udp[-1]
                counts = re.search(r"收到=(\d+) 序列跨度=(\d+) 缺失=(\d+)", udp_line)
                if counts:
                    stages["uplink_packets"] = int(counts.group(1))
                    stages["uplink_missing_packets"] = int(counts.group(3))
                    stages["gateway_forward_to_asr_input_ms"] = round((asr_start - udp_at) * 1000, 2)
        warnings.append("ASR/网关按相邻埋点关联；无全链路 request_id，繁忙环境请用专用测试设备并隔离请求。")
    records = {}
    for logged_at, line in read_lines(agent):
        offset = line.find('{"ts":')
        if offset < 0:
            continue
        try:
            item = json.loads(line[offset:])
        except json.JSONDecodeError:
            continue
        duration = item.get("duration_ms")
        if duration is None or item.get("path") != "/v1/chat/completions":
            continue
        finished = timestamp(item.get("ts")) if item.get("ts") else logged_at
        begun = finished - duration / 1000
        if begun < start - 0.25 or finished >= end:
            continue
        if normalize(item.get("user_preview", "")) != normalize(actual_query):
            continue
        extra = item.get("extra") or {}
        records[item["request_id"]] = {
            "request_id": item["request_id"], "route": item.get("route"),
            "queue_ms": item.get("queue_ms"), "router_ms": item.get("router_ms"),
            "rag_ms": item.get("rag_ms"), "llm_ttft_ms": extra.get("llm_ttft_ms"),
            "llm_ms": extra.get("llm_ms"), "total_ms": duration,
            "tool_calls": extra.get("tool_calls"), "status": item.get("status"),
            "started_at": begun, "finished_at": finished,
        }
    rounds = sorted(records.values(), key=lambda item: item["started_at"])
    if sentence_at is not None and rounds:
        final = rounds[-1]
        if final["llm_ms"] is not None and final["llm_ttft_ms"] is not None:
            first_chunk = final["finished_at"] - (final["llm_ms"] - final["llm_ttft_ms"]) / 1000
            if first_chunk <= sentence_at:
                stages["last_llm_first_chunk_to_sentence_ms_estimate"] = round((sentence_at - first_chunk) * 1000, 2)
                warnings.append("模型首包→首句埋点为估算，包含分句、TTS 建连/处理与调度；不是纯 TTS 或音频首包耗时。")
    tools = []
    if not concurrent:
        for t, line in window:
            match = re.search(r"执行工具: ([^，,\s]+)", line)
            if not match:
                continue
            next_llm = next((at for at, entry in window
                             if at > t and "发送给LLM的请求:" in entry), None)
            tools.append({"name": match.group(1),
                          "execution_and_resume_ms_estimate": round((next_llm - t) * 1000, 2)
                          if next_llm is not None else None})
    if tools:
        warnings.append("工具耗时按执行开始→下一轮 LLM 请求估算，含结果处理；缺埋点时显示未知。")
    if not rounds:
        warnings.append("无匹配 Agent 完成记录，模型/RAG 内部耗时未知。user_preview 截断或旧日志格式可能无法关联。")
    return {"query": actual_query, "server_started_at": dt.datetime.fromtimestamp(start, dt.timezone.utc).isoformat(),
            "stages": stages, "agent_rounds": rounds, "tools": tools, "warnings": warnings}


LABELS = {
    "input_audio_ms": "输入音频时长", "asr_ms": "ASR", "chat_to_first_sentence_ms": "识别后→首句发送埋点",
    "asr_input_to_first_sentence_ms": "ASR 输入→首句发送埋点",
    "gateway_forward_to_asr_input_ms": "网关结束转发→ASR 输入",
    "last_llm_first_chunk_to_sentence_ms_estimate": "末轮模型首包→首句发送埋点（估算）",
    "ota_ms": "OTA", "mqtt_connect_ms": "MQTT 建连/鉴权", "mqtt_subscribe_ms": "MQTT 订阅",
    "hello_ms": "hello 握手", "mcp_discovery_ms": "MCP 初始化/发现",
    "submit_to_stt_ms": "提交→STT", "submit_to_sentence_ms": "提交→首字幕",
    "submit_to_audio_ms": "提交→首 UDP 音频", "submit_to_stop_ms": "提交→TTS stop",
    "audio_span_ms": "首包→末包", "sentence_to_audio_ms": "首字幕→首 UDP 音频",
}


def render_report(report):
    output = io.StringIO()
    print("\n问题：" + report["query"], file=output)
    for group in ("client", "stages"):
        for key, value in report.get(group, {}).items():
            label = LABELS.get(key, key)
            print(f"  {label}: {value:.2f} ms" if key.endswith(("_ms", "_estimate")) and isinstance(value, (int, float))
                  else f"  {label}: {value}", file=output)
    for i, row in enumerate(report.get("agent_rounds", []), 1):
        print(f"  Agent 第 {i} 轮 [{row['request_id']}] route={row['route']} status={row['status']}", file=output)
        for key in ("queue_ms", "router_ms", "rag_ms", "llm_ttft_ms", "llm_ms", "total_ms"):
            value = row.get(key)
            print(f"    {key}: {value:.2f} ms" if value is not None else f"    {key}: 未知", file=output)
    for tool in report.get("tools", []):
        value = tool["execution_and_resume_ms_estimate"]
        print(f"  工具 {tool['name']} 执行/衔接（估算）: {value:.2f} ms" if value is not None
              else f"  工具 {tool['name']}: 耗时未知", file=output)
    for warning in report.get("warnings", []):
        print("  提示：" + warning, file=output)
    return output.getvalue()


def display(report):
    print(render_report(report), end="")


def log_slug(value):
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", value or "run").strip("-")
    return (slug or "run")[:48]


def write_run_log(report, mode, log_dir, exit_code):
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    saved_at = dt.datetime.now().astimezone()
    filename = f"{saved_at.strftime('%Y%m%dT%H%M%S%f%z')}_{mode}_{log_slug(report.get('query'))}.log"
    path = directory / filename
    report["log_file"] = str(path)
    content = [
        "KSZ request latency diagnostic",
        f"saved_at: {saved_at.isoformat()}",
        f"mode: {mode}",
        f"exit_code: {exit_code}",
        "",
        *[f"  +{row['elapsed_ms']:9.2f} ms {row['event']}" +
          (f" {row['detail']}" if row.get('detail') is not None else "")
          for row in report.get("events", [])],
        render_report(report).rstrip(),
        "",
    ]
    path.write_text("\n".join(content), encoding="utf-8")
    return path


def docker_logs(container, since):
    result = subprocess.run(["docker", "logs", "--timestamps", "--since", since, container],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(f"读取 {container} 日志失败；请检查 Docker 权限与容器名")
    return result.stdout + result.stderr


def main():
    parser = argparse.ArgumentParser(description="KSZ 单次请求延时诊断（日志分析/原生 MQTT+UDP）")
    modes = parser.add_subparsers(dest="mode", required=True)
    logs = modes.add_parser("logs", help="分析已有请求，不发送新请求，无额外依赖")
    logs.add_argument("--query", required=True)
    logs.add_argument("--server-log", type=Path)
    logs.add_argument("--agent-log", type=Path)
    logs.add_argument("--mqtt-log", type=Path)
    logs.add_argument("--since", default=None, help="ISO 时间；默认读取最近 30 分钟并选择最后一次匹配")
    logs.add_argument("--device")
    request = modes.add_parser("request", help="发送一次真实请求并测量收到的控制/音频")
    request.add_argument("--ota", required=True)
    request.add_argument("--device", required=True, help="已绑定的专用测试 MAC；会占用该设备 MQTT 会话")
    source = request.add_mutually_exclusive_group()
    source.add_argument("--text")
    source.add_argument("--audio", type=Path, help="音频文件；ffmpeg 转为 16kHz/单声道/60ms Opus")
    request.add_argument("--scenario", choices=("chat", "weather", "news", "mcp"), default="chat")
    request.add_argument("--mcp", action=argparse.BooleanOptionalAction, default=False)
    request.add_argument("--expect-tool", help="实际未调用指定工具时返回失败")
    request.add_argument("--timeout", type=float, default=90)
    request.add_argument("--with-logs", action="store_true", help="在 Docker 宿主机补充内部日志指标")
    request.add_argument("--mqtt-host", help="覆盖 OTA MQTT 主机，用于本机对比；端口不变")
    request.add_argument("--udp-host", help="覆盖 hello UDP 主机，用于本机对比；端口不变")
    for mode in (logs, request):
        mode.add_argument("--server-container", default="xiaozhi-esp32-server_showroom")
        mode.add_argument("--mqtt-container", default="xiaozhi-esp32-server_showroom-xiaozhi-mqtt-gateway-1")
        mode.add_argument("--agent-container", default="museum-guide-agent-showroom")
        mode.add_argument("--json-out", type=Path)
        mode.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR,
                          help="每次运行的诊断日志目录，默认 KSZ/logs")
        mode.add_argument("--no-log", dest="write_log", action="store_false", default=True,
                          help="不写入本次运行的 KSZ/logs 日志文件")
    args = parser.parse_args()
    if args.mode == "request" and args.scenario == "mcp" and not args.mcp:
        parser.error("--scenario mcp 需要 --mcp")
    if args.mode == "request" and args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    if args.mode == "logs":
        since = args.since or (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)).isoformat()
        texts = {}
        for name in ("server", "mqtt", "agent"):
            path = getattr(args, name + "_log")
            texts[name] = path.read_text() if path else docker_logs(getattr(args, name + "_container"), since)
        report = analyze(**texts, query=args.query, since=args.since, device=args.device)
        exit_code = 0
    else:
        from latency_mqtt import run_request
        since = dt.datetime.now(dt.timezone.utc).isoformat()
        report, exit_code = run_request(args)
        if args.with_logs:
            try:
                texts = {name: docker_logs(getattr(args, name + "_container"), since)
                         for name in ("server", "mqtt", "agent")}
                internal = analyze(**texts, query=report["recognized_text"] or report["query"],
                                   since=since, device=args.device)
                report["warnings"].extend(internal.pop("warnings"))
                report.update({key: value for key, value in internal.items() if key != "query"})
            except ValueError as error:
                report["warnings"].append(str(error))
                exit_code = 1
        if args.expect_tool and args.expect_tool not in report.get("observed_tools", []):
            report["warnings"].append("未实际调用预期工具：" + args.expect_tool)
            exit_code = 1
    log_path = write_run_log(report, args.mode, args.log_dir, exit_code) if args.write_log else None
    display(report)
    if log_path:
        print(f"\n日志文件：{log_path}")
    if args.json_out:
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"诊断失败：{error}", file=sys.stderr)
        sys.exit(1)
