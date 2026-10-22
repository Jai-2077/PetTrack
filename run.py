"""Entry point (same file for laptop and cloud):  python run.py"""
import logging

from waitress import serve

from pettrack import create_app
from pettrack.config import Config, ConfigError

if __name__ == "__main__":
    try:
        cfg = Config.from_env()
    except ConfigError as e:
        raise SystemExit(f"Config error: {e}")
    app = create_app(cfg)
    logging.getLogger("pettrack").info(
        "PetTrack %s mode on http://%s:%s  (data: %s)", cfg.mode, cfg.host, cfg.port, cfg.data_dir
    )
    serve(app, host=cfg.host, port=cfg.port, threads=8)
