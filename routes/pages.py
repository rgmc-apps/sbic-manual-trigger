"""Top-level page routes: dashboard, trigger index, and reconcile."""
from flask import Blueprint, render_template
from core.config import BQ_COMPANY_NAME_TERMS, PROCESSES, RECONCILE_COMPANIES

bp = Blueprint("pages", __name__)


@bp.route("/")
def dashboard_page():
    return render_template("dashboard.html", companies=sorted(BQ_COMPANY_NAME_TERMS.keys()))


@bp.route("/trigger")
def index():
    return render_template("index.html", processes=PROCESSES)


@bp.route("/reconcile")
def reconcile_page():
    return render_template("reconcile.html", companies=RECONCILE_COMPANIES)
