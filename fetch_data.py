"""
fetch_data.py — Recupero dati reali PUN e PSV dal GME
======================================================
Fonte unica: sezione "Esiti" del portale gme.mercatoelettrico.org.
Le pagine Esiti leggono i dati da API interne del sito (DNN), che rispondono
solo se la richiesta porta i codici di sessione rilasciati dalla pagina stessa
(ModuleId, TabId, RequestVerificationToken + cookie). Qui si fa lo stesso.

PUN : Esiti → Elettricità → MGP → PUN Index GME
      medie giornaliere e mensili ufficiali (€/MWh → €/kWh).
PSV : Esiti → Gas → MGP-GAS → Negoziazione Continua
      prezzo di riferimento del prodotto giornaliero MGP-<giorno di consegna>
      (€/MWh → €/Smc con PCS = 0.03852 GJ/Smc).

Nessuna stima: se il GME non risponde si mantengono gli ultimi dati salvati
e si segnala l'avviso. Tutto viene salvato in dati_energia.json.
"""

import json
import re
import statistics
import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta, datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
OUTPUT = BASE_DIR / "dati_energia.json"

# ─── Parametri contratto Sorgenia ────────────────────────────────────────────
PCS = 0.03852           # GJ/Smc (fattore di conversione gas)
PERDITE_RETE = 0.10     # 10% in bassa tensione (condizioni economiche e bolletta set-26)
FEE_LUCE = 0.007        # €/kWh — Listino 0.014 meno 0.007 di sconto per contatore orario
FEE_GAS = 0.05          # €/Smc — Primi 12 mesi (dal 13° mese: 0.10 €/Smc)
FEE_LUCE_ANNO2 = 0.030  # €/kWh — Dal 13° mese: 0.037 meno 0.007 (+ Indice GO)
FEE_GAS_ANNO2 = 0.10    # €/Smc — Dal 13° mese di fornitura
QUOTA_FISSA_POD = 73.08 # €/anno — In bolletta 6.09 €/mese (il contratto dice 72: chiesto a Sorgenia)
QUOTA_FISSA_PDR = 72.0  # €/anno — Corrispettivo commercializzazione gas (contratto)
SCONTO_FEDELTA_MESE = 0.30  # €/mese per fornitura — 5% di 72 €/anno nei primi 12 mesi
# Contatore orario: ogni ora si paga al PUN di quell'ora e i consumi serali costano di più.
# Rapporto PUN pagato / PUN medio GME, ricavato dalla bolletta di set-26 (0,21717 / 0,20754).
COEFF_PROFILO_ORARIO = 1.0464
MESI_SCONTO = 15        # numero di bollette con sconto SEQUOIA15
SCONTO_TOT = 100.0      # € totali sconto (50€ luce + 50€ gas)

# ─── Componenti ARERA e imposte (dalle bollette di settembre 2026) ───────────
# ARERA le aggiorna ogni trimestre: riprenderle dagli "Elementi di dettaglio"
# dell'ultima bolletta quando cambiano.
DISPACCIAMENTO_LUCE = 0.01505289       # €/kWh — corrispettivo CDISPD
POTENZA_KW = 3.0                       # kW impegnati
RETE_LUCE_FISSA = 1.92                 # €/mese
RETE_LUCE_POTENZA = 1.97656667         # €/kW/mese
RETE_LUCE_KWH = 0.0119 + 0.00283       # €/kWh — distribuzione + maggiorazioni UC
ONERI_LUCE_KWH = 0.031515 + 0.001638   # €/kWh — ASOS + ARIM
ACCISA_LUCE_KWH = 0.0227               # €/kWh — residenti fino a 3 kW: vedi _accisa_luce()
IVA_LUCE = 0.10

