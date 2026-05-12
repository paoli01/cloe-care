"""cloe-care — Système de gestion d'incidents indépendant."""
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from db import init_db
from workers.investigate_worker import (
    is_worker_alive,
    queue_size,
    start_worker,
    stop_worker,
)

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_worker()
    try:
        yield
    finally:
        stop_worker()


app = FastAPI(
    title="cloe-care",
    description="Système de gestion d'incidents Cloe",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if os.getenv("DEBUG") == "true" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://hellocloe.fr",
        "https://www.hellocloe.fr",
        "https://app.hellocloe.fr",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "cloe-care",
        "version": "0.1.0",
        "queue_size": queue_size(),
        "worker_alive": is_worker_alive(),
    }


# Routers (branchés progressivement à chaque PR feature/*)
from routers import attachments, status as status_router, tickets  # noqa: E402

app.include_router(tickets.router)
app.include_router(attachments.router)
app.include_router(status_router.router)
