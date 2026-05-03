"""
Text-to-speech handler (stub).

Defaults to pyttsx3 (offline, local). Returns base64-encoded audio that the
frontend can play directly via `new Audio("data:audio/wav;base64,...")`.
"""

import asyncio
import base64
import tempfile
from pathlib import Path
from typing import Dict

from utils.logger import setup_logger

logger = setup_logger(__name__)


class TTSHandler:
    def __init__(self, config: Dict):
        cfg = config.get("tts", {})
        self.engine_name = cfg.get("engine", "pyttsx3")
        self.rate = cfg.get("rate", 180)
        self.voice = cfg.get("voice", "default")

    async def synthesize(self, text: str) -> str:
        """
        Synthesize speech from `text`. Returns base64-encoded WAV, or empty
        string on failure.
        """
        if not text:
            return ""

        # pyttsx3 is blocking — run in a thread so we don't block the event loop.
        return await asyncio.to_thread(self._synthesize_sync, text)

    def _synthesize_sync(self, text: str) -> str:
        try:
            import pyttsx3  # type: ignore

            engine = pyttsx3.init()
            engine.setProperty("rate", self.rate)

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                out_path = Path(tmp.name)

            try:
                engine.save_to_file(text, str(out_path))
                engine.runAndWait()
                audio_bytes = out_path.read_bytes()
                return base64.b64encode(audio_bytes).decode("ascii")
            finally:
                if out_path.exists():
                    out_path.unlink()
        except ImportError:
            logger.warning(
                "pyttsx3 not installed; TTS disabled. Install with: pip install pyttsx3"
            )
            return ""
        except Exception as exc:
            logger.error(f"TTS synthesis failed: {exc}")
            return ""
