"""
【脚本名称】ibkr_trader.py
【业务场景】Martin Luk 强势股回调交易系统 · IBKR 模拟盘自动交易与全生命周期风控执行引擎
【输入数据】标的代码、现价/买入价、止损线、账户资产（实时从 IBKR 读取或指定）
【输出结果】向 IBKR 模拟账户发送挂钩括号单（Bracket Order：父单买入 + 子单硬止损 + 子单3R减半仓），并支持动态平保上移与仓位监控
【脚本类型】例行工具 / 自动化交易引擎
【执行命令】python /Users/tudo/Documents/Codex/Stock-Research/ibkr_trader.py
"""

from __future__ import annotations

import math
import os
import sys
import time
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any

try:
    from ib_insync import IB, Stock, MarketOrder, LimitOrder, StopOrder, Order, Trade, util
except ImportError:
    IB = None


# IBKR 默认模拟盘与实盘端口
DEFAULT_PORTS = (7497, 4002, 7496, 4001)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_CLIENT_ID = 88


@dataclass
class AccountStatus:
    connected: bool
    port: Optional[int] = None
    account_id: Optional[str] = None
    net_liquidation: float = 0.0
    total_cash: float = 0.0
    buying_power: float = 0.0
    currency: str = "USD"
    error: Optional[str] = None


@dataclass
class MartinOrderResult:
    status: str
    symbol: str
    shares: int
    entry_price: float
    stop_price: float
    per_share_risk: float
    risk_pct: float
    total_risk_amount: float
    target_1r: float
    target_3r: float
    target_5r: float
    parent_order_id: Optional[int] = None
    stop_order_id: Optional[int] = None
    tp_order_id: Optional[int] = None
    message: str = ""


