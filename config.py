"""
Configurazione del bot XAUUSD.

NON caricare mai questo file su GitHub pubblico o condividerlo con le
chiavi API compilate: chiunque le legga puo' usarle al posto tuo.
Meglio ancora: in produzione, imposta questi valori come variabili
d'ambiente invece di scriverli qui (vedi commento in fondo al file).
"""

import os

# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "INSERISCI_QUI_IL_TUO_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "INSERISCI_QUI_IL_TUO_CHAT_ID")

# ---------------------------------------------------------------------------
# FONTE DATI PREZZI (Twelve Data - registrazione gratuita su twelvedata.com)
# ---------------------------------------------------------------------------
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "INSERISCI_QUI_LA_TUA_API_KEY")

# ---------------------------------------------------------------------------
# FASCIA ORARIA ATTIVA (ora italiana, formato 24h)
# ---------------------------------------------------------------------------
ORARIO_INIZIO = 9   # il bot inizia a controllare il mercato dalle 9:00
ORARIO_FINE = 23    # e smette alle 23:00

# ---------------------------------------------------------------------------
# TIMEFRAME E FREQUENZA DI CONTROLLO
# ---------------------------------------------------------------------------
TIMEFRAME = "15min"              # candele a 15 minuti (opzioni Twelve Data: 1min,5min,15min,30min,1h,4h,1day)
CANDELE_STORICO = 200            # quante candele scaricare ad ogni controllo
INTERVALLO_CONTROLLO_SECONDI = 300  # ogni quanto ricontrollare il mercato (300 = 5 minuti)
COOLDOWN_MINUTI = 120            # non rimandare lo stesso tipo di segnale prima di X minuti
SIGNAL_VALIDITY_MINUTES = 180     # dopo quanto un segnale e' considerato "scaduto" per il calcolo del lotto

# ---------------------------------------------------------------------------
# PARAMETRI INDICATORI (puoi modificarli per rendere il bot piu'/meno permissivo)
# ---------------------------------------------------------------------------
RSI_PERIOD = 14
RSI_OVERSOLD = 30     # RSI sotto questa soglia = possibile ipervenduto (occhio a BUY)
RSI_OVERBOUGHT = 70   # RSI sopra questa soglia = possibile ipercomprato (occhio a SELL)

SMA_FAST = 20         # media mobile veloce (numero di candele)
SMA_SLOW = 50         # media mobile lenta

SR_LOOKBACK = 40       # quante candele guardare indietro per trovare supporto/resistenza
SR_BUFFER = 3.0        # distanza in $ entro cui il prezzo e' considerato "vicino" a supp/res
SL_BUFFER = 2.0        # $ di margine extra oltre il supporto/resistenza per lo Stop Loss

# ---------------------------------------------------------------------------
# NOTA sulle variabili d'ambiente (metodo consigliato per le chiavi)
# ---------------------------------------------------------------------------
# Invece di scrivere le chiavi sopra in chiaro, su Linux/macOS puoi fare:
#
#   export TELEGRAM_BOT_TOKEN="123456:ABCDEF..."
#   export TELEGRAM_CHAT_ID="987654321"
#   export TWELVE_DATA_API_KEY="xxxxxxxxxxxx"
#   python bot.py
#
# Oppure creare un file .env e usare una libreria come python-dotenv.
