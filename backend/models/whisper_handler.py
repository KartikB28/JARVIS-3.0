"""
Whisper speech-to-text handler (stub).

Plug in `openai-whisper`, `faster-whisper`, or a remote STT service here.
Kept as a thin async interface so it can be swapped without touching callers.
"""

import base64
from typing import Dict, Union

from utils.logger import setup_logger

logger = setup_logger(__name__)


class WhisperHandler:
    def __init__(self, config: Dict):
        cfg = config.get("whisper", {})
        self.model_name = cfg.get("model", "base")
        self.language = cfg.get("language", "en")
        self._model = None  # Lazy-loaded on first transcribe()

    def _load_model(self):
        """Lazy-load the whisper model on first use."""
        if self._model is not None:
            return
        try:
            import whisper  # type: ignore

            logger.info(f"Loading Whisper model: {self.model_name}")
            self._model = whisper.load_model(self.model_name)
        except ImportError:
            logger.warning(
                "whisper package not installed; transcription will return empty string. "
                "Install with: pip install openai-whisper"
            )

    async def transcribe(self, audio_data: Union[str, bytes]) -> str:
        """
        Transcribe base64-encoded audio (from the browser) or raw bytes.
        Returns transcript text, or empty string on failure.
        """
        self._load_model()
        if self._model is None:
            return ""

        try:
            if isinstance(audio_data, str):
                audio_bytes = base64.b64decode(audio_data)
            else:
                audio_bytes = audio_data

            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
                tmp.write(audio_bytes)
                tmp.flush()
                result = self._model.transcribe(tmp.name, language=self.language)
                return result.get("text", "").strip()
        except Exception as exc:
            logger.error(f"Whisper transcription failed: {exc}")
            return ""