RETE_GAS_FISSA = 6.1325 + 1.15041667   # €/mese — distribuzione + canone comunale
RETE_GAS_SMC = 0.07668019 + 0.111183 + 0.03846154  # €/Smc — trasporto, distribuzione, altri oneri (scaglione 120-480)
ONERI_GAS_FISSA = -1.8025              # €/mese — UG2 (è uno sconto)
ONERI_GAS_SMC = 0.0496 + 0.02820513 + 0.00769231   # €/Smc — UG2, altri oneri, UG3
ACCISA_GAS_SMC = 0.175 + 0.005165      # €/Smc — erariale + addizionale regionale (scaglione 120-480 Smc/anno)
IVA_GAS_CONSUMI = 0.10                 # sulla parte a consumo (fino a 480 Smc/anno)
IVA_GAS_FISSA = 0.22                   # sulle quote fisse

CANONE_TV_MESE = 9.0                   # €/mese, addebitato da gennaio a ottobre

# ─── Portale GME ─────────────────────────────────────────────────────────────
GME_SITE = "https://gme.mercatoelettrico.org"
PAGINA_PUN = "/it-it/Home/Esiti/Elettricita/MGP/Esiti/PUN"
PAGINA_GAS = "/it-it/Home/Esiti/Gas/MGP-GAS/Esiti/NegoziazioneContinua"
API_PUN = GME_SITE + "/DesktopModules/GmeEsitiPrezziME/API/item/GetMEPrezzi"
API_GAS = GME_SITE + "/DesktopModules/GmeEsitiMGAS/API/item/GetGasEsitiMGAS"
FONTE = "Reale (GME)"
FONTE_PARZIALE = "Reale (GME) · mese in corso"
GIORNI_GAS_DA_RICONTROLLARE = 3  # le sessioni più recenti possono essere ancora aperte

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
}


class GMEError(Exception):
    pass


# ─── Sessione GME ────────────────────────────────────────────────────────────

def _sessione_gme(pagina: str, modulo: str) -> tuple[requests.Session, dict]:
    """
    Apre la pagina Esiti come farebbe un browser e ricava gli header che le
    API del modulo DNN richiedono. Ritorna (sessione con cookie, header).
    """
    s = requests.Session()
    s.headers.update(HEADERS)
    r = s.get(GME_SITE + pagina, timeout=20)
    r.raise_for_status()
    html = r.text

    token = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', html)
    inizio = html.find(f'window["{modulo}"]')
    blocco = html[inizio:html.find("</script>", inizio)] if inizio >= 0 else ""
    module_id = re.search(r'"ModuleId"\s*:\s*(\d+)', blocco)
    tab_id = re.search(r'"TabId"\s*:\s*(\d+)', blocco)
    if not (token and module_id and tab_id):
        raise GMEError(f"Pagina GME cambiata: impossibile leggere i codici di sessione per {modulo}")

    return s, {
        "ModuleId": module_id.group(1),
        "TabId": tab_id.group(1),
        "RequestVerificationToken": token.group(1),
        "Accept": "application/json",
        "Referer": GME_SITE + pagina,
    }


def _get_json(s: requests.Session, headers: dict, url: str, params: dict):
    r = s.get(url, headers=headers, params=params, timeout=20)
    if r.status_code != 200:
        raise GMEError(f"GME ha risposto HTTP {r.status_code} ({url.rsplit('/', 1)[-1]})")
    return r.json()


