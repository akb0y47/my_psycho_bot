#!/usr/bin/env python3
"""
Live Polymarket Bot - Daily Noon Markets (V5c Strategy)

Trades daily "Bitcoin above $X at 12PM ET" markets.
Matches V5c backtest that generated $1,206 profit on 225 trades.

Trading window: 8am-11am ET (3 hours before settlement)
Markets: Two adjacent $2k strikes around current BTC price
Strategy: PANIC_DIP detection + volatility entries

Usage:
    python live_daily_bot.py --size 10
"""

import os
import sys
import time
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

from src import TradingBot, Config
from src.gamma_client import GammaClient
from lib import PriceTracker, PositionManager
from lib.console import Colors, log, StatusDisplay, format_pnl
import requests


def get_btc_price() -> float:
    """Get current BTC price from Binance."""
    try:
        resp = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5)
        return float(resp.json()['price'])
    except:
        return 0.0


def calculate_strikes(btc_price: float) -> tuple[int, int]:
    """Calculate adjacent $2k strikes (same as V5c backtest)."""
    lower = (int(btc_price) // 2000) * 2000
    upper = lower + 2000
    return lower, upper


def find_daily_market(strike: int, date_str: str) -> Optional[str]:
    """
    Find daily market slug for given strike and date.
    
    Format: "bitcoin-above-{strike}k-on-{month}-{day}"
    Example: "bitcoin-above-88k-on-january-21"
    """
    strike_k = strike // 1000
    gamma = GammaClient()
    slug = f"bitcoin-above-{strike_k}k-on-{date_str}"
    
    market = gamma.get_market_by_slug(slug)
    if market:
        return slug
    
    return None


def get_token_ids_for_market(slug: str) -> Optional[Dict[str, str]]:
    """Get YES/NO token IDs for a market slug."""
    gamma = GammaClient()
    market = gamma.get_market_by_slug(slug)
    
    if not market:
        return None
    
    try:
        token_ids = gamma.parse_token_ids(market)
        # Daily markets have YES/NO, map to upper/lower
        return {
            'yes': token_ids.get('yes', ''),
            'no': token_ids.get('no', '')
        }
    except:
        return None


class DailyMarketBot:
    """
    Bot for daily noon BTC markets.
    Implements V5c backtest strategy.
    """
    
    def __init__(self, position_size: float = 10.0):
        self.position_size = position_size
        
        # Initialize trading bot
        self.config = Config.from_env()
        self.bot = TradingBot(
            config=self.config,
            private_key=os.getenv("POLY_PRIVATE_KEY")
        )
        
        # Price tracking & positions
        self.price_tracker = PriceTracker(
            lookback_seconds=10,
            drop_threshold=0.30
        )
        self.position_mgr = PositionManager(
            take_profit=0.15,
            stop_loss=0.50,
            max_positions=2  # One per strike
        )
        
        # Market state
        self.lower_strike = 0
        self.upper_strike = 0
        self.lower_token_id = ""
        self.upper_token_id = ""
        self.market_slugs = {}
        
        # Stats
        self.session_start = time.time()
        self.entry_signals = {"PANIC_DIP": 0, "HIGH_VOL": 0, "MED_VOL": 0}
        self.signal_pnl = {"PANIC_DIP": 0.0, "HIGH_VOL": 0.0, "MED_VOL": 0.0}
    
    async def initialize(self) -> bool:
        """Find today's daily markets."""
        log("Initializing daily market bot...", "info")
        
        # Get current BTC price
        btc_price = get_btc_price()
        if btc_price == 0:
            log("Failed to get BTC price", "error")
            return False
        
        log(f"BTC Price: ${btc_price:,.2f}", "info")
        
        # Calculate strikes
        self.lower_strike, self.upper_strike = calculate_strikes(btc_price)
        log(f"Lower Strike: ${self.lower_strike:,}", "info")
        log(f"Upper Strike: ${self.upper_strike:,}", "info")
        
        # Check distance filter (±$900 from midpoint like V5c)
        midpoint = (self.lower_strike + self.upper_strike) / 2
        distance = abs(btc_price - midpoint)
        if distance > 900:
            log(f"BTC too far from strikes (${distance:.0f} > $900) - skipping today", "warning")
            return False
        
        # Get today's date string (format: january-22)
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%B-%d").lower()
        
        # Find markets
        log(f"Searching for markets on {date_str}...", "info")
        
        lower_slug = find_daily_market(self.lower_strike, date_str)
        upper_slug = find_daily_market(self.upper_strike, date_str)
        
        if not lower_slug or not upper_slug:
            log("Could not find daily markets", "error")
            return False
        
        # Get token IDs
        lower_tokens = get_token_ids_for_market(lower_slug)
        upper_tokens = get_token_ids_for_market(upper_slug)
        
        if not lower_tokens or not upper_tokens:
            log("Could not get token IDs", "error")
            return False
        
        # For lower strike: trade NO (bet BTC stays below)
        # For upper strike: trade YES (bet BTC goes above)
        self.lower_token_id = lower_tokens['no']
        self.upper_token_id = upper_tokens['yes']
        
        log(f"Lower NO token: {self.lower_token_id[:16]}...", "success")
        log(f"Upper YES token: {self.upper_token_id[:16]}...", "success")
        
        return True
    
    def is_trading_hours(self) -> bool:
        """Check if within 8am-11am ET trading window."""
        now = datetime.now(timezone.utc)
        hour_et = (now.hour - 5) % 24  # Convert UTC to ET (rough)
        return 8 <= hour_et < 11
    
    def calculate_exit_target(self, entry_price: float, volatility: float) -> float:
        """
        Calculate exit target (from V5c backtest optimization).
        
        Best trades: entries 0.7-4.5¢ → exits 15-30¢ (6-21x returns)
        """
        if entry_price <= 0.015:
            return min(entry_price + 0.20, 0.30)
        elif entry_price <= 0.030:
            if volatility > 0.10:
                return entry_price + 0.18
            elif volatility > 0.05:
                return entry_price + 0.15
            else:
                return entry_price + 0.12
        else:
            return entry_price + 0.12
    
    def should_enter(self, side: str, current_price: float) -> tuple[bool, str]:
        """
        Entry logic from V5c backtest.
        
        PANIC_DIP: 79% of trades, 80% of profit
        """
        if self.position_mgr.has_position(side):
            return False, "position_exists"
        
        # Price range (best trades were <6¢)
        if current_price < 0.005 or current_price > 0.06:
            return False, "price_out_of_range"
        
        # Priority 1: PANIC_DIP (30% drop in 10s)
        flash_event = self.price_tracker.detect_flash_crash(side)
        if flash_event:
            log(f"PANIC_DIP on {side}: {flash_event.old_price:.3f} -> {flash_event.new_price:.3f}", "warning")
            return True, "PANIC_DIP"
        
        # Priority 2: Volatility entries
        volatility = self.price_tracker.get_volatility(side, seconds=60)
        
        if volatility > 0.08 and current_price <= 0.06:
            return True, "HIGH_VOL"
        
        if volatility > 0.05 and current_price <= 0.04:
            return True, "MED_VOL"
        
        return False, "no_signal"
    
    async def get_current_prices(self) -> Dict[str, float]:
        """Get current market prices for both strikes."""
        prices = {}
        
        # Get lower strike NO price
        try:
            book = await self.bot.clob_client.get_order_book(self.lower_token_id)
            if book and 'asks' in book and book['asks']:
                prices['lower'] = float(book['asks'][0]['price'])
        except:
            pass
        
        # Get upper strike YES price
        try:
            book = await self.bot.clob_client.get_order_book(self.upper_token_id)
            if book and 'bids' in book and book['bids']:
                prices['upper'] = float(book['bids'][0]['price'])
        except:
            pass
        
        return prices
    
    async def place_entry_order(self, side: str, price: float, reason: str) -> bool:
        """Place entry order."""
        token_id = self.lower_token_id if side == 'lower' else self.upper_token_id
        size = self.position_size / price
        
        strike = self.lower_strike if side == 'lower' else self.upper_strike
        log(f"Entering {side.upper()} ${strike:,}: {size:.1f} @ {price:.3f} ({reason})", "trade")
        
        try:
            result = await self.bot.place_order(
                token_id=token_id,
                price=price,
                size=size,
                side="BUY"
            )
            
            if result.success:
                volatility = self.price_tracker.get_volatility(side, seconds=60)
                exit_target = self.calculate_exit_target(price, volatility)
                stop_delta = price * 0.50
                
                self.position_mgr.take_profit = exit_target - price
                self.position_mgr.stop_loss = stop_delta
                
                position = self.position_mgr.open_position(
                    side=side,
                    token_id=token_id,
                    entry_price=price,
                    size=size,
                    order_id=result.order_id
                )
                
                if position:
                    position.entry_reason = reason
                    self.entry_signals[reason] = self.entry_signals.get(reason, 0) + 1
                    log(f"Position opened: TP {exit_target:.3f} | SL {price - stop_delta:.3f}", "success")
                    return True
        except Exception as e:
            log(f"Order error: {e}", "error")
        
        return False
    
    async def check_exits(self, prices: Dict[str, float]) -> None:
        """Check and execute exits."""
        exits = self.position_mgr.check_all_exits(prices)
        
        for position, exit_type, pnl in exits:
            log(f"Exiting {position.side.upper()}: {exit_type} | PnL {pnl:+.2f}", "trade")
            
            try:
                result = await self.bot.place_order(
                    token_id=position.token_id,
                    price=prices[position.side],
                    size=position.size,
                    side="SELL"
                )
                
                if result.success:
                    entry_reason = getattr(position, 'entry_reason', 'UNKNOWN')
                    if entry_reason in self.signal_pnl:
                        self.signal_pnl[entry_reason] += pnl
                    
                    self.position_mgr.close_position(position.id, realized_pnl=pnl)
                    log(f"Closed: {format_pnl(pnl)} | Signal: {entry_reason}", "success")
            except Exception as e:
                log(f"Exit error: {e}", "error")
    
    def render_status(self, prices: Dict[str, float]) -> None:
        """Display live status."""
        display = StatusDisplay(width=80)
        
        display.add_bold_separator("=")
        display.add_header("Daily BTC Market Bot - V5c Strategy")
        display.add_bold_separator("=")
        display.add_blank()
        
        # Market info
        now = datetime.now(timezone.utc)
        noon_et = now.replace(hour=17, minute=0, second=0)  # Noon ET = 5pm UTC
        remaining = (noon_et - now).total_seconds()
        mins = int(remaining // 60)
        secs = int(remaining % 60)
        
        display.add_line(f"{Colors.BOLD}Strikes:{Colors.RESET}")
        display.add_line(f"  Lower: ${self.lower_strike:,} (NO)")
        display.add_line(f"  Upper: ${self.upper_strike:,} (YES)")
        display.add_line(f"{Colors.BOLD}Settlement:{Colors.RESET} {mins:02d}:{secs:02d} until noon ET")
        display.add_blank()
        
        # Prices
        display.add_line(f"{Colors.BOLD}Current Prices:{Colors.RESET}")
        display.add_line(f"  Lower NO:  {prices.get('lower', 0):.4f}")
        display.add_line(f"  Upper YES: {prices.get('upper', 0):.4f}")
        display.add_blank()
        
        # Positions
        positions = self.position_mgr.get_all_positions()
        display.add_line(f"{Colors.BOLD}Positions ({len(positions)}):{Colors.RESET}")
        
        if positions:
            for pos in positions:
                current = prices.get(pos.side, 0)
                pnl = pos.get_pnl(current) if current > 0 else 0
                hold_time = int(pos.get_hold_time())
                
                display.add_line(
                    f"  {pos.side.upper()}: {pos.entry_price:.3f} → {current:.3f} | "
                    f"{format_pnl(pnl)} | {hold_time}s"
                )
        else:
            display.add_line("  No positions")
        
        display.add_blank()
        
        # Stats
        stats = self.position_mgr.get_stats()
        total_pnl = self.position_mgr.get_total_pnl(prices)
        
        display.add_line(f"{Colors.BOLD}Stats:{Colors.RESET}")
        display.add_line(f"  Trades: {stats['trades_closed']} | Win Rate: {stats['win_rate']:.1f}%")
        display.add_line(f"  Total PnL: {format_pnl(total_pnl)}")
        
        if self.entry_signals:
            display.add_blank()
            for signal, count in self.entry_signals.items():
                pnl = self.signal_pnl.get(signal, 0.0)
                display.add_line(f"  {signal}: {count} | {format_pnl(pnl)}")
        
        display.add_blank()
        display.add_separator()
        display.render(in_place=True)
    
    async def run(self) -> None:
        """Main trading loop."""
        if not await self.initialize():
            return
        
        log("Bot started - monitoring markets...", "success")
        
        try:
            while True:
                # Check trading hours
                if not self.is_trading_hours():
                    log("Outside trading hours (8-11am ET)", "warning")
                    await asyncio.sleep(60)
                    continue
                
                # Get prices
                prices = await self.get_current_prices()
                
                if not prices:
                    await asyncio.sleep(5)
                    continue
                
                # Record for flash crash detection
                self.price_tracker.record_prices(prices)
                
                # Check exits
                await self.check_exits(prices)
                
                # Check entries
                for side in ['lower', 'upper']:
                    if side not in prices:
                        continue
                    
                    should_enter, reason = self.should_enter(side, prices[side])
                    if should_enter:
                        await self.place_entry_order(side, prices[side], reason)
                
                # Display status
                self.render_status(prices)
                
                await asyncio.sleep(2)
                
        except KeyboardInterrupt:
            log("\nShutting down...", "warning")
        finally:
            stats = self.position_mgr.get_stats()
            log(f"Final: {stats['trades_closed']} trades | {format_pnl(stats['total_pnl'])}", "info")


async def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Daily BTC Market Bot")
    parser.add_argument("--size", type=float, default=10.0, help="Position size")
    args = parser.parse_args()
    
    if not os.getenv("POLY_PRIVATE_KEY") or not os.getenv("POLY_SAFE_ADDRESS"):
        log("Missing credentials in .env", "error")
        sys.exit(1)
    
    bot = DailyMarketBot(position_size=args.size)
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
