from __future__ import annotations

import logging
from datetime import timedelta

from flask import Flask, render_template
from markupsafe import Markup, escape
from werkzeug.middleware.proxy_fix import ProxyFix

__version__ = "1.0.0"


def create_app(config=None, start_services: bool = True) -> Flask:
    from . import api, db, hardware, security, services, views, web
    from .config import Config
    from .sync import SyncService

    cfg = config or Config.from_env()
    cfg.validate()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    app = Flask(__name__)
    app.config.update(
        PT=cfg,
        SECRET_KEY=cfg.secret_key(),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        TEMPLATES_AUTO_RELOAD=False,
    )
    if cfg.mode == "cloud":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_db(cfg.db_path)

    sync = SyncService(cfg)
    hw = hardware.HardwareService(cfg, hardware.make_scan_handler(cfg, sync))
    app.extensions["pt_sync"] = sync
    app.extensions["pt_hw"] = hw

    security.install(app)
    app.teardown_appcontext(web.close_conn)
    app.register_blueprint(views.bp)
    app.register_blueprint(api.bp)

    @app.template_filter("age")
    def _age(bd):
        return services.age_text(bd)

    @app.template_filter("nl2br")
    def _nl2br(s):
        return Markup("<br>").join(escape(line) for line in (s or "").splitlines())

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    @app.errorhandler(429)
    def _err(e):
        code = getattr(e, "code", 500)
        msg = {413: "That upload is too large.", 404: "Nothing here."}.get(code, getattr(e, "description", ""))
        return render_template("error.html", code=code, message=msg), code

    @app.errorhandler(500)
    def _err500(e):
        return render_template("error.html", code=500, message="Something broke on our side. Try again."), 500

    if start_services:
        hw.start()
        sync.start()
    return app
