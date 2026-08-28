# Copyright 2026 Cursortouch — Auto-Use

"""LLM Provider module for managing different language model providers"""

# (connect, read) seconds for every provider HTTP call.
#
# `requests` defaults BOTH halves to None, which means block forever: a
# provider that accepts the TCP+TLS handshake and then goes silent hangs the
# agent with no way out but killing the process — the retry ladder never runs
# and the Stop flag is never read again.
#
# The read half is a bound on ONE socket read, not a cap on the whole request,
# and that distinction is the point: these calls are non-streaming, so the
# provider sends nothing at all while the model is still generating. A short
# total cap would kill a slow reasoning response seconds before it landed; a
# generous per-read bound only fires once the socket has genuinely gone quiet.
#
# Mirrors the web agent's ureq bounds in Auto_Use/web/llm_provider/mod.rs.
LLM_HTTP_TIMEOUT = (15, 180)
