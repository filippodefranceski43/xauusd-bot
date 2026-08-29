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
    signal = generate_signal(df)

    if signal:
        log.info("Segnale trovato: %s", signal)
        send_telegram_message(format_signal_message(signal))
    else:
        log.info("Nessuna condizione di ingresso al momento.")


if __name__ == "__main__":
    main()
