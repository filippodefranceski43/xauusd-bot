"""
Bot di monitoraggio XAUUSD -> notifica Telegram (versione GitHub Actions)
=========================================================================

COSA FA:
- Fa UN SOLO controllo del mercato XAU/USD e poi termina (non resta acceso).
- E' pensato per essere lanciato automaticamente ogni 15 minuti da GitHub
  Actions (gratis), che si occupa lui di "risvegliarlo" a intervalli.
- Se e' fuori dalla fascia oraria attiva (9:00-23:00, Europe/Rome), il
  controllo si ferma subito senza fare nulla.
- Se le condizioni tecniche indicano un possibile "buon momento", manda un
  messaggio Telegram con direzione, entrata, Stop Loss, 3 Take Profit.
- Se nel frattempo TU hai scritto un messaggio al bot, ad ogni controllo lo
  legge e ti risponde con una "fotografia" del mercato in quel momento
  (prezzo attuale, RSI, trend, supporto/resistenza). NON e' una chat in
  tempo reale: la risposta arriva al controllo successivo (quindi entro
  l'intervallo impostato nel workflow, es. 5 minuti), non istantaneamente.
- NON si collega a MetaTrader 5 e NON esegue nessuna operazione da solo.
  L'esecuzione resta sempre manuale, a te.

PERCHE' QUESTA VERSIONE E' DIVERSA DALLA PRIMA:
La prima versione era pensata per restare accesa 24/7 su un PC o un VPS
(quindi con un costo o un PC sempre acceso). Questa versione invece non
resta mai accesa: viene "chiamata" a intervalli da GitHub Actions, gira
per pochi secondi, e si spegne. Risultato: stesso comportamento per te
(notifiche solo nella fascia oraria giusta), ma a costo zero e senza
bisogno di nessun tuo dispositivo sempre attivo.

CONFIGURAZIONE:
Le chiavi (token Telegram, chat ID, API key Twelve Data) NON vanno scritte
in questo file: si impostano come "Secrets" nel repository GitHub (vedi
LEGGIMI.md), e arrivano allo script automaticamente come variabili
d'ambiente quando gira su GitHub Actions.
"""

import os
import sys
import logging
from datetime import datetime

import requests
import pandas as pd
import numpy as np

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    from backports.zoneinfo import ZoneInfo

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("xauusd-bot")

TZ = ZoneInfo("Europe/Rome")


# ---------------------------------------------------------------------------
# FASCIA ORARIA ATTIVA
# ---------------------------------------------------------------------------
def is_within_active_hours(now: datetime = None) -> bool:
    """True se l'ora attuale (Italia) e' dentro la fascia attiva configurata."""
    now = now or datetime.now(TZ)
    start = now.replace(hour=config.ORARIO_INIZIO, minute=0, second=0, microsecond=0)
    end = now.replace(hour=config.ORARIO_FINE, minute=0, second=0, microsecond=0)
    return start <= now < end


# ---------------------------------------------------------------------------
# DATI DI PREZZO (Twelve Data - piano gratuito)
# ---------------------------------------------------------------------------
def fetch_price_data(interval: str = "15min", outputsize: int = 200) -> pd.DataFrame:
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": "XAU/USD",
        "interval": interval,
        "outputsize": outputsize,
        "apikey": config.TWELVE_DATA_API_KEY,
        "order": "ASC",
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if "values" not in data:
        raise RuntimeError(f"Risposta inattesa da Twelve Data: {data}")

    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    df = df.sort_values("datetime").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# INDICATORI TECNICI
# ---------------------------------------------------------------------------
def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["sma_fast"] = df["close"].rolling(config.SMA_FAST).mean()
    df["sma_slow"] = df["close"].rolling(config.SMA_SLOW).mean()
    df["rsi"] = compute_rsi(df["close"], config.RSI_PERIOD)
    df["recent_low"] = df["low"].rolling(config.SR_LOOKBACK).min()
    df["recent_high"] = df["high"].rolling(config.SR_LOOKBACK).max()
    return df


# ---------------------------------------------------------------------------
# LOGICA DEL SEGNALE
# ---------------------------------------------------------------------------
def generate_signal(df: pd.DataFrame):
    if len(df) < max(config.SMA_SLOW, config.SR_LOOKBACK) + 5:
        return None

    last = df.iloc[-1]
    price = last["close"]
    rsi = last["rsi"]
    support = last["recent_low"]
    resistance = last["recent_high"]

    if pd.isna(rsi) or pd.isna(support) or pd.isna(resistance):
        return None

    near_support = (price - support) <= config.SR_BUFFER
    near_resistance = (resistance - price) <= config.SR_BUFFER

    if rsi <= config.RSI_OVERSOLD and near_support:
        entry = price
        sl = support - config.SL_BUFFER
        risk = entry - sl
        return {
            "direction": "BUY",
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tp1": round(entry + risk * 1.0, 2),
            "tp2": round(entry + risk * 2.0, 2),
            "tp3": round(entry + risk * 3.0, 2),
            "rsi": round(rsi, 1),
            "motivo": "RSI in ipervenduto + prezzo vicino al supporto recente",
        }

    if rsi >= config.RSI_OVERBOUGHT and near_resistance:
        entry = price
        sl = resistance + config.SL_BUFFER
        risk = sl - entry
        return {
            "direction": "SELL",
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tp1": round(entry - risk * 1.0, 2),
            "tp2": round(entry - risk * 2.0, 2),
            "tp3": round(entry - risk * 3.0, 2),
            "rsi": round(rsi, 1),
            "motivo": "RSI in ipercomprato + prezzo vicino alla resistenza recente",
        }

    return None


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------
def send_telegram_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    resp = requests.post(url, data=payload, timeout=15)
    if not resp.ok:
        log.error("Errore invio Telegram: %s", resp.text)
        sys.exit(1)


