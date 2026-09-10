def accept_pcm_frame(conn, pcm_frame):
    if conn.client_listen_mode == "manual":
        conn.asr_audio.append(pcm_frame)
    else:
        conn.asr_audio_queue.put(pcm_frame)
