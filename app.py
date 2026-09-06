"""
app.py — EnergyTrack Dashboard (Vercel Serverless)
==========================================
Endpoints:
  GET  /              → Dashboard HTML
  POST /api/aggiorna  → Scarica dati e ritorna JSON on-the-fly
  POST /api/simula    → Calcola stima bolletta con parametri custom
  POST /api/parametri → Aggiorna parametri temporanei in memoria
"""

import json
from pathlib import Path
from flask import Flask, jsonify, render_template, request

import fetch_data

BASE_DIR = Path(__file__).parent

app = Flask(__name__, template_folder="templates")

@app.after_request
def add_header(r):
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, public, max-age=0"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    return r

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/aggiorna", methods=["POST", "GET"])
def api_aggiorna():
    """Vercel serverless: scarica e ritorna i dati all'istante."""
    import urllib3
    urllib3.disable_warnings()
    try:
        data = fetch_data.aggiorna()
        return jsonify(data)
    except Exception as e:
        print(f"[ERROR] Aggiornamento fallito: {e}")
        return jsonify({"errore": str(e)}), 500

@app.route("/api/simula", methods=["POST"])
def api_simula():
    body = request.get_json(force=True)
    try:
        risultato = fetch_data.stima_bolletta(
            pun_medio=float(body.get("pun_medio", 0)),
            psv_medio=float(body.get("psv_medio", 0)),
            kwh=float(body.get("kwh", 0)),
            smc=float(body.get("smc", 0)),
            sconto_residuo=float(body.get("sconto_residuo", 0)),
        )
        return jsonify(risultato)
    except Exception as e:
        return jsonify({"errore": str(e)}), 400

@app.route("/api/parametri", methods=["POST"])
def api_aggiorna_parametri():
    body = request.get_json(force=True)
    allowed = {"fee_luce_kwh", "fee_gas_smc", "perdite_rete",
               "quota_fissa_pod_anno", "quota_fissa_pdr_anno"}
    for k, v in body.items():
        if k in allowed:
            mapping = {
                "fee_luce_kwh": "FEE_LUCE",
                "fee_gas_smc": "FEE_GAS",
                "perdite_rete": "PERDITE_RETE",
                "quota_fissa_pod_anno": "QUOTA_FISSA_POD",
                "quota_fissa_pdr_anno": "QUOTA_FISSA_PDR",
            }
            if k in mapping:
                setattr(fetch_data, mapping[k], float(v))
    
    return jsonify({"messaggio": "Parametri temporanei aggiornati per la sessione."})

if __name__ == "__main__":
    app.run(port=5000, debug=True)