def get_telegram_updates():
    """Recupera i messaggi nuovi ricevuti dal bot (non ancora letti)."""
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
    resp = requests.get(url, params={"timeout": 0}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("result", [])


def mark_updates_as_read(updates: list) -> None:
    """Segna come 'letti' i messaggi appena processati, cosi' al prossimo
    controllo Telegram non li rimanda piu'."""
    if not updates:
        return
    last_update_id = max(u["update_id"] for u in updates)
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
    requests.get(url, params={"offset": last_update_id + 1, "timeout": 0}, timeout=15)


def build_market_report(df) -> str:
    """Costruisce un messaggio con una 'fotografia' del mercato attuale."""
    last = df.iloc[-1]
    price = last["close"]
    rsi = last["rsi"]
    sma_fast = last["sma_fast"]
    sma_slow = last["sma_slow"]
    support = last["recent_low"]
    resistance = last["recent_high"]

    if pd.isna(rsi):
        rsi_status = "dati insufficienti"
    elif rsi >= config.RSI_OVERBOUGHT:
        rsi_status = f"ipercomprato ({rsi:.1f}) — occhio a possibili storni"
    elif rsi <= config.RSI_OVERSOLD:
        rsi_status = f"ipervenduto ({rsi:.1f}) — occhio a possibili rimbalzi"
    else:
        rsi_status = f"neutrale ({rsi:.1f})"

    if pd.isna(sma_fast) or pd.isna(sma_slow):
        trend = "dati insufficienti"
    elif sma_fast > sma_slow:
        trend = "rialzista (media veloce sopra la lenta)"
    elif sma_fast < sma_slow:
        trend = "ribassista (media veloce sotto la lenta)"
    else:
        trend = "laterale"

    now_str = datetime.now(TZ).strftime("%d/%m %H:%M")

    return (
        f"📊 <b>XAUUSD — fotografia del mercato</b>\n"
        f"<i>{now_str}</i>\n\n"
        f"Prezzo attuale: <b>{price:.2f}</b>\n"
        f"RSI: {rsi_status}\n"
        f"Trend di fondo: {trend}\n"
        f"Supporto recente: {support:.2f}\n"
        f"Resistenza recente: {resistance:.2f}\n\n"
        f"⚠️ Non è un consiglio finanziario, è solo una lettura automatica "
        f"degli indicatori al momento della richiesta."
    )


def process_incoming_messages(df) -> None:
    """Legge eventuali messaggi che hai scritto al bot e risponde con un
    report del mercato. Ignora messaggi da chat diverse dalla tua."""
    updates = get_telegram_updates()
    if not updates:
        return

    my_chat_id = str(config.TELEGRAM_CHAT_ID)
    relevant = [
        u for u in updates
        if "message" in u and str(u["message"].get("chat", {}).get("id")) == my_chat_id
    ]

    if relevant:
        log.info("Trovati %d messaggi nuovi, rispondo con il report del mercato.", len(relevant))
        report = build_market_report(df)
        send_telegram_message(report)

    # Segna come letti TUTTI gli update ricevuti (anche quelli scartati),
    # cosi' non restano "in coda" per sempre.
    mark_updates_as_read(updates)


def format_signal_message(signal: dict) -> str:
    emoji = "🟢" if signal["direction"] == "BUY" else "🔴"
    return (
        f"{emoji} <b>XAUUSD {signal['direction']}</b>\n\n"
        f"Entrata: <b>{signal['entry']}</b>\n"
        f"Stop Loss: <b>{signal['sl']}</b>\n"
        f"TP1: {signal['tp1']}\n"
        f"TP2: {signal['tp2']}\n"
        f"TP3: {signal['tp3']}\n\n"
        f"RSI: {signal['rsi']}\n"
        f"Motivo: {signal['motivo']}\n\n"
        f"⚠️ Verifica sempre tu prima di operare. Questo NON è un consiglio "
        f"finanziario, è un filtro automatico basato su regole tecniche."
    )


# ---------------------------------------------------------------------------
# ESECUZIONE SINGOLA (chiamata da GitHub Actions)
# ---------------------------------------------------------------------------
def main():
    now = datetime.now(TZ)

    if not is_within_active_hours(now):
        log.info("Fuori orario attivo (%s). Nessun controllo effettuato.", now.strftime("%H:%M"))
        return

    log.info("Controllo il mercato XAUUSD alle %s...", now.strftime("%H:%M"))

    df = fetch_price_data(interval=config.TIMEFRAME, outputsize=config.CANDELE_STORICO)
    df = add_indicators(df)

    # 1) Rispondi a eventuali messaggi che hai scritto al bot nel frattempo
    process_incoming_messages(df)

    # 2) Controlla se ci sono le condizioni per un segnale di trading
    signal = generate_signal(df)

    if signal:
        log.info("Segnale trovato: %s", signal)
        send_telegram_message(format_signal_message(signal))
    else:
        log.info("Nessuna condizione di ingresso al momento.")


if __name__ == "__main__":
    main()
