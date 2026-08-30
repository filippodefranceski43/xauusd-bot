"""
Bot di monitoraggio XAUUSD -> notifica Telegram (versione GitHub Actions)
=========================================================================

COSA FA QUESTO FILE (bot.py):
- Fa UN SOLO controllo del mercato XAU/USD e poi termina (non resta acceso).
- E' pensato per essere lanciato automaticamente ogni minuto da GitHub
  Actions (gratis, repository pubblica), che si occupa lui di "risvegliarlo".
- Se e' fuori dalla fascia oraria attiva (9:00-23:00, Europe/Rome), il
  controllo si ferma subito senza fare nulla.
- Se il mercato e' chiuso (weekend, o dati fermi/non aggiornati), non
  genera segnali (per evitare segnali basati su dati vecchi).
- Ti avvisa su Telegram quando il mercato PASSA da aperto a chiuso o
  viceversa (non ad ogni controllo, solo al cambiamento). Il controllo
  resta legato alla fascia attiva 9:00-23:00, quindi un cambiamento
  avvenuto fuori da quella fascia viene notificato al primo controllo
  utile dentro l'orario attivo, non nell'istante esatto.
- Se le condizioni tecniche indicano un possibile "buon momento" (mercato
  aperto), manda un messaggio Telegram con direzione, entrata, Stop Loss,
  3 Take Profit, e salva il segnale su Cloudflare KV (un mini-database
  condiviso) cosi' il Worker (vedi sotto) puo' usarlo quando rispondi con
  budget e rischio%.
- NON si collega a MetaTrader 5 e NON esegue nessuna operazione da solo.
  L'esecuzione resta sempre manuale, a te.

COSA NON FA PIU' QUESTO FILE (rispetto a prima):
Le risposte istantanee ai tuoi messaggi Telegram ("mercato",
"budget rischio%") NON sono piu' gestite qui: sono state spostate in un
Cloudflare Worker (file separato, webhook.js) che risponde in tempo reale
quando scrivi al bot, invece di aspettare il prossimo controllo periodico.
Questo file (bot.py) continua a occuparsi SOLO di controllare il mercato e
mandare le notifiche di segnale quando li trova.

CONFIGURAZIONE:
Le chiavi (token Telegram, chat ID, API key Twelve Data, credenziali
Cloudflare) NON vanno scritte in questo file: si impostano come "Secrets"
nel repository GitHub (vedi LEGGIMI.md), e arrivano allo script
automaticamente come variabili d'ambiente quando gira su GitHub Actions.
"""

import sys
import json
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
# TELEGRAM (solo invio - le risposte ai messaggi sono nel Worker)
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
        f"💰 Rispondimi con <b>budget e rischio%</b> (es. <code>300 5</code> "
        f"= budget 300€, rischio 5%) e ti calcolo il volume esatto da "
        f"inserire su MT5.\n\n"
        f"⚠️ Verifica sempre tu prima di operare. Questo NON è un consiglio "
        f"finanziario, è un filtro automatico basato su regole tecniche."
    )


# ---------------------------------------------------------------------------
# CLOUDFLARE KV - salva l'ultimo segnale cosi' il Worker puo' leggerlo
# quando rispondi con "budget rischio%"
# ---------------------------------------------------------------------------
def save_signal_to_cloudflare_kv(signal: dict, now: datetime) -> None:
    payload = json.dumps({**signal, "creato_alle": now.isoformat()})
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{config.CLOUDFLARE_ACCOUNT_ID}"
        f"/storage/kv/namespaces/{config.CLOUDFLARE_KV_NAMESPACE_ID}/values/ultimo_segnale"
    )
    headers = {
        "Authorization": f"Bearer {config.CLOUDFLARE_API_TOKEN}",
        "Content-Type": "text/plain",
    }
    resp = requests.put(url, headers=headers, data=payload, timeout=15)
    if not resp.ok:
        log.error("Errore salvataggio segnale su Cloudflare KV: %s", resp.text)
    else:
        log.info("Segnale salvato su Cloudflare KV.")


# ---------------------------------------------------------------------------
# CLOUDFLARE KV - stato mercato (aperto/chiuso), per notificarti solo
# quando CAMBIA, non ad ogni controllo
# ---------------------------------------------------------------------------
def get_market_state_kv():
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{config.CLOUDFLARE_ACCOUNT_ID}"
        f"/storage/kv/namespaces/{config.CLOUDFLARE_KV_NAMESPACE_ID}/values/stato_mercato"
    )
    headers = {"Authorization": f"Bearer {config.CLOUDFLARE_API_TOKEN}"}
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code == 404:
        return None  # prima esecuzione in assoluto, nessuno stato salvato ancora
    if not resp.ok:
        log.error("Errore lettura stato mercato da Cloudflare KV: %s", resp.text)
        return None
    return resp.text.strip()


def save_market_state_kv(stato: str) -> None:
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{config.CLOUDFLARE_ACCOUNT_ID}"
        f"/storage/kv/namespaces/{config.CLOUDFLARE_KV_NAMESPACE_ID}/values/stato_mercato"
    )
    headers = {
        "Authorization": f"Bearer {config.CLOUDFLARE_API_TOKEN}",
        "Content-Type": "text/plain",
    }
    resp = requests.put(url, headers=headers, data=stato, timeout=15)
    if not resp.ok:
        log.error("Errore salvataggio stato mercato su Cloudflare KV: %s", resp.text)


def notifica_se_cambiato_stato_mercato(chiuso: bool) -> None:
    """Manda una notifica Telegram SOLO quando il mercato passa da aperto a
    chiuso o viceversa (non ad ogni controllo)."""
    stato_attuale = "chiuso" if chiuso else "aperto"
    stato_precedente = get_market_state_kv()

    if stato_precedente is not None and stato_precedente != stato_attuale:
        log.info("Transizione mercato rilevata: %s -> %s", stato_precedente, stato_attuale)
        if stato_attuale == "chiuso":
            messaggio = (
                "📴 <b>Mercato XAUUSD CHIUSO</b>\n\n"
                "Il mercato dell'oro si è appena chiuso (weekend). Riapre "
                "indicativamente domenica sera / lunedì mattina. In questo "
                "periodo non riceverai notifiche di segnale."
            )
        else:
            messaggio = (
                "🟢 <b>Mercato XAUUSD APERTO</b>\n\n"
                "Il mercato dell'oro è appena riaperto. Il monitoraggio dei "
                "segnali riprende normalmente."
            )
        send_telegram_message(messaggio)

    save_market_state_kv(stato_attuale)


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

    notifica_se_cambiato_stato_mercato(chiuso)

    if chiuso:
        log.info("Mercato chiuso (weekend o dati non aggiornati): nessun controllo segnali.")
        return

    signal = generate_signal(df)

    if signal:
        log.info("Segnale trovato: %s", signal)
        save_signal_to_cloudflare_kv(signal, now)
        send_telegram_message(format_signal_message(signal))
    else:
        log.info("Nessuna condizione di ingresso al momento.")


if __name__ == "__main__":
    main()
