"""
backtest.py - Verifica storica della logica di segnale
=========================================================

COSA FA:
Prende lo storico dei prezzi XAUUSD (dallo stesso provider, Twelve Data,
con lo stesso timeframe usato dal bot in tempo reale) e ripercorre
candela per candela chiedendosi: "in questo momento, con i dati
disponibili FINO A QUI, la logica avrebbe generato un segnale?" - esattamente
come farebbe bot.py dal vivo, senza sbirciare nel futuro (niente look-ahead
bias: ogni segnale usa solo i dati fino a quel momento).

Per ogni segnale trovato, scorre le candele successive per vedere se il
prezzo tocca prima lo Stop Loss o uno dei Take Profit, e alla fine ti manda
su Telegram un riepilogo: quanti segnali sono andati in perdita, quanti in
guadagno (e a quale TP), quanti non si sono ancora conclusi.

QUESTO SCRIPT NON GIRA IN AUTOMATICO: va lanciato manualmente da GitHub
Actions (workflow_dispatch) quando vuoi controllare le statistiche, non è
programmato per ripetersi da solo.

LIMITI ONESTI DI QUESTO BACKTEST (leggili prima di fidarti dei numeri):
- Nessuno spread, commissione o slippage incluso: nella realtà il risultato
  sarebbe leggermente peggiore.
- Il campione storico è limitato da quanti dati Twelve Data restituisce in
  una sola chiamata sul piano gratuito (settimane/pochi mesi, non anni) -
  con poche decine di segnali le percentuali hanno un margine di errore
  alto, non sono una legge statistica.
- Se una candela tocca sia lo Stop Loss sia un Take Profit, conto il caso
  peggiore (Stop Loss) - scelta prudente ma non certa al 100%.
- Ogni segnale viene valutato in modo indipendente, come se ogni volta
  partissi con un conto "fresco": non è una simulazione di equity reale
  con una sola posizione alla volta.
- Risultati passati non garantiscono risultati futuri: il mercato cambia
  comportamento nel tempo, quello che ha funzionato ieri puo' smettere di
  funzionare domani.
"""

import logging

import config
from bot import (
    fetch_price_data,
    add_indicators,
    generate_signal,
    send_telegram_message,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backtest")

OUTPUTSIZE = 5000       # max storico richiesto a Twelve Data in una chiamata
MAX_CANDELE_ATTESA = 200  # se un segnale non tocca ne' SL ne' TP entro N candele, lo conto come "non concluso"


def simula():
    log.info("Scarico lo storico XAUUSD (%s, fino a %d candele)...", config.TIMEFRAME, OUTPUTSIZE)
    df = fetch_price_data(interval=config.TIMEFRAME, outputsize=OUTPUTSIZE)
    df = add_indicators(df)
    log.info(
        "Scaricate %d candele, dal %s al %s",
        len(df), df.iloc[0]["datetime"], df.iloc[-1]["datetime"],
    )

    risultati = []
    i = max(config.SMA_SLOW, config.SR_LOOKBACK) + 5
    n = len(df)

    while i < n:
        finestra = df.iloc[: i + 1]  # solo dati FINO a i, niente sguardo al futuro
        segnale = generate_signal(finestra)

        if segnale is None:
            i += 1
            continue

        direzione = segnale["direction"]
        sl = segnale["sl"]
        tp1 = segnale["tp1"]
        tp2 = segnale["tp2"]
        tp3 = segnale["tp3"]

        esito = None

        for j in range(i + 1, min(i + 1 + MAX_CANDELE_ATTESA, n)):
            candela = df.iloc[j]

            if direzione == "BUY":
                sl_colpito = candela["low"] <= sl
                tp1_colpito = candela["high"] >= tp1
                tp2_colpito = candela["high"] >= tp2
                tp3_colpito = candela["high"] >= tp3
            else:
                sl_colpito = candela["high"] >= sl
                tp1_colpito = candela["low"] <= tp1
                tp2_colpito = candela["low"] <= tp2
                tp3_colpito = candela["low"] <= tp3

            if sl_colpito:
                esito = "sl"  # caso peggiore: se tocca anche un TP nella stessa candela, conto comunque SL
                break
            if tp3_colpito:
                esito = "tp3"
                break
            if tp2_colpito:
                esito = "tp2"
                break
            if tp1_colpito:
                esito = "tp1"
                break

        if esito is None:
            esito = "non_concluso"

        risultati.append({"data": str(df.iloc[i]["datetime"]), "direzione": direzione, "esito": esito})
        i += 1

    return risultati


def riepilogo(risultati) -> str:
    totale = len(risultati)
    if totale == 0:
        return "Nessun segnale generato dalla logica nel periodo storico analizzato."

    conteggio = {"sl": 0, "tp1": 0, "tp2": 0, "tp3": 0, "non_concluso": 0}
    for r in risultati:
        conteggio[r["esito"]] += 1

    conclusi = totale - conteggio["non_concluso"]
    vincenti = conteggio["tp1"] + conteggio["tp2"] + conteggio["tp3"]
    r_netto = vincenti - conteggio["sl"]  # +1R per ogni vincita (chiusura ipotetica al primo TP), -1R per ogni SL

    testo = (
        f"Segnali generati nel periodo storico: <b>{totale}</b>\n"
        f"• Andati in Stop Loss: {conteggio['sl']}\n"
        f"• Arrivati almeno a TP1: {vincenti} "
        f"(TP1: {conteggio['tp1']}, TP2: {conteggio['tp2']}, TP3: {conteggio['tp3']})\n"
        f"• Non conclusi entro {MAX_CANDELE_ATTESA} candele: {conteggio['non_concluso']}\n\n"
    )
    if conclusi > 0:
        win_rate = vincenti / conclusi * 100
        testo += f"Win rate su operazioni concluse: <b>{win_rate:.1f}%</b>\n"
    testo += f"Risultato netto ipotetico: <b>{r_netto:+d}R</b> (chiudendo sempre al primo TP raggiunto)\n\n"
    testo += (
        "⚠️ Nessuno spread/commissione incluso, campione storico limitato, "
        "un segnale alla volta valutato in modo indipendente. Risultati "
        "passati non garantiscono risultati futuri."
    )
    return testo


def main():
    risultati = simula()
    testo = riepilogo(risultati)
    log.info("\n%s", testo.replace("<b>", "").replace("</b>", ""))
    send_telegram_message("📊 <b>Backtest storico XAUUSD</b>\n\n" + testo)


if __name__ == "__main__":
    main()
