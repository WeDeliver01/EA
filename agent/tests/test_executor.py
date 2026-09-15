from __future__ import annotations

import pytest

from agent import mt5_client as mt5_client_module
from agent.executor import CommandExecutor
from agent.mt5_client import MT5Client
from agent.store import AgentStore
from agent.tests.fake_mt5 import FakeMT5, make_bar, make_symbol
from agent.transport import Envelope


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeMT5:
    f = FakeMT5()
    monkeypatch.setattr(mt5_client_module, "mt5", f)
    return f


@pytest.fixture
async def executor(fake: FakeMT5) -> CommandExecutor:
    client = MT5Client()
    await client.connect()
    return CommandExecutor(mt5_client=client, store=AgentStore())


def command_envelope(command_type: str, payload: dict) -> Envelope:
    return Envelope(
        v=1,
        type=f"command.{command_type}",
        id="1",
        correlation_id=None,
        ts="",
        payload=payload,
    )


class TestHandleGetBars:
    async def test_dispatches_by_count(self, fake: FakeMT5, executor: CommandExecutor) -> None:
        fake.symbols["XAUUSD"] = make_symbol()
        fake.rates = [make_bar(open=100.0, close=101.0)]

        result = await executor.handle(
            command_envelope("get_bars", {"symbol": "XAUUSD", "timeframe": "M15", "count": 10})
        )

        assert len(result["bars"]) == 1
        assert result["bars"][0]["open"] == "100.0"
        assert result["bars"][0]["close"] == "101.0"

    async def test_dispatches_by_range(self, fake: FakeMT5, executor: CommandExecutor) -> None:
        fake.symbols["XAUUSD"] = make_symbol()
        fake.rates = [make_bar()]

        result = await executor.handle(
            command_envelope(
                "get_bars",
                {
                    "symbol": "XAUUSD",
                    "timeframe": "M15",
                    "from": "2023-11-14T12:00:00+00:00",
                    "to": "2023-11-14T13:00:00+00:00",
                },
            )
        )

        assert len(result["bars"]) == 1
        kind, *_ = fake.copy_rates_calls[0]
        assert kind == "range"

    async def test_unknown_command_reports_error(self, executor: CommandExecutor) -> None:
        result = await executor.handle(command_envelope("not_a_real_command", {}))
        assert result == {"error": "UNKNOWN_COMMAND", "type": "command.not_a_real_command"}
