#!/usr/bin/env python3
"""Sim runner with real market data from SharedSignals DB."""
import sys, json, os, sqlite3
sys.path.insert(0, "/opt/investment/tradingagent")
sys.path.insert(0, "/opt/investment/SharedSignals")

market = os.environ.get("SIM_MARKET", "crypto")
DB = "/opt/investment/MarketGraphRuntime/read_model/marketdata.sqlite"
configs = {
    "crypto": {"market_key": "Crypto", "table": "market_bars_daily", "sim_mod": "Crypto.simulator", "sim_cls": "CryptoSimulator", "cfg_mod": "Crypto.common", "cfg_cls": "CryptoConfig"},
    "pm": {"market_key": "PM", "table": "market_pm_prices", "sim_mod": "PM.simulator", "sim_cls": "PMSimulator", "cfg_mod": "PM.common", "cfg_cls": "PMConfig"},
    "us": {"market_key": "US", "table": "market_bars_daily", "sim_mod": "US.simulator", "sim_cls": "USSimulator", "cfg_mod": "US.common", "cfg_cls": "USConfig"},
}
cfg = configs.get(market, configs["crypto"])
tbl = cfg["table"]
mk = cfg["market_key"]

conn = sqlite3.connect(DB)
if market == "pm":
    sql = "SELECT market_id as symbol, price, price_time as trade_date FROM {} ORDER BY price_time DESC LIMIT 10".format(tbl)
    rows = conn.execute(sql).fetchall()
else:
    sql = "SELECT symbol, close as price, trade_date FROM {} WHERE market=? ORDER BY trade_date DESC LIMIT 10".format(tbl)
    rows = conn.execute(sql, [mk]).fetchall()
conn.close()

signals = []
for r in rows:
    signals.append({"symbol": str(r[0]), "price": float(r[1] or 0), "trade_date": str(r[2]), "side": "buy", "quantity": 1, "market": market, "capital_layer": "simulated"})

if not signals:
    print(json.dumps({"market": market, "status": "no_data", "signals": 0}))
    sys.exit(0)

sim_mod = __import__(cfg["sim_mod"], fromlist=[cfg["sim_cls"]])
cfg_mod = __import__(cfg["cfg_mod"], fromlist=[cfg["cfg_cls"]])
config = getattr(cfg_mod, cfg["cfg_cls"])()
simulator = getattr(sim_mod, cfg["sim_cls"])(config=config)

from shared.markets.style_runner import StyleRunner
from datetime import date

runner = StyleRunner(market, simulator)
result = runner.run(signals, date=str(date.today()))

print(json.dumps({"market": market, "status": "ok", "signals": len(signals), "data_rows": len(rows), "timestamp": str(date.today())}, default=str))
