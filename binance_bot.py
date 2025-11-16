import time
import json
import threading
import sys
from decimal import Decimal, getcontext
from datetime import datetime
from collections import deque
from websocket import WebSocketApp
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException

# ===== USER SETTINGS (UPDATE THESE!) =====
# API Keys must be the long HMAC text strings from Binance
API_KEY = "POgvmlFM0wWHQJKd4yVrdPfGbYsSrjHoNUh3cjCZmA7oKimFsd6P8xuB3sqB0hZi"
API_SECRET = "cpGAtcThiPQxaMx6Hs63CGbV91OflupvCPm4gjAfoI5p0sdX8leJH2XZaQp6XC7v"

TESTNET = False       # False = Mainnet (real funds).
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]    # Trading pairs
QUOTE_USDT_AMOUNT = 12.0  # USDT amount per trade (Adjust to be above $10 minimum)
LOG_FILE = "bot_log.txt"
MIN_TIME_BETWEEN_ORDERS = 1.0  # Seconds to wait between actions

# ===== DECIMAL PRECISION =====
getcontext().prec = 18

# ===== INIT BINANCE CLIENT (Global Client Instance) =====
# Increased timeout to 30s to handle slow connections
client = Client(API_KEY, API_SECRET, {'timeout': 30})
if TESTNET:
    client.API_URL = 'https://testnet.binance.vision/api'


# ===== UTILITIES =====
def now_str():
    """Returns current time string for logging."""
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def log(msg):
    """Prints message to console and logs it to file."""
    line = f"[{now_str()}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"Failed to write to log file: {e}")

