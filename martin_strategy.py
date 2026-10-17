"""
【脚本名称】martin_strategy.py
【业务场景】Martin Luk 2025年USIC冠军策略核心量化引擎与风控算法
【输入数据】K线历史数据（支持 Longbridge OpenAPI / yfinance 自动降级双通道）
【输出结果】强势股初筛、AVWAP与均线支撑区共振、日内突破触发、仓位与R期望风控计算
【脚本类型】例行工具 / 核心策略库
【执行命令】python /Users/tudo/Documents/Codex/Stock-Research/martin_strategy.py
"""

from __future__ import annotations

import json
import math
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd
import numpy as np


@dataclass
class PullbackSignal:
    symbol: str
    current_price: float
    status: str  # "IN_RETTEST_ZONE", "BREAKOUT_TRIGGERED", "WATCHING", "BROKEN"
    ema9: float
    ema21: float
    ema50: float
    peak_date: str
    peak_high: float
    avwap: float
    support_zone: Tuple[float, float]
    dist_to_ema21_pct: float
    dist_to_avwap_pct: float
    is_vcp_higher_lows: bool
    reasons: List[str]


@dataclass
class PositionSizingResult:
    symbol: str
    account_capital: float
    risk_budget_pct: float
    entry_price: float
    stop_price: float
    stop_dist_pct: float
    per_share_risk: float  # 1R
    max_risk_amount: float
    shares: int
    position_value: float
    position_pct: float
    target_3r: float
    target_5r: float
    is_risk_valid: bool
    warning: Optional[str] = None