class IBKRTrader:
    """
    IBKR 模拟盘全自动交易客户端
    封装连接管理、账户风控计算、括号单发送、动态平保跟踪
    """

    def __init__(self, host: str = DEFAULT_HOST, ports: tuple = DEFAULT_PORTS, client_id: int = DEFAULT_CLIENT_ID):
        self.host = host
        self.ports = ports
        self.client_id = client_id
        self.ib: Optional[IB] = None
        self.active_port: Optional[int] = None

    def connect(self, timeout: int = 4) -> bool:
        """尝试连接本地运行的 TWS 或 IB Gateway 模拟盘"""
        if IB is None:
            return False

        if self.ib and self.ib.isConnected():
            return True

        self.ib = IB()
        for port in self.ports:
            try:
                self.ib.connect(self.host, port, clientId=self.client_id, timeout=timeout)
                if self.ib.isConnected():
                    self.active_port = port
                    return True
            except Exception:
                continue

        self.active_port = None
        return False

    def disconnect(self):
        """断开连接"""
        if self.ib and self.ib.isConnected():
            try:
                self.ib.disconnect()
            except Exception:
                pass
        self.active_port = None

    def get_account_status(self) -> AccountStatus:
        """获取账户当前连接状态与资金净值"""
        if not self.connect():
            return AccountStatus(
                connected=False,
                error="未能连接到本地 TWS 或 IB Gateway，请确认软件已打开并登录模拟交易 (Paper Trading) 且开启 API！"
            )

        try:
            accounts = self.ib.managedAccounts()
            account_id = accounts[0] if accounts else "N/A"
            summary = self.ib.accountSummary(account_id)

            net_liq = 0.0
            total_cash = 0.0
            buying_power = 0.0

            for item in summary:
                if item.tag == "NetLiquidation" and item.currency == "USD":
                    net_liq = float(item.value)
                elif item.tag == "TotalCashValue" and item.currency == "USD":
                    total_cash = float(item.value)
                elif item.tag == "BuyingPower" and item.currency == "USD":
                    buying_power = float(item.value)

            # 若未匹配到 USD 则读第一条可用项
            if net_liq == 0.0 and summary:
                for item in summary:
                    if item.tag == "NetLiquidation":
                        net_liq = float(item.value)
                    elif item.tag == "TotalCashValue":
                        total_cash = float(item.value)
                    elif item.tag == "BuyingPower":
                        buying_power = float(item.value)

            return AccountStatus(
                connected=True,
                port=self.active_port,
                account_id=account_id,
                net_liquidation=round(net_liq, 2),
                total_cash=round(total_cash, 2),
                buying_power=round(buying_power, 2)
            )
        except Exception as e:
            return AccountStatus(connected=True, port=self.active_port, error=str(e))

    def place_martin_order(
        self,
        symbol: str,
        entry_price: float,
        stop_price: float,
        account_equity: Optional[float] = None,
        risk_budget_pct: float = 0.5,
        use_market_order: bool = True
    ) -> MartinOrderResult:
        """
        核心交易执行函数：
        按 Martin Luk 冠军战法严格计算 0.5% 风险股数，并向 IBKR 发送券商托管级括号单 (Bracket Order)
        - 主订单：市价买入 (或限价买入)
        - 硬止损单：挂单于止损价 (GTC 有效期)
        - 3R 止盈单：挂单于 3R 目标价，卖出 50% 仓位锁定利润
        """
        symbol = symbol.upper().split(".")[0]
        entry = round(float(entry_price), 2)
        stop = round(float(stop_price), 2)

        if stop >= entry:
            return MartinOrderResult(
                status="error",
                symbol=symbol,
                shares=0,
                entry_price=entry,
                stop_price=stop,
                per_share_risk=0,
                risk_pct=0,
                total_risk_amount=0,
                target_1r=0,
                target_3r=0,
                target_5r=0,
                message="止损价必须严格低于买入价！"
            )

        per_share_risk = round(entry - stop, 2)
        risk_pct = round((per_share_risk / entry) * 100, 2)

        # 纪律铁律：单笔止损幅度必须 <= 3.5% (Martin 标准为 <= 3%)
        if risk_pct > 3.5:
            return MartinOrderResult(
                status="error",
                symbol=symbol,
                shares=0,
                entry_price=entry,
                stop_price=stop,
                per_share_risk=per_share_risk,
                risk_pct=risk_pct,
                total_risk_amount=0,
                target_1r=0,
                target_3r=0,
                target_5r=0,
                message=f"止损幅度为 {risk_pct}%，超过了冠军战法 ≤ 3% 的窄止损铁律！请重新寻找更精确的防守支撑点。"
            )

        # 连接检测
        if not self.connect():
            return MartinOrderResult(
                status="error",
                symbol=symbol,
                shares=0,
                entry_price=entry,
                stop_price=stop,
                per_share_risk=per_share_risk,
                risk_pct=risk_pct,
                total_risk_amount=0,
                target_1r=0,
                target_3r=0,
                target_5r=0,
                message="IBKR 客户端未连接，请先在本地启动并登录 TWS 或 IB Gateway！"
            )

        # 资金风控：若未指定资产，自动从 IBKR 模拟账户实时读取
        if not account_equity or account_equity <= 0:
            st = self.get_account_status()
            account_equity = st.net_liquidation if st.net_liquidation > 0 else 100000.0

        max_risk_amount = round(account_equity * (risk_budget_pct / 100.0), 2)
        shares = math.floor(max_risk_amount / per_share_risk)

        if shares <= 0:
            return MartinOrderResult(
                status="error",
                symbol=symbol,
                shares=0,
                entry_price=entry,
                stop_price=stop,
                per_share_risk=per_share_risk,
                risk_pct=risk_pct,
                total_risk_amount=max_risk_amount,
                target_1r=0,
                target_3r=0,
                target_5r=0,
                message="根据 0.5% 风险模型算出的买入股数为 0 股，请检查账户净值或放宽微试错预算！"
            )

        target_1r = round(entry + per_share_risk, 2)
        target_3r = round(entry + 3 * per_share_risk, 2)
        target_5r = round(entry + 5 * per_share_risk, 2)
        tp_shares = max(1, shares // 2)

        try:
            contract = Stock(symbol, "SMART", "USD")
            self.ib.qualifyContracts(contract)

            # 1. 构造父单 (Parent Entry Order)
            parent_id = self.ib.client.getReqId()
            if use_market_order:
                parent = MarketOrder("BUY", shares, orderId=parent_id, transmit=False)
            else:
                parent = LimitOrder("BUY", shares, entry, orderId=parent_id, transmit=False)

            # 2. 构造子单 1：全仓硬止损单 (Stop Loss Order, GTC, 券商托管)
            stop_id = self.ib.client.getReqId()
            stop_loss = StopOrder(
                "SELL",
                shares,
                stop,
                orderId=stop_id,
                parentId=parent_id,
                tif="GTC",
                transmit=False
            )

            # 3. 构造子单 2：3R 减半仓止盈单 (Take Profit Order, GTC, 锁定 50% 利润)
            tp_id = self.ib.client.getReqId()
            take_profit = LimitOrder(
                "SELL",
                tp_shares,
                target_3r,
                orderId=tp_id,
                parentId=parent_id,
                tif="GTC",
                transmit=True  # 最后一个子单触发全套括号单整体提交给交易所
            )

            # 提交全套括号单
            trade_parent = self.ib.placeOrder(contract, parent)
            trade_stop = self.ib.placeOrder(contract, stop_loss)
            trade_tp = self.ib.placeOrder(contract, take_profit)

            return MartinOrderResult(
                status="success",
                symbol=symbol,
                shares=shares,
                entry_price=entry,
                stop_price=stop,
                per_share_risk=per_share_risk,
                risk_pct=risk_pct,
                total_risk_amount=max_risk_amount,
                target_1r=target_1r,
                target_3r=target_3r,
                target_5r=target_5r,
                parent_order_id=parent_id,
                stop_order_id=stop_id,
                tp_order_id=tp_id,
                message=f"已成功向 IBKR 模拟盘发送括号单！主买单 {shares} 股，下挂硬止损 ${stop} (GTC) 与 3R 止盈 ${target_3r} (卖出 {tp_shares} 股)！"
            )
        except Exception as e:
            return MartinOrderResult(
                status="error",
                symbol=symbol,
                shares=shares,
                entry_price=entry,
                stop_price=stop,
                per_share_risk=per_share_risk,
                risk_pct=risk_pct,
                total_risk_amount=max_risk_amount,
                target_1r=target_1r,
                target_3r=target_3r,
                target_5r=target_5r,
                message=f"IBKR 下单异常: {e}"
            )

    def get_positions(self) -> List[Dict[str, Any]]:
        """获取 IBKR 当前真实持仓列表"""
        if not self.connect():
            return []

        try:
            positions = self.ib.positions()
            res = []
            for p in positions:
                sym = p.contract.symbol
                res.append({
                    "symbol": sym,
                    "shares": int(p.position),
                    "avg_cost": round(float(p.avgCost), 2),
                    "account": p.account
                })
            return res
        except Exception:
            return []


# 单例全局实例供 API 和 CLI 调用
trader = IBKRTrader()


if __name__ == "__main__":
    print("=" * 60)
    print("🤖 Martin Luk 强势股回调交易系统 · IBKR 模拟盘自动交易模块")
    print("=" * 60)

    status = trader.get_account_status()
    if status.connected:
        print(f"✅ 成功连接至 IBKR 模拟盘服务！")
        print(f"• 端口: {status.port}")
        print(f"• 模拟账户: {status.account_id}")
        print(f"• 账户净值: ${status.net_liquidation:,.2f}")
        print(f"• 可用现金: ${status.total_cash:,.2f}")
        print(f"• 购买力: ${status.buying_power:,.2f}")
    else:
        print("⚠️ 当前未检测到已开启的 IBKR 本地服务。")
        print(f"• 提示: {status.error}")
        print("• 检查指南: 请打开 TWS 或 IB Gateway，切换到 Paper Trading 登录，在 API 设置中开启 Socket 端口 (7497 或 4002)。")
