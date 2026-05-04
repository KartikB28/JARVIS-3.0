"""
FastAPI server - main entry point.
Integrates all CHAPPIE systems.
"""

import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import asyncio

from agents.chappie import CHAPPIE
from models.tts_handler import TTSHandler
from models.whisper_handler import WhisperHandler
from utils.logger import setup_logger

logger = setup_logger(__name__)

# Load configuration relative to this file so the server runs from any cwd.
CONFIG_PATH = Path(__file__).parent / "config.json"
with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

# The static frontend lives at <repo>/frontend (one level above backend/).
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

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


@app.on_event("startup")
async def on_startup():
    """Kick off the file index build in the background. First run can take
    30-60s on big drives; the agent is usable immediately, indexed search
    just isn't available until it finishes."""
    asyncio.create_task(_background_index_build())


async def _background_index_build():
    try:
        result = await chappie.indexer.build_index()
        logger.info(f"File index ready: {result}")
    except Exception as exc:
        logger.error(f"Background index build failed: {exc}")


@app.post("/reindex")
async def reindex():
    """Force a full rebuild of the file index."""
    return await chappie.indexer.build_index(force=True)


@app.get("/index/stats")
async def index_stats():
    return chappie.indexer.stats()


@app.get("/index/search")
async def index_search(q: str, kind: str = None, limit: int = 10):
    """Debug endpoint: return raw matches for a query."""
    matches = chappie.indexer.search(q, kind=kind, limit=limit)
    return {
        "query": q,
        "results": [
            {
                "name": m.name,
                "path": m.path,
                "kind": m.kind,
                "location": m.location,
                "score": m.score,
            }
            for m in matches
        ],
    }


@app.on_event("shutdown")
async def on_shutdown():
    chappie.shutdown()


# Serve the static frontend last so it doesn't shadow the API routes above.
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(FRONTEND_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