class DataFeed:
    """双通道数据接入层：优先长桥 CLI，遇到 Token 过期自动无缝降级到 yfinance"""

    @staticmethod
    def is_longbridge_available() -> bool:
        try:
            res = subprocess.run(
                ["longbridge", "check"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False
            )
            return "token" in res.stdout and "OK" in res.stdout
        except Exception:
            return False

    @classmethod
    def get_daily_bars(cls, symbol: str, count: int = 120) -> pd.DataFrame:
        clean_symbol = symbol.replace(".US", "").replace(".HK", "").upper()
        # 尝试 Longbridge
        if cls.is_longbridge_available():
            lb_symbol = f"{clean_symbol}.US" if "." not in symbol else symbol
            try:
                res = subprocess.run(
                    ["longbridge", "kline", lb_symbol, "--period", "day", "--count", str(count), "--format", "json"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False
                )
                if res.returncode == 0 and res.stdout.strip():
                    data = json.loads(res.stdout)
                    df = pd.DataFrame(data)
                    df.rename(columns={
                        "timestamp": "Date",
                        "open": "Open",
                        "high": "High",
                        "low": "Low",
                        "close": "Close",
                        "volume": "Volume"
                    }, inplace=True)
                    df['Close'] = df['Close'].astype(float)
                    df['High'] = df['High'].astype(float)
                    df['Low'] = df['Low'].astype(float)
                    df['Open'] = df['Open'].astype(float)
                    df['Volume'] = df['Volume'].astype(float)
                    return df
            except Exception:
                pass

        # 降级备用：yfinance
        import yfinance as yf
        ticker = yf.Ticker(clean_symbol)
        df = ticker.history(period="6mo", interval="1d")
        if df.empty:
            raise ValueError(f"无法获取标的 {clean_symbol} 的历史数据")
        return df


class MartinStrategyEngine:
    """Martin Luk 策略量化计算模型"""

    @staticmethod
    def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df['EMA9'] = df['Close'].ewm(span=9, adjust=False).mean()
        df['EMA21'] = df['Close'].ewm(span=21, adjust=False).mean()
        df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()

        # 计算近 20 日局部高点作为 AVWAP 锚点
        lookback = min(30, len(df))
        subset = df.iloc[-lookback:]
        peak_idx = subset['High'].idxmax()
        peak_loc = df.index.get_loc(peak_idx)

        # 锚定成交量加权均价 (AVWAP)
        avwap_df = df.iloc[peak_loc:].copy()
        typical_price = (avwap_df['High'] + avwap_df['Low'] + avwap_df['Close']) / 3.0
        cum_vp = (typical_price * avwap_df['Volume']).cumsum()
        cum_vol = avwap_df['Volume'].cumsum()
        avwap = cum_vp / cum_vol
        
        # 补齐整列
        df['AVWAP'] = np.nan
        df.iloc[peak_loc:, df.columns.get_loc('AVWAP')] = avwap.values
        df.attrs['peak_date'] = str(peak_idx)[:10]
        df.attrs['peak_high'] = float(df.loc[peak_idx, 'High'])
        return df

    @classmethod
    def analyze_pullback(cls, symbol: str, df: pd.DataFrame) -> PullbackSignal:
        df = cls.calculate_indicators(df)
        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else latest

        current_price = float(latest['Close'])
        ema9 = float(latest['EMA9'])
        ema21 = float(latest['EMA21'])
        ema50 = float(latest['EMA50'])
        avwap = float(latest['AVWAP'])
        peak_date = df.attrs.get('peak_date', 'N/A')
        peak_high = df.attrs.get('peak_high', 0.0)

        # 支撑带：以 AVWAP 和 21 EMA 为核心参考
        sup_low = min(ema21, avwap) * 0.985
        sup_high = max(ema21, avwap) * 1.015

        dist_to_ema21 = (current_price - ema21) / ema21 * 100
        dist_to_avwap = (current_price - avwap) / avwap * 100

        # VCP 形态检测：近5日低点是否高于前15日低点（Higher Lows）
        recent_low = df.iloc[-5:]['Low'].min()
        prior_low = df.iloc[-20:-5]['Low'].min() if len(df) >= 20 else recent_low
        is_higher_low = recent_low >= prior_low

        reasons = []
        if current_price > ema50:
            reasons.append("股价保持在 50 EMA 强弱线上方")
        else:
            reasons.append("股价跌破 50 EMA，大周期转弱")

        if ema9 >= ema21 * 0.995:
            reasons.append("EMA 9 与 EMA 21 保持多头或收敛走平")

        # 状态判定
        in_zone = (current_price >= sup_low) and (current_price <= sup_high)
        if current_price < sup_low * 0.98:
            status = "BROKEN"
            reasons.append("跌穿关键支撑带，失去试错胜率")
        elif in_zone:
            # 检查是否已有回升拐头（突破前一根K线高点）
            if current_price > prev['High']:
                status = "BREAKOUT_TRIGGERED"
                reasons.append("在支撑区域出现回升，突破前K线最高价，触发入场信号！")
            else:
                status = "IN_RETTEST_ZONE"
                reasons.append("价格正处于 21 EMA 与高点 AVWAP 支撑带，等待反弹买点信号")
        else:
            status = "WATCHING"
            reasons.append("尚未回踩至支撑密集带，继续观察")

        return PullbackSignal(
            symbol=symbol,
            current_price=current_price,
            status=status,
            ema9=ema9,
            ema21=ema21,
            ema50=ema50,
            peak_date=peak_date,
            peak_high=peak_high,
            avwap=avwap,
            support_zone=(sup_low, sup_high),
            dist_to_ema21_pct=dist_to_ema21,
            dist_to_avwap_pct=dist_to_avwap,
            is_vcp_higher_lows=is_higher_low,
            reasons=reasons
        )

    @staticmethod
    def calculate_position(
        symbol: str,
        account_capital: float,
        entry_price: float,
        stop_price: float,
        risk_budget_pct: float = 0.5
    ) -> PositionSizingResult:
        """
        第三步：先定止损与仓位，再下单执行
        - 止损建议在 2.5% ~ 3.0%
        - 账户单笔风险 0.5%
        - 输出 3R / 5R 预期止盈
        """
        if stop_price >= entry_price:
            raise ValueError("止损价必须严格低于买入价")

        per_share_risk = entry_price - stop_price
        stop_dist_pct = (per_share_risk / entry_price) * 100.0

        max_risk_amount = account_capital * (risk_budget_pct / 100.0)
        shares = int(math.floor(max_risk_amount / per_share_risk))
        position_value = shares * entry_price
        position_pct = (position_value / account_capital) * 100.0

        target_3r = entry_price + (3.0 * per_share_risk)
        target_5r = entry_price + (5.0 * per_share_risk)

        warning = None
        is_risk_valid = True
        if stop_dist_pct > 3.0:
            is_risk_valid = False
            warning = f"止损距离为 {stop_dist_pct:.2f}%，超出 Martin 建议的 2.5%~3.0% 舒适试错带宽！"

        return PositionSizingResult(
            symbol=symbol,
            account_capital=account_capital,
            risk_budget_pct=risk_budget_pct,
            entry_price=entry_price,
            stop_price=stop_price,
            stop_dist_pct=stop_dist_pct,
            per_share_risk=per_share_risk,
            max_risk_amount=max_risk_amount,
            shares=shares,
            position_value=position_value,
            position_pct=position_pct,
            target_3r=target_3r,
            target_5r=target_5r,
            is_risk_valid=is_risk_valid,
            warning=warning
        )


if __name__ == "__main__":
    print("Testing Martin Strategy Engine with NVDA...")
    feed = DataFeed()
    print("Longbridge available:", feed.is_longbridge_available())
    df = feed.get_daily_bars("NVDA", count=100)
    sig = MartinStrategyEngine.analyze_pullback("NVDA", df)
    print(f"Signal Status: {sig.status}")
    print(f"Price: {sig.current_price:.2f}, EMA21: {sig.ema21:.2f}, AVWAP: {sig.avwap:.2f}")
    print("Reasons:", sig.reasons)
    
    pos = MartinStrategyEngine.calculate_position("NVDA", 10000, 215.0, 209.0)
    print(f"Buy shares: {pos.shares}, Risk 1R: ${pos.per_share_risk:.2f}, 3R: ${pos.target_3r:.2f}, 5R: ${pos.target_5r:.2f}")
