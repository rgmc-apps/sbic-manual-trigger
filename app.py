import os

from flask import Flask

app = Flask(__name__)

from routes.pages import bp as pages_bp
from routes.buffer import bp as buffer_bp
from routes.lookups import bp as lookups_bp
from routes.overrides import bp as overrides_bp
from routes.handoff_history import bp as handoff_history_bp
from routes.shipto_fixes import bp as shipto_fixes_bp
from routes.customer_shipto import bp as customer_shipto_bp
from routes.missing_items import bp as missing_items_bp
from routes.document_ai_search import bp as document_ai_search_bp
from routes.reprocess import bp as reprocess_bp
from routes.legacy_trigger import bp as legacy_trigger_bp
from routes.dashboard import bp as dashboard_bp
from routes.handoff_retrigger import bp as handoff_retrigger_bp

app.register_blueprint(pages_bp)
app.register_blueprint(buffer_bp)
app.register_blueprint(lookups_bp)
app.register_blueprint(overrides_bp)
app.register_blueprint(handoff_history_bp)
app.register_blueprint(shipto_fixes_bp)
app.register_blueprint(customer_shipto_bp)
app.register_blueprint(missing_items_bp)
app.register_blueprint(document_ai_search_bp)
app.register_blueprint(reprocess_bp)
app.register_blueprint(legacy_trigger_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(handoff_retrigger_bp)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
