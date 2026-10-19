from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO


def tts_complete_sentence(conn, text, sentence_id, *, feedback=False):
    tts = conn.tts
    method = getattr(tts, "tts_tool_feedback" if feedback else "tts_complete_sentence", None)
    if callable(method):
        return method(conn, text, sentence_id)

    from core.providers.tts.base import TTSProviderBase

    message = TTSMessageDTO(sentence_id, SentenceType.MIDDLE, ContentType.TEXT, content_detail=text)
    if getattr(type(tts), "tts_text_priority_thread", None) is TTSProviderBase.tts_text_priority_thread:
        message.ksz_complete_sentence = True
        tts.tts_text_queue.put(message)
    elif feedback:
        tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=text, sentence_id=sentence_id)
    else:
        tts.tts_text_queue.put(message)


def consume_complete_sentence(tts, message):
    tts._process_remaining_text_stream(opus_handler=tts.handle_opus)
    tts.tts_text_buff = [message.content_detail]
    tts.processed_chars = 0
    try:
        tts._process_remaining_text_stream(opus_handler=tts.handle_opus)
    finally:
        tts.tts_text_buff = []
        tts.processed_chars = 0
    tts.is_first_sentence = False
