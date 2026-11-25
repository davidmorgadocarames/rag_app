"""Shared conversational understanding layer (T14.2.1-T14.2.3, phase 14, block 14-B2).

``rag_app.assistant.fast_path`` -- exact greeting/thanks/farewell detection (en/es) that
skips the LLM router only (T14.2.2). ``rag_app.assistant.router`` -- the LLM router that
classifies everything else (T14.2.3). ``rag_app.assistant.core`` -- the shared
understand -> retrieve -> answer core used by the stream, ``POST /chat`` and the eval
runner (T14.2.1).
"""

from __future__ import annotations