def _yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _inizio_periodo(months: int) -> date:
    """Primo giorno del mese di `months` mesi fa (così i mesi sono completi)."""
    oggi = date.today()
    m = oggi.year * 12 + (oggi.month - 1) - months
    return date(m // 12, m % 12 + 1, 1)


# ─── PUN: Esiti MGP → PUN Index GME ──────────────────────────────────────────

def fetch_pun(months: int = 14) -> tuple[dict[str, float], dict[str, float]]:
    """
    Ritorna (giornaliero {YYYY-MM-DD: €/kWh}, mensile {YYYY-MM: €/kWh}).
    Il GME pubblica verso le 13 anche il PUN del giorno dopo: è incluso.
    """
    s, headers = _sessione_gme(PAGINA_PUN, "GmeEsitiPrezziME")
    base = {"Mercato": "MGP", "Zona": "PUN", "Tipologia": "PUN",
            "DataInizio": _yyyymmdd(_inizio_periodo(months)),
            "DataFine": _yyyymmdd(date.today() + timedelta(days=1))}

    giorni = _get_json(s, headers, API_PUN, {**base, "Granularita": "d"})
    mesi = _get_json(s, headers, API_PUN, {**base, "Granularita": "m"})
    if not giorni:
        raise GMEError("Il GME non ha restituito dati PUN giornalieri")

    daily = {}
    for row in giorni:
        df = str(row["df"])
        daily[f"{df[:4]}-{df[4:6]}-{df[6:8]}"] = round(row["p"] / 1000, 6)

    monthly = {}
    for row in mesi:
        df = str(row["df"])
        monthly[f"{df[:4]}-{df[4:6]}"] = round(row["p"] / 1000, 6)

    return dict(sorted(daily.items())), dict(sorted(monthly.items()))


# ─── PSV: Esiti MGP-GAS → prezzo di riferimento ──────────────────────────────

def fetch_psv_mwh(months: int = 14, cache: dict[str, float] | None = None) -> dict[str, float]:
    """
    Ritorna {YYYY-MM-DD (giorno di consegna): €/MWh}.
    Il GME espone una sessione di negoziazione per richiesta, quindi si
    scaricano solo i giorni mancanti in cache più gli ultimi giorni (ancora
    soggetti a variazione). Il prodotto MGP-D può essere trattato in più
    sessioni (es. nel weekend): vale il prezzo dell'ultima sessione.
    """
    prezzi = dict(cache or {})
    inizio = _inizio_periodo(months)
    domani = date.today() + timedelta(days=1)
    soglia_ricontrollo = date.today() - timedelta(days=GIORNI_GAS_DA_RICONTROLLARE)

    consegne = [inizio + timedelta(days=i) for i in range((domani - inizio).days + 1)]
    sessioni = sorted({c - timedelta(days=1) for c in consegne
                       if c.isoformat() not in prezzi or c >= soglia_ricontrollo})
    if not sessioni:
        return prezzi

    print(f"    --> {len(sessioni)} sessioni MGP-GAS da scaricare")
    _prezzi_riferimento(_scarica_sessioni(sessioni), prezzi)

    limite = inizio.isoformat()
    return {k: v for k, v in sorted(prezzi.items()) if k >= limite}


def fetch_psv_mese(anno: int, mese: int, parallelo: int = 4) -> dict[str, float]:
    """
    Prezzi gas {YYYY-MM-DD: €/MWh} per i giorni di consegna di un solo mese
    (fino a domani). Usato dalla versione Vercel, che scarica un mese per
    richiesta così ogni mese chiuso resta in cache sulla CDN.
    """
    primo = date(anno, mese, 1)
    ultimo = date(anno + mese // 12, mese % 12 + 1, 1) - timedelta(days=1)
    fine = min(ultimo, date.today() + timedelta(days=1))
    if fine < primo:
        return {}
    sessioni = [primo + timedelta(days=i - 1) for i in range((fine - primo).days + 1)]
    prezzi = _prezzi_riferimento(_scarica_sessioni(sessioni, parallelo), {})
    return {k: v for k, v in sorted(prezzi.items()) if primo.isoformat() <= k <= fine.isoformat()}


def _scarica_sessioni(sessioni: list[date], parallelo: int = 1) -> list[list[dict]]:
    """Esiti MGP-GAS di ogni sessione, nello stesso ordine di `sessioni`."""
    s, headers = _sessione_gme(PAGINA_GAS, "GmeEsitiMGAS")

    def una(sessione: date) -> list[dict]:
        return _get_json(s, headers, API_GAS, {"DataSessione": _yyyymmdd(sessione), "Mercato": "MGP"})

    if parallelo > 1:
        with ThreadPoolExecutor(max_workers=parallelo) as pool:
            return list(pool.map(una, sessioni))
    return [una(sessione) for sessione in sessioni]


def _prezzi_riferimento(risposte: list[list[dict]], prezzi: dict[str, float]) -> dict[str, float]:
    """Aggiorna `prezzi` con il prezzo di riferimento dei prodotti MGP-<giorno>; l'ultima sessione vince."""
    for righe in risposte:
        for riga in righe:
            prodotto = riga.get("prodotto") or ""
            rif = riga.get("prezzoRiferimento")
            if prodotto.startswith("MGP-") and rif is not None:
                prezzi[prodotto[4:]] = round(float(rif), 6)
    return prezzi


def psv_smc(mwh: float) -> float:
    """€/MWh → €/Smc (1 Smc = PCS GJ = PCS/3.6 MWh)."""
    return round(mwh * PCS / 3.6, 6)


# ─── Calcolo medie mensili ────────────────────────────────────────────────────

def medie_mensili(daily: dict[str, float]) -> dict[str, float]:
    per_mese: dict[str, list] = {}
    for giorno, val in daily.items():
        per_mese.setdefault(giorno[:7], []).append(val)
    return {mese: round(statistics.mean(v), 6) for mese, v in sorted(per_mese.items())}


def fonti_mensili(mesi) -> dict[str, str]:
    mese_corrente = date.today().strftime("%Y-%m")
    return {m: FONTE_PARZIALE if m >= mese_corrente else FONTE for m in mesi}


# ─── Stima bolletta Sorgenia ─────────────────────────────────────────────────

def _accisa_luce(kwh: float) -> float:
    """
    Accisa per abitazione di residenza con potenza fino a 3 kW (bolletta mensile):
    esente fino a 150 kWh, tassati i kWh oltre 150 fino a 220, oltre 220 kWh
    l'agevolazione si perde su tutto il consumo. Approssimazione: da verificare
    con la prima bolletta invernale sopra i 150 kWh.
    """
    if kwh <= 150:
        return 0.0
    if kwh <= 220:
        return (kwh - 150) * ACCISA_LUCE_KWH
    return kwh * ACCISA_LUCE_KWH


def stima_bolletta(
    pun_medio: float,       # €/kWh — media mensile GME (corretta con COEFF_PROFILO_ORARIO)
    psv_medio: float,       # €/Smc
    kwh: float,             # kWh consumati nel mese
    smc: float,             # Smc consumati nel mese
    sconto_residuo: float = 0.0,
    canone_tv: float = 0.0,
) -> dict:
    """
    Stima della bolletta mensile Sorgenia completa, voce per voce come negli
    "Elementi di dettaglio": vendita, rete, oneri di sistema, accise e IVA.
    """
    # Luce (IVA 10% su tutto)
    pun_pagato = pun_medio * COEFF_PROFILO_ORARIO
    luce = {
        "energia": pun_pagato * (1 + PERDITE_RETE) * kwh + (FEE_LUCE + DISPACCIAMENTO_LUCE) * kwh,
        "vendita_fissa": QUOTA_FISSA_POD / 12 - SCONTO_FEDELTA_MESE,
        "rete": RETE_LUCE_FISSA + RETE_LUCE_POTENZA * POTENZA_KW + RETE_LUCE_KWH * kwh,
        "oneri": ONERI_LUCE_KWH * kwh,
        "accise": _accisa_luce(kwh),
    }
    luce["iva"] = sum(luce.values()) * IVA_LUCE
    spesa_luce = sum(luce.values())

    # Gas (IVA 10% sulla parte a consumo, 22% sulle quote fisse)
    gas = {
        "energia": (psv_medio + FEE_GAS) * smc,
        "vendita_fissa": QUOTA_FISSA_PDR / 12 - SCONTO_FEDELTA_MESE,
        "rete": RETE_GAS_FISSA + RETE_GAS_SMC * smc,
        "oneri": ONERI_GAS_FISSA + ONERI_GAS_SMC * smc,
        "accise": ACCISA_GAS_SMC * smc,
    }
    parte_consumi = gas["energia"] + (RETE_GAS_SMC + ONERI_GAS_SMC) * smc + gas["accise"]
    parte_fissa = gas["vendita_fissa"] + RETE_GAS_FISSA + ONERI_GAS_FISSA
    gas["iva"] = parte_consumi * IVA_GAS_CONSUMI + parte_fissa * IVA_GAS_FISSA
    spesa_gas = sum(gas.values())

    totale = spesa_luce + spesa_gas + canone_tv - sconto_residuo

    return {
        "prezzo_luce_kwh": round(spesa_luce / kwh, 5) if kwh > 0 else 0,
        "spesa_luce": round(spesa_luce, 2),
        "dettagli_luce": {k: round(v, 2) for k, v in luce.items()},
        "prezzo_gas_smc": round(spesa_gas / smc, 4) if smc > 0 else 0,
        "spesa_gas": round(spesa_gas, 2),
        "dettagli_gas": {k: round(v, 2) for k, v in gas.items()},
        "canone_tv": round(canone_tv, 2),
        "sconto": round(sconto_residuo, 2),
        "totale": round(max(totale, 0), 2)
    }


# ─── Main: aggiorna dati_energia.json ────────────────────────────────────────

def _motivo(e: Exception) -> str:
    """Messaggio breve e leggibile per l'avviso in dashboard."""
    if isinstance(e, GMEError):
        return str(e)
    if isinstance(e, (requests.ConnectionError, requests.Timeout)):
        return "GME non raggiungibile: connessione assente o sito non disponibile"
    return f"errore imprevisto ({type(e).__name__})"


def _leggi_cache() -> dict:
    """Ritorna i dati salvati solo se provengono dal GME (scarta le vecchie stime TTF)."""
    try:
        with open(OUTPUT, encoding="utf-8") as f:
            dati = json.load(f)
        return dati if dati.get("fonte_dati") == "GME" else {}
    except (OSError, ValueError):
        return {}


def aggiorna(months: int = 14) -> dict:
    cache = _leggi_cache()
    avvisi = []

    print("[*] Recupero PUN dal GME...")
    try:
        pun_daily, pun_monthly = fetch_pun(months)
        print(f"    --> {len(pun_daily)} giorni PUN | {len(pun_monthly)} mesi")
    except Exception as e:
        print(f"[ERROR] PUN: {e}")
        avvisi.append(f"PUN non aggiornato ({_motivo(e)})")
        pun_daily, pun_monthly = cache.get("pun_daily", {}), cache.get("pun_monthly", {})

    print("[*] Recupero PSV (MGP-GAS) dal GME...")
    psv_cache = cache.get("psv_daily_mwh", {})
    try:
        psv_mwh = fetch_psv_mwh(months, psv_cache)
        print(f"    --> {len(psv_mwh)} giorni PSV")
    except Exception as e:
        print(f"[ERROR] PSV: {e}")
        avvisi.append(f"PSV non aggiornato ({_motivo(e)})")
        psv_mwh = psv_cache

    if not pun_daily and not psv_mwh:
        raise GMEError("; ".join(avvisi) or "Nessun dato disponibile dal GME")

    psv_daily = {k: psv_smc(v) for k, v in psv_mwh.items()}
    psv_monthly = medie_mensili(psv_daily)

    # Se non è arrivato nulla di nuovo, la data di aggiornamento resta quella dei dati salvati
    nessun_dato_nuovo = len(avvisi) == 2
    payload = {
        "aggiornato_il": cache.get("aggiornato_il") if nessun_dato_nuovo else datetime.now().isoformat(timespec="seconds"),
        "fonte_dati": "GME",
        "avviso": " · ".join(avvisi) or None,
        "parametri": {
            "fee_luce_kwh": FEE_LUCE,
            "fee_luce_anno2_kwh": FEE_LUCE_ANNO2,
            "fee_gas_smc": FEE_GAS,
            "fee_gas_anno2_smc": FEE_GAS_ANNO2,
            "perdite_rete": PERDITE_RETE,
            "quota_fissa_pod_anno": QUOTA_FISSA_POD,
            "quota_fissa_pdr_anno": QUOTA_FISSA_PDR,
            "pcs_gj_smc": PCS,
        },
        "pun_daily": pun_daily,
        "psv_daily": psv_daily,
        "psv_daily_mwh": psv_mwh,
        "pun_monthly": pun_monthly,
        "fonti_pun": fonti_mensili(pun_monthly),
        "psv_monthly": psv_monthly,
        "fonti_psv": fonti_mensili(psv_monthly),
    }

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[OK] Salvato in {OUTPUT}" + (f" (con avvisi: {payload['avviso']})" if avvisi else ""))
    return payload


if __name__ == "__main__":
    aggiorna()
