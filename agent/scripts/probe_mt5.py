"""Standalone connectivity probe, run manually: confirms the MetaTrader5
package can talk to the local terminal before any of agent/ is trusted.

    python agent/scripts/probe_mt5.py [SYMBOL]

Not part of the agent service; delete-safe once mt5_client.py has coverage.
"""

from __future__ import annotations

import sys

import MetaTrader5 as mt5


def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "XAUUSD"

    if not mt5.initialize():
        print(f"initialize() failed: {mt5.last_error()}")
        return 1
    print("initialize() OK")

    try:
        terminal = mt5.terminal_info()
        if terminal is None:
            print(f"terminal_info() failed: {mt5.last_error()}")
            return 1
        print(
            f"terminal: connected={terminal.connected} "
            f"trade_allowed={terminal.trade_allowed} build={terminal.build} "
            f"path={terminal.path}"
        )

        account = mt5.account_info()
        if account is None:
            print(f"account_info() failed: {mt5.last_error()}")
            return 1
        print(
            f"account: login={account.login} server={account.server} "
            f"currency={account.currency} leverage={account.leverage} "
            f"balance={account.balance} equity={account.equity} "
            f"trade_mode={account.trade_mode} trade_allowed={account.trade_allowed}"
        )

        if not mt5.symbol_select(symbol, True):
            print(f"symbol_select({symbol}) failed: {mt5.last_error()}")
            return 1

        info = mt5.symbol_info(symbol)
        if info is None:
            print(f"symbol_info({symbol}) failed: {mt5.last_error()}")
            return 1
        print(
            f"symbol_info: digits={info.digits} point={info.point} "
            f"trade_tick_size={info.trade_tick_size} "
            f"trade_tick_value={info.trade_tick_value} "
            f"trade_contract_size={info.trade_contract_size} "
            f"volume_min={info.volume_min} volume_max={info.volume_max} "
            f"volume_step={info.volume_step} "
            f"trade_stops_level={info.trade_stops_level} "
            f"trade_freeze_level={info.trade_freeze_level} "
            f"filling_mode={info.filling_mode} "
            f"currency_profit={info.currency_profit} "
            f"currency_margin={info.currency_margin}"
        )

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            print(f"symbol_info_tick({symbol}) failed: {mt5.last_error()}")
            return 1
        print(f"tick: bid={tick.bid} ask={tick.ask} time={tick.time}")

        positions = mt5.positions_get()
        print(f"open positions: {len(positions) if positions is not None else 0}")

        print("PROBE OK")
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
