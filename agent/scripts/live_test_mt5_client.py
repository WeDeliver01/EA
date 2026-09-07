"""Manual, human-supervised exercise of MT5Client against a live demo
account: place, modify, partially close, close. Not part of CI - it moves
real (demo) positions. Run from the repo root:

    python -m agent.scripts.live_test_mt5_client [SYMBOL]

Mirrors SPEC-10 Phase 5's first acceptance bullet.
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal

from agent.models import OrderSide, OrderType, PlaceOrderCommand
from agent.mt5_client import MT5Client


async def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "XAUUSD"
    client = MT5Client()
    await client.connect()
    try:
        health = await client.terminal_health()
        print(f"terminal: {health}")
        if not health.trade_allowed:
            print("Algo Trading is off in the terminal - aborting.")
            return 1

        account = await client.account_snapshot()
        print(f"account: {account}")

        spec = await client.get_symbol_spec(symbol)
        print(f"spec: {spec}")

        bid, ask, tick_time = await client.get_tick(symbol)
        print(f"tick: bid={bid} ask={ask} time={tick_time}")

        volume = spec.volume_step * 2  # 2 steps, so we can partial-close by 1 step later
        stop_loss = (ask - Decimal("5.00")).quantize(spec.point)
        take_profit = (ask + Decimal("5.00")).quantize(spec.point)

        place_cmd = PlaceOrderCommand(
            client_order_id="LIVE-TEST-PLACE-0001",
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            volume=volume,
            limit_price=None,
            stop_loss=stop_loss,
            take_profit=take_profit,
            max_slippage_points=50,
            magic=999000001,
            comment="live-test",
        )
        result = await client.place_order(place_cmd)
        print(f"place_order -> {result}")
        if result.retcode != 10009 or result.broker_position_id is None:
            print("place_order did not fill DONE - aborting.")
            return 1
        position_id = result.broker_position_id

        positions = await client.get_positions()
        print(f"positions after open: {positions}")

        new_sl = (stop_loss + Decimal("0.50")).quantize(spec.point)
        modify_result = await client.modify_position(position_id, stop_loss=new_sl)
        print(f"modify_position -> {modify_result}")

        partial_fill = await client.close_position(
            position_id, max_slippage_points=50, volume=spec.volume_step
        )
        print(f"close_position (partial) -> {partial_fill}")

        positions = await client.get_positions()
        print(f"positions after partial close: {positions}")

        final_fill = await client.close_position(position_id, max_slippage_points=50)
        print(f"close_position (final) -> {final_fill}")

        positions = await client.get_positions()
        print(f"positions after final close: {positions}")

        deals = await client.get_deals()
        recent = [d for d in deals if d.broker_position_id == position_id]
        print(f"deals for this position: {len(recent)}")
        for d in recent:
            print(f"  {d}")

        print("LIVE TEST OK")
        return 0
    finally:
        await client.shutdown()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
