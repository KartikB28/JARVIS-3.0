"""
FastAPI server - main entry point.
Integrates all CHAPPIE systems.
"""

import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from agents.chappie import CHAPPIE
from models.tts_handler import TTSHandler
from models.whisper_handler import WhisperHandler
from utils.logger import setup_logger

logger = setup_logger(__name__)

# Load configuration relative to this file so the server runs from any cwd.
CONFIG_PATH = Path(__file__).parent / "config.json"
with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

app = FastAPI(title="CHAPPIE - AI Desktop Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

chappie = CHAPPIE(CONFIG)
whisper = WhisperHandler(CONFIG)
tts = TTSHandler(CONFIG)

active_connections = []


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "voice_input":
                transcript = await whisper.transcribe(data.get("audio_data", ""))
                await websocket.send_json({"type": "transcription", "text": transcript})

                if transcript:
                    response = await chappie.process(transcript)
                    audio = await tts.synthesize(response)
                    await websocket.send_json(
                        {"type": "response", "text": response, "audio": audio}
                    )

            elif msg_type == "text_input":
                text = data.get("text", "")
                response = await chappie.process(text)
                audio = await tts.synthesize(response)
                await websocket.send_json(
                    {"type": "response", "text": response, "audio": audio}
                )

            else:
                await websocket.send_json(
                    {"type": "error", "message": f"Unknown message type: {msg_type}"}
                )

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception as exc:
        logger.error(f"WebSocket error: {exc}")
    finally:
        if websocket in active_connections:
            active_connections.remove(websocket)


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "chappie_ready": True,
        "session_id": chappie.session_id,
    }


@app.get("/status")
async def get_status():
    return chappie.get_status()


@app.get("/knowledge")
async def get_knowledge():
    """Expose CHAPPIE's learned knowledge."""
    return {
        "preferences": chappie.kb.get_all_preferences(),
        "skills": chappie.kb.get_learned_skills(),
        "resources": [
            {
                "resource": r["resource"],
                "type": r["resource_type"],
                "access_count": r["access_count"],
            }
            for r in chappie.kb.get_frequent_resources(limit=10)
        ],
    }


@app.post("/process")
async def process_text(payload: dict):
    """Plain HTTP entry point for text input (handy for curl/CLI tests)."""
    text = payload.get("text", "")
    if not text:
        return {"error": "text is required"}
    response = await chappie.process(text)
    return {"response": response}


@app.on_event("shutdown")
async def on_shutdown():
    chappie.shutdown()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
