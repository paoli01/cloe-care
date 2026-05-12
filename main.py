"""cloe-care — Système de gestion d'incidents indépendant."""
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from db import init_db

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


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
    }
