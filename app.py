"""
app.py — EnergyTrack Dashboard (Vercel Serverless)
==========================================
Nessun salvataggio su server: i dati arrivano dal GME a ogni apertura.
Per stare nei limiti di Vercel il gas si scarica un mese per richiesta e
le risposte vengono messe in cache sulla CDN di Vercel (s-maxage): i mesi
chiusi restano in cache 30 giorni, il mese in corso e il PUN 15 minuti.

Endpoints:
  GET  /                → Dashboard HTML
  GET  /api/pun         → PUN giornaliero e mensile (GME)
  GET  /api/gas/YYYY-MM → Prezzi gas MGP-GAS di un mese (GME, €/MWh)
  POST /api/simula      → Calcola stima bolletta con parametri custom
  POST /api/parametri   → Aggiorna parametri temporanei in memoria
"""

from datetime import date, timedelta
from flask import Flask, jsonify, render_template, request

import fetch_data

app = Flask(__name__, template_folder="templates")

CACHE_BREVE = "public, max-age=0, s-maxage=900, stale-while-revalidate=3600"
CACHE_LUNGA = "public, max-age=0, s-maxage=2592000"


@app.after_request
def add_header(r):
    # Le API impostano la propria cache CDN; tutto il resto non va in cache
    if "Cache-Control" not in r.headers:
        r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, public, max-age=0"
        r.headers["Pragma"] = "no-cache"
        r.headers["Expires"] = "0"
    return r


def _errore(e: Exception):
    print(f"[ERROR] {e}")
    return jsonify({"errore": fetch_data._motivo(e)}), 502


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/pun")
def api_pun():
    try:
        daily, monthly = fetch_data.fetch_pun(months=14)
    except Exception as e:
        return _errore(e)
    r = jsonify({"pun_daily": daily, "pun_monthly": monthly})
    r.headers["Cache-Control"] = CACHE_BREVE
    return r


@app.route("/api/gas/<int:anno>-<int:mese>")
def api_gas(anno, mese):
    if not 1 <= mese <= 12:
        return jsonify({"errore": "Mese non valido"}), 400
    try:
        giorni = fetch_data.fetch_psv_mese(anno, mese)
    except Exception as e:
        return _errore(e)
    # Un mese è definitivo quando anche le ultime sessioni sono chiuse
    fine_mese = date(anno + mese // 12, mese % 12 + 1, 1) - timedelta(days=1)
    definitivo = fine_mese < date.today() - timedelta(days=fetch_data.GIORNI_GAS_DA_RICONTROLLARE)
    r = jsonify({"mese": f"{anno}-{mese:02d}", "giorni": giorni, "definitivo": definitivo})
    r.headers["Cache-Control"] = CACHE_LUNGA if definitivo and giorni else CACHE_BREVE
    return r


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
            canone_tv=float(body.get("canone_tv", 0)),
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
