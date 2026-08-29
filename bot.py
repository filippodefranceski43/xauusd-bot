"""
Bot di monitoraggio XAUUSD -> notifica Telegram (versione GitHub Actions)
=========================================================================

COSA FA:
- Fa UN SOLO controllo del mercato XAU/USD e poi termina (non resta acceso).
- E' pensato per essere lanciato automaticamente a intervalli ravvicinati da
  GitHub Actions (gratis), che si occupa lui di "risvegliarlo".
- Se e' fuori dalla fascia oraria attiva (9:00-23:00, Europe/Rome), il
  controllo si ferma subito senza fare nulla.
- Se scrivi al bot su Telegram esattamente la parola "mercato", ad ogni
  controllo lo legge e ti risponde con una "fotografia" del mercato in
  quel momento (prezzo, RSI, trend, variazione 24h, massimo/minimo 24h,
  supporto/resistenza, volatilita'). NON e' una chat in tempo reale: la
  risposta arriva al controllo successivo (entro l'intervallo impostato
  nel workflow), non istantaneamente.
- Se il mercato e' chiuso (weekend, o dati fermi/non aggiornati), lo dice
  chiaramente sia nella risposta a "mercato" sia nei log, e in quel caso
  NON genera segnali di trading (per evitare segnali basati su dati vecchi).
- Se le condizioni tecniche indicano un possibile "buon momento" (mercato
  aperto), manda un messaggio Telegram con direzione, entrata, Stop Loss,
  3 Take Profit.
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
from datetime import datetime, timedelta

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
# MERCATO APERTO/CHIUSO
# ---------------------------------------------------------------------------
def is_market_closed(now_utc: datetime = None, last_candle_time=None) -> bool:
    """
    Stima se il mercato XAUUSD e' chiuso (weekend). Il forex/oro chiude
    circa venerdi' alle 22:00 UTC e riapre domenica alle 22:00 UTC (orari
    approssimativi, variano leggermente da broker a broker).

    In aggiunta, se l'ultima candela ricevuta dai dati e' piu' vecchia di
    90 minuti rispetto ad ora, trattiamo il mercato come chiuso/dati fermi
    anche in un giorno feriale (es. festivita').
    """
    now_utc = now_utc or datetime.now(ZoneInfo("UTC"))
    weekday = now_utc.weekday()  # Monday=0 ... Sunday=6

    weekend_closed = (
        weekday == 5  # sabato: chiuso tutto il giorno
        or (weekday == 6 and now_utc.hour < 22)  # domenica prima delle 22 UTC
        or (weekday == 4 and now_utc.hour >= 22)  # venerdi' dopo le 22 UTC
    )

    stale_data = False
    if last_candle_time is not None:
        last_candle_time_utc = last_candle_time
        if last_candle_time_utc.tzinfo is None:
            last_candle_time_utc = last_candle_time_utc.replace(tzinfo=ZoneInfo("UTC"))
        stale_data = (now_utc - last_candle_time_utc) > timedelta(minutes=90)

    return weekend_closed or stale_data


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


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range: misura la volatilita' recente del prezzo."""
    high_low = df["high"] - df["low"]
    high_prev_close = (df["high"] - df["close"].shift()).abs()
    low_prev_close = (df["low"] - df["close"].shift()).abs()
    true_range = pd.concat([high_low, high_prev_close, low_prev_close], axis=1).max(axis=1)
    return true_range.rolling(period).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["sma_fast"] = df["close"].rolling(config.SMA_FAST).mean()
    df["sma_slow"] = df["close"].rolling(config.SMA_SLOW).mean()
    df["rsi"] = compute_rsi(df["close"], config.RSI_PERIOD)
    df["recent_low"] = df["low"].rolling(config.SR_LOOKBACK).min()
    df["recent_high"] = df["high"].rolling(config.SR_LOOKBACK).max()
    df["atr"] = compute_atr(df, period=14)
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


def build_market_report(df, market_closed: bool) -> str:
    """Costruisce un messaggio con una 'fotografia' del mercato attuale."""
    now_str = datetime.now(TZ).strftime("%d/%m %H:%M")

    if market_closed:
        return (
            f"📴 <b>XAUUSD — mercato CHIUSO</b>\n"
            f"<i>{now_str}</i>\n\n"
            f"Il mercato dell'oro è chiuso in questo momento (weekend o dati "
            f"non aggiornati). Riapre indicativamente domenica sera / lunedì "
            f"mattina.\n\n"
            f"Non ti mando dati di prezzo perché con il mercato chiuso "
            f"sarebbero non aggiornati e potrebbero fuorviarti."
        )

    last = df.iloc[-1]
    price = last["close"]
    rsi = last["rsi"]
    sma_fast = last["sma_fast"]
    sma_slow = last["sma_slow"]
    support = last["recent_low"]
    resistance = last["recent_high"]
    atr = last["atr"]

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

    # Variazione e massimo/minimo delle ultime 24 ore (in base al timeframe
    # configurato: con candele da 15 min, 24h = 96 candele)
    candele_24h = int(24 * 60 / int("".join(filter(str.isdigit, config.TIMEFRAME)) or 15))
    if len(df) > candele_24h:
        finestra_24h = df.iloc[-candele_24h:]
        prezzo_24h_fa = df.iloc[-candele_24h]["close"]
        variazione_pct = ((price - prezzo_24h_fa) / prezzo_24h_fa) * 100
        massimo_24h = finestra_24h["high"].max()
        minimo_24h = finestra_24h["low"].min()
        riga_24h = (
            f"Variazione 24h: {variazione_pct:+.2f}%\n"
            f"Massimo/minimo 24h: {massimo_24h:.2f} / {minimo_24h:.2f}\n"
        )
    else:
        riga_24h = ""

    volatilita = f"{atr:.2f}" if not pd.isna(atr) else "dati insufficienti"

    return (
        f"📊 <b>XAUUSD — fotografia del mercato</b>\n"
        f"<i>{now_str}</i>\n\n"
        f"Prezzo attuale: <b>{price:.2f}</b>\n"
        f"RSI: {rsi_status}\n"
        f"Trend di fondo: {trend}\n"
        f"{riga_24h}"
        f"Supporto recente: {support:.2f}\n"
        f"Resistenza recente: {resistance:.2f}\n"
        f"Volatilità (ATR 14): {volatilita} $ a candela\n\n"
        f"⚠️ Non è un consiglio finanziario, è solo una lettura automatica "
        f"degli indicatori al momento della richiesta."
    )


def process_incoming_messages(df, market_closed: bool) -> None:
    """Legge eventuali messaggi che hai scritto al bot. Se il testo e' (o
    contiene) la parola 'mercato', risponde con il report. Ignora messaggi
    da chat diverse dalla tua, e qualsiasi altro testo."""
    updates = get_telegram_updates()
    if not updates:
        return

    my_chat_id = str(config.TELEGRAM_CHAT_ID)
    richieste_mercato = 0

    for u in updates:
        msg = u.get("message")
        if not msg:
            continue
        if str(msg.get("chat", {}).get("id")) != my_chat_id:
            continue
        testo = (msg.get("text") or "").strip().lower()
        if testo == "mercato":
            richieste_mercato += 1

    if richieste_mercato:
        log.info("Ricevuta/e %d richiesta/e 'mercato', rispondo.", richieste_mercato)
        send_telegram_message(build_market_report(df, market_closed))

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

    ultima_candela = df.iloc[-1]["datetime"]
    chiuso = is_market_closed(
        now_utc=datetime.now(ZoneInfo("UTC")),
        last_candle_time=ultima_candela,
    )
    if chiuso:
        log.info("Mercato considerato chiuso (weekend o dati non aggiornati).")

    # 1) Rispondi a eventuali messaggi "mercato" che hai scritto al bot
    process_incoming_messages(df, chiuso)

    # 2) Controlla se ci sono le condizioni per un segnale di trading
    #    (solo se il mercato e' aperto: con dati fermi non avrebbe senso)
    if chiuso:
        log.info("Mercato chiuso: nessun controllo segnali.")
        return

    signal = generate_signal(df)

    if signal:
        log.info("Segnale trovato: %s", signal)
        send_telegram_message(format_signal_message(signal))
    else:
        log.info("Nessuna condizione di ingresso al momento.")


if __name__ == "__main__":
    main()
