from flask import Flask

from .config import default_flask_config
from .repositories.database import init_db
from .web import register_web_hooks
from .errors import forbidden, not_found, bad_request


def create_app(config=None, *, initialize_database=True):
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.update(default_flask_config())
    if config:
        app.config.update(config)
    register_web_hooks(app)
    from .routes import admin, auth, business, dashboard, finance, inventory, reports, returns, sales
    for blueprint in (auth.bp, business.bp, dashboard.bp, finance.bp, inventory.bp, reports.bp, returns.bp, sales.bp, admin.bp):
        app.register_blueprint(blueprint)
    aliases = []
    for rule in app.url_map.iter_rules():
        if "." not in rule.endpoint:
            continue
        endpoint = rule.endpoint.rsplit(".", 1)[1]
        if endpoint not in app.view_functions:
            aliases.append((rule, endpoint, app.view_functions[rule.endpoint]))
    for rule, endpoint, view in aliases:
        if endpoint not in app.view_functions:
            app.add_url_rule(rule.rule, endpoint=endpoint, view_func=view, methods=rule.methods - {"HEAD", "OPTIONS"}, defaults=rule.defaults)
    app.register_error_handler(403, forbidden)
    app.register_error_handler(404, not_found)
    app.register_error_handler(400, bad_request)
    if initialize_database:
        init_db(app)
    return app
