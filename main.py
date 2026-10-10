import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI

from bot import DB, Bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
TICK_SECONDS = 15

db = DB(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
bot = Bot(db)
state = {"last_tick": None, "last_error": None}


def run_forever():
    while True:
        try:
            bot.tick()
            state["last_tick"] = datetime.now(timezone.utc).isoformat()
            state["last_error"] = None
        except Exception as e:  # never let the loop die
            logging.exception("tick failed")
            state["last_error"] = str(e)[:300]
        time.sleep(TICK_SECONDS)


@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=run_forever, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
@app.get("/health")
def health():
    """Ping this URL every 5 minutes (UptimeRobot) so the free server stays awake."""
    return {"ok": True, **state}
