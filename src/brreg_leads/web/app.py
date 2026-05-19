from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from ..db import init_db
from . import routes

TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app() -> FastAPI:
    init_db()
    app = FastAPI(title="Brreg Leads", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates = templates
    app.include_router(routes.router)
    return app
