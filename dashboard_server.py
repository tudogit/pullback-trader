"""
【脚本名称】dashboard_server.py
【业务场景】Martin Luk 交易系统多线程本地服务：持仓管理API + 历史日志归档 + 9 EMA 乖离率实时计算
【输入数据】长桥 API + 网页交互请求 + 本地持仓文件 (trades_data.json)
【输出结果】本地 HTTP 服务 (http://127.0.0.1:1098)
【脚本类型】例行工具 / 本地后台服务
【执行命令】python3 dashboard_server.py
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import subprocess
import threading
import urllib.parse
from datetime import datetime
import pandas as pd

PORT = 1098
BASE_DIR = "/Users/tudo/Documents/Codex/Stock-Research"
HTML_FILE = os.path.join(BASE_DIR, "martin_dashboard.html")
GEN_SCRIPT = os.path.join(BASE_DIR, "generate_web_dashboard.py")
TRADES_FILE = os.path.join(BASE_DIR, "trades_data.json")

is_refreshing = False
lock = threading.Lock()

def load_trades():
    if not os.path.exists(TRADES_FILE):
        return {"active": [], "closed": []}
    try:
        with open(TRADES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"active": [], "closed": []}


def save_trades(data):
    with open(TRADES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def get_batch_quotes(symbols):
    """
    使用 longbridge quote 批量获取多个美股最新价，毫秒级单次请求
    """
    if not symbols:
        return {}
    res_dict = {}
    lb_symbols = [f"{s}.US" if "." not in s else s for s in symbols]
    try:
        cmd = ["longbridge", "quote"] + lb_symbols + ["--format", "json"]
        env = os.environ.copy()
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=5)
        if p.returncode == 0 and p.stdout.strip():
            items = json.loads(p.stdout)
            for item in items:
                raw_sym = item.get("symbol", "")
                base_sym = raw_sym.split(".")[0].upper()
                last_price = float(item.get("last", 0) or 0)
                if last_price > 0:
                    res_dict[base_sym] = round(last_price, 2)
                    res_dict[raw_sym] = round(last_price, 2)
    except Exception as e:
        print(f"Longbridge 批量行情异常: {e}")
    return res_dict



class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        params = json.loads(body) if body else {}

        # 1. 开仓记入
        if path == "/api/trades/open":
            entry = round(float(params.get("entry_price", 0)), 2)
            stop = round(float(params.get("stop_price", 0)), 2)
            risk = round(entry - stop, 2)
            with lock:
                data = load_trades()
                new_trade = {
                    "id": f"T{int(datetime.now().timestamp()*1000)}",
                    "symbol": params.get("symbol", "").upper(),
                    "entry_price": entry,
                    "stop_price": stop,
                    "shares": int(params.get("shares", 0)),
                    "open_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "per_share_risk": risk,
                    "target_1r": round(entry + risk, 2),
                    "target_3r": round(entry + 3 * risk, 2),
                    "target_5r": round(entry + 5 * risk, 2),
                    "status": "OPEN",
                    "partial_sold": False
                }
                data["active"].append(new_trade)
                save_trades(data)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success", "trade": new_trade}).encode("utf-8"))
            return

        # 2. 减仓/平仓归档
        elif path == "/api/trades/close":
            trade_id = params.get("id")
            close_price = float(params.get("close_price", 0))
            is_partial = params.get("is_partial", False)

            with lock:
                data = load_trades()
                active = []
                for t in data["active"]:
                    if t["id"] == trade_id:
                        per_share_risk = t["per_share_risk"] if t["per_share_risk"] > 0 else 0.01
                        pnl = (close_price - t["entry_price"]) * (t["shares"] // 2 if is_partial else t["shares"])
                        r_multiple = (close_price - t["entry_price"]) / per_share_risk

                        closed_record = {
                            **t,
                            "close_price": close_price,
                            "close_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "pnl": round(pnl, 2),
                            "captured_r": round(r_multiple, 2),
                            "action": "3R减半仓" if is_partial else "全平清仓"
                        }
                        data["closed"].insert(0, closed_record)

                        if is_partial:
                            t["shares"] = t["shares"] - (t["shares"] // 2)
                            t["partial_sold"] = True
                            t["stop_price"] = t["entry_price"] # 自动平保
                            active.append(t)
                    else:
                        active.append(t)
                data["active"] = active
                save_trades(data)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success"}).encode("utf-8"))
            return

        # 3. 删除平仓日志记录 (带单条删除与一键清空)
        elif path == "/api/trades/delete_closed":
            record_id = params.get("id")
            clear_all = params.get("clear_all", False)
            with lock:
                data = load_trades()
                if clear_all:
                    data["closed"] = []
                elif record_id:
                    data["closed"] = [c for c in data.get("closed", []) if c.get("id") != record_id]
                save_trades(data)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success"}).encode("utf-8"))
            return

        # 4. 容错机制：删除未实际买入的误记持仓（如有减仓记录则坚决禁止删除）
        elif path == "/api/trades/delete_active":
            trade_id = params.get("id")
            with lock:
                data = load_trades()
                target_trade = next((t for t in data.get("active", []) if t.get("id") == trade_id), None)
                if not target_trade:
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "message": "未找到该持仓记录"}).encode("utf-8"))
                    return

                if target_trade.get("partial_sold", False):
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "message": "该股票已发生过 3R/5R 减仓，已有实际交易，不允许直接删除，必须按交易纪律正常平仓离场！"}).encode("utf-8"))
                    return

                data["active"] = [t for t in data.get("active", []) if t.get("id") != trade_id]
                save_trades(data)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success", "message": "已成功移除误记持仓"}).encode("utf-8"))
            return

        super().do_POST()

    def do_GET(self):
        global is_refreshing
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)
        
        # 获取持仓列表与历史日志 (联动长桥批量极速获取现价与9 EMA 乖离率)
        if path == "/api/trades/list":
            with lock:
                trades_data = load_trades()

            active_raw = trades_data.get("active", [])
            symbols_to_fetch = [t["symbol"] for t in active_raw]
            quotes_map = get_batch_quotes(symbols_to_fetch) if symbols_to_fetch else {}

            active_list = []
            needs_save = False
            for t in active_raw:
                sym = t["symbol"]
                base_sym = sym.split(".")[0].upper()
                curr_price = quotes_map.get(base_sym) or quotes_map.get(sym) or t.get("current_price") or t["entry_price"]
                
                # 9 EMA 缓存与懒加载计算 (日线EMA变化较平缓，优先读缓存)
                ema9 = t.get("ema9")
                if not ema9 or ema9 <= 0:
                    try:
                        import yfinance as yf
                        df = yf.Ticker(base_sym).history(period="1mo", interval="1d")
                        if not df.empty:
                            ema9 = round(float(df["Close"].ewm(span=9, adjust=False).mean().iloc[-1]), 2)
                            t["ema9"] = ema9
                            needs_save = True
                    except Exception:
                        ema9 = curr_price

                if curr_price == t["entry_price"] and base_sym not in quotes_map:
                    try:
                        import yfinance as yf
                        df = yf.Ticker(base_sym).history(period="5d", interval="1d")
                        if not df.empty:
                            curr_price = round(float(df.iloc[-1]["Close"]), 2)
                    except Exception:
                        pass

                # 计算实际浮动盈亏与 R 倍数
                pnl = (curr_price - t["entry_price"]) * t["shares"]
                pnl_pct = ((curr_price - t["entry_price"]) / t["entry_price"]) * 100
                r_mult = (curr_price - t["entry_price"]) / (t["per_share_risk"] if t["per_share_risk"] > 0 else 0.01)
                
                # 9 EMA 乖离率
                ema9_dist = ((curr_price - ema9) / ema9) * 100 if ema9 and ema9 > 0 else 0

                active_list.append({
                    **t,
                    "entry_price": round(float(t.get("entry_price", 0)), 2),
                    "stop_price": round(float(t.get("stop_price", 0)), 2),
                    "target_1r": round(float(t.get("target_1r", 0)), 2),
                    "target_3r": round(float(t.get("target_3r", 0)), 2),
                    "target_5r": round(float(t.get("target_5r", 0)), 2),
                    "current_price": round(curr_price, 2),
                    "ema9": round(ema9, 2) if ema9 else round(curr_price, 2),
                    "ema9_dist": round(ema9_dist, 2),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "r_mult": round(r_mult, 2)
                })

            if needs_save:
                with lock:
                    save_trades(trades_data)

            resp = json.dumps({"active": active_list, "closed": trades_data.get("closed", [])})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp.encode("utf-8"))
            return

        elif path == "/api/refresh":
            with lock:
                if is_refreshing:
                    resp = json.dumps({"status": "busy", "message": "已有任务正在刷新中..."}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(resp)
                    return
                is_refreshing = True

            def run_task():
                global is_refreshing
                print("🔄 正在执行长桥API选股与指标重新测算...")
                try:
                    subprocess.run(["python3", GEN_SCRIPT], check=True)
                    print("✅ 选股与看板刷新成功！")
                except Exception as e:
                    print(f"❌ 选股刷新失败: {e}")
                finally:
                    with lock:
                        is_refreshing = False

            threading.Thread(target=run_task, daemon=True).start()

            resp = json.dumps({"status": "started", "message": "刷新任务已启动"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp)
            return

        elif path == "/api/status":
            with lock:
                st = is_refreshing
            resp = json.dumps({"refreshing": st}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp)
            return

        elif path == "/api/intraday":
            sym = query.get("symbol", ["NVDA"])[0].upper()
            period = query.get("period", ["5m"])[0].lower()
            sup_low = float(query.get("sup_low", ["0"])[0])
            sup_high = float(query.get("sup_high", ["999999"])[0])
            if period not in ["1m", "5m", "15m"]:
                period = "5m"

            env = os.environ.copy()
            env["LONGBRIDGE_REGION"] = "global"
            cmd = [
                "longbridge", "kline", f"{sym}.US",
                "--period", period,
                "--count", "30",
                "--format", "json"
            ]
            bars = []
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=6)
                if res.returncode == 0 and res.stdout.strip():
                    raw_data = json.loads(res.stdout)
                    for d in raw_data[-20:]:
                        t_str = d.get("time", "")
                        t_display = t_str.split("T")[1][:5] if "T" in t_str else t_str[-8:-3]
                        bars.append({
                            "time": t_display,
                            "open": round(float(d["open"]), 2),
                            "high": round(float(d["high"]), 2),
                            "low": round(float(d["low"]), 2),
                            "close": round(float(d["close"]), 2),
                            "volume": int(float(d["volume"]))
                        })
            except Exception as e:
                print(f"Error fetching intraday: {e}")

            if not bars:
                try:
                    import yfinance as yf
                    yf_period_map = {"1m": "1d", "5m": "5d", "15m": "5d"}
                    ticker = yf.Ticker(sym)
                    df = ticker.history(period=yf_period_map.get(period, "5d"), interval=period)
                    for dt, row in df.tail(20).iterrows():
                        bars.append({
                            "time": str(dt)[11:16],
                            "open": round(float(row['Open']), 2),
                            "high": round(float(row['High']), 2),
                            "low": round(float(row['Low']), 2),
                            "close": round(float(row['Close']), 2),
                            "volume": int(row['Volume'])
                        })
                except Exception as e:
                    print(f"yfinance intraday error: {e}")

            is_breakout = False
            is_flush_reclaim = False
            trigger_price = 0.0
            latest_price = 0.0
            prev_low = 0.0
            status_text = "等待形态生成"

            if len(bars) >= 2:
                latest = bars[-1]
                prev = bars[-2]
                latest_price = latest['close']
                trigger_price = prev['high']
                prev_low = min(latest['low'], prev['low'])

                recent_min_low = min([b['low'] for b in bars[-3:]])
                if sup_low > 0:
                    flushed_below_or_near = (recent_min_low <= sup_low * 1.008)
                    reclaimed_above = (latest_price >= sup_low)
                    if flushed_below_or_near and reclaimed_above and (latest_price > trigger_price):
                        is_flush_reclaim = True

                if latest_price > trigger_price:
                    is_breakout = True
                    if is_flush_reclaim:
                        status_text = f"🌟【黄金神级扳机：诱空洗盘极速收复 (Flush & Reclaim)】最新价 ${latest_price:.2f} 探底回升，突破前K高点 ${trigger_price:.2f}！"
                    else:
                        status_text = f"🔥【买入触发】最新价 ${latest_price:.2f} 已突破前一根K线高点 ${trigger_price:.2f}，可轻仓试错！"
                else:
                    diff = trigger_price - latest_price
                    diff_pct = (diff / latest_price) * 100
                    status_text = f"👀 蓄势整理中：离前K高点扳机位 ${trigger_price:.2f} 还差 ${diff:.2f} (+{diff_pct:.2f}%)"

            resp_data = {
                "symbol": sym,
                "period": period,
                "bars": bars,
                "latest_price": latest_price,
                "trigger_price": trigger_price,
                "suggested_stop": prev_low,
                "is_breakout": is_breakout,
                "is_flush_reclaim": is_flush_reclaim,
                "status_text": status_text
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp_data).encode("utf-8"))
            return

        elif path in ["/", "/index.html"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            with open(HTML_FILE, "rb") as f:
                self.wfile.write(f.read())
            return

        super().do_GET()


def run():
    os.chdir(BASE_DIR)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), DashboardHandler)
    print(f"🚀 Martin Dashboard 服务已就绪: http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