def round_step_down(qty: Decimal, step: Decimal) -> Decimal:
    """Rounds down the quantity to the nearest step size (Binance LOT_SIZE rule)."""
    if step is None or step == 0:
        return qty
    return (qty // step) * step


# =========================================================
# === CORE LOGIC FOR A SINGLE SYMBOL (Encapsulated) =======
# =========================================================

def run_symbol_bot(SYMBOL):
    """
    Main function to run the bot logic for a single trading pair.
    """
    # 1. STATE VARIABLES
    in_position = False
    bought_qty = Decimal('0')
    last_action_time = 0
    closed_klines = deque(maxlen=10)
    
    # 2. SYMBOL FILTERS
    try:
        symbol_info = client.get_symbol_info(SYMBOL)
    except (BinanceAPIException, BinanceRequestException) as e:
        log(f"Error for {SYMBOL}: Failed to get symbol info (API/Request Error): {e}")
        return
    except Exception as e:
        log(f"Error for {SYMBOL}: Unexpected error getting symbol info: {e}")
        return
    
    if not symbol_info:
        log(f"Error for {SYMBOL}: Symbol not found.")
        return

    step_size = None
    tick_size = None
    min_notional = Decimal('10.0') 
    
    for f in symbol_info.get('filters', []):
        if f['filterType'] == 'LOT_SIZE':
            step_size = Decimal(f.get('stepSize', '0.001'))
        if f['filterType'] == 'PRICE_FILTER':
            tick_size = Decimal(f.get('tickSize', '0.000001'))
        if f['filterType'] == 'MIN_NOTIONAL':
            min_notional = Decimal(f.get('minNotional', '10.0')) 
            
    if min_notional < Decimal('10.0'):
        min_notional = Decimal('10.0') 

    log(f"[{SYMBOL}] Filters loaded: Step={step_size}, Tick={tick_size}, MinNotional={min_notional}")


    # 3. MARKET ORDERS (Live Orders)
    def market_buy_by_usdt(symbol, usdt_amount):
        nonlocal bought_qty, in_position 
        try:
            ticker = client.get_symbol_ticker(symbol=symbol)
            price = Decimal(ticker['price'])
            
            # Calculate quantity based on USDD amount
            qty = (Decimal(str(usdt_amount)) / Decimal(str(price)))
            
            # Apply LOT_SIZE filter (round down quantity)
            qty_rounded = round_step_down(qty, step_size) 
            
            if qty_rounded <= 0:
                log(f"[{symbol}] Buy quantity too small after rounding. Original Qty: {qty}")
                return None

            notional = qty_rounded * price
            if notional < min_notional:
                log(f"[{symbol}] Order notional {notional.quantize(tick_size)} < min_notional {min_notional}.")
                return None

            log(f"[{symbol}] Placing LIVE market BUY: {qty_rounded} {symbol.replace('USDT', '')} @ approx {price}")
            
            # === LIVE ORDER EXECUTION ===
            order = client.order_market_buy(symbol=symbol, quantity=float(qty_rounded))
            
            # Update state only upon successful execution
            executed_qty = Decimal(order['executedQty'])
            bought_qty = executed_qty
            in_position = True

            log(f"[{symbol}] Market BUY executed: {executed_qty} {symbol.replace('USDT', '')}")
            return order

        except BinanceAPIException as e:
            log(f"[{symbol}] Market BUY FAILED (API Error): {e}")
            return None
        except Exception as e:
            log(f"[{symbol}] Market BUY FAILED (General Error): {e}")
            return None


    def market_sell_all(symbol, quantity):
        nonlocal bought_qty, in_position
        try:
            if quantity <= 0:
                log(f"[{symbol}] No quantity to sell.")
                return None
            
            # Apply LOT_SIZE filter (round down quantity)
            qty_rounded = round_step_down(quantity, step_size)
                
            if qty_rounded <= 0:
                log(f"[{symbol}] Sell quantity too small after rounding. Original Qty: {quantity}")
                return None
                
            log(f"[{symbol}] Placing LIVE market SELL: {qty_rounded} {symbol.replace('USDT', '')}")
            
            # === LIVE ORDER EXECUTION ===
            order = client.order_market_sell(symbol=symbol, quantity=float(qty_rounded))

            # Update state only upon successful execution
            bought_qty = Decimal('0')
            in_position = False

            log(f"[{symbol}] SELL executed: qty={order.get('executedQty', qty_rounded)}")
            return order

        except BinanceAPIException as e:
            log(f"[{symbol}] Market SELL FAILED (API Error): {e}")
            return None
        except Exception as e:
            log(f"[{symbol}] Market SELL FAILED (General Error): {e}")
            return None


    # 4. STRATEGY (3-Candle Strike)
    def analyze_and_maybe_trade():
        nonlocal in_position, last_action_time, bought_qty
        
        # Need at least 3 closed candles
        if len(closed_klines) < 3:
            return
            
        if time.time() - last_action_time < MIN_TIME_BETWEEN_ORDERS:
            return
            
        last3 = list(closed_klines)[-3:]
        
        # Check for 3 consecutive green candles (BUY)
        greens = all(k['close'] > k['open'] for k in last3)
        # Check for 3 consecutive red candles (SELL)
        reds = all(k['close'] < k['open'] for k in last3)
        
        # BUY signal: 3 Greens and NOT in position
        if greens and not in_position:
            log(f"[{SYMBOL}] Detected 3 consecutive green candles — BUY signal.")
            order = market_buy_by_usdt(SYMBOL, QUOTE_USDT_AMOUNT)
            if order:
                last_action_time = time.time()
                
        # SELL signal: 3 Reds and IS in position
        elif reds and in_position:
            log(f"[{SYMBOL}] Detected 3 consecutive red candles — SELL signal.")
            if bought_qty > 0:
                order = market_sell_all(SYMBOL, bought_qty)
                if order:
                    last_action_time = time.time()
            else:
                log(f"[{SYMBOL}] WARNING: State error: In position but qty=0, resetting state.")
                in_position = False

    # 5. WEBSOCKET
    stream = f"{SYMBOL.lower()}@kline_1m"
    ws_url = f"wss://stream.binance.com:9443/ws/{stream}"

    def on_message(ws, message):
        try:
            data = json.loads(message)
            k = data.get('k', {})
            is_closed = k.get('x', False)
            
            if is_closed:
                # Extract prices as Decimal
                open_price = Decimal(k.get('o', '0'))
                close_price = Decimal(k.get('c', '0'))
                
                closed_klines.append({'open': open_price, 'close': close_price})
                log(f"[{SYMBOL}] Candle closed — open={open_price}, close={close_price}")
                
                # Run the strategy logic
                analyze_and_maybe_trade()
        except Exception as e:
            log(f"[{SYMBOL}] WebSocket message error: {e}. Message: {message[:100]}...")

    def on_open(ws):
        log(f"[{SYMBOL}] WebSocket connected — Monitoring 1m candles.")

    def on_error(ws, error):
        log(f"[{SYMBOL}] WebSocket error: {error}")

    def on_close(ws, code, reason):
        log(f"[{SYMBOL}] WebSocket closed: code={code}, reason={reason}. Attempting to reconnect in 5s...")
        time.sleep(5)
        # Simple reconnection logic
        ws.run_forever()

    # Start the WebSocket connection
    ws = WebSocketApp(ws_url, on_open=on_open, on_message=on_message,
                      on_error=on_error, on_close=on_close)
    
    while True:
        try:
            ws.run_forever()
        except Exception as e:
            log(f"[{SYMBOL}] Failed to reconnect WebSocket: {e}. Retrying in 10s...")
            time.sleep(10)


# ===== MAIN (Use threading) =====
if __name__ == '__main__':
    if 'YOUR_NEW_API_KEY_HERE' in API_KEY:
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("!! 🛑 ERROR: Please replace the placeholder API Keys and SECRET !!")
        print("!! The bot will not run with the default placeholder values.            !!")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        sys.exit(1)

    log("Starting multi-symbol bot - WARNING: Mainnet (real money) is enabled.")
    log(f"Trading pairs: {', '.join(SYMBOLS)} - USDT per trade: {QUOTE_USDT_AMOUNT}")
    
    threads = []
    
    # Loop to start a thread for each symbol
    for symbol in SYMBOLS:
        log(f"Initializing bot for: {symbol}")
        t = threading.Thread(target=run_symbol_bot, args=(symbol,), daemon=True)
        threads.append(t)
        t.start()
        
    log(f"All {len(SYMBOLS)} bots are running in separate threads.")
    
    try:
        # Keep the main thread alive
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("Bot stopped by user (KeyboardInterrupt). Exiting.")
        sys.exit(0)
