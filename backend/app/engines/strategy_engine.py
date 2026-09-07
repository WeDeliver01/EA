"""SPEC-05 §1-2: StrategyEngine, the single composition root.

`evaluate()` is pure: no I/O, no clock, no randomness, no mutation of
`state`, deterministic in `config` and `state` alone (P7). It is called
identically by the live strategy worker, the paper trader and the
backtester (SPEC-07 §1) - the only thing that differs between modes is how
`MarketState` gets built and what happens to the `Decision` afterwards.

`engine_duration_ms` is always 0 here: measuring it needs the wall clock,
which this function may not read (P7 / the purity grep in SPEC-05 §1). The
impure caller times the call from outside and attaches the real duration
with `dataclasses.replace(decision, engine_duration_ms=elapsed_ms)` before
persisting it - see docs/adr/0001-mvp-scope.md.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from app.domain.market.market_state import MarketState
from app.domain.strategy.decision import Decision, TakeProfit
from app.domain.strategy.enums import DecisionOutcome
from app.domain.strategy.evidence import Evidence
from app.domain.strategy.gate_result import GateResult
from app.engines.config import StrategyConfig
from app.engines.confluence.engine import ConfluenceEngine
from app.engines.context.engine import ContextEngine
from app.engines.evidence.collector import EvidenceCollector
from app.engines.gates.strategy_gates import evaluate_post_construction, evaluate_pre_construction
from app.engines.liquidity.engine import LiquidityEngine
from app.engines.manipulation.engine import ManipulationEngine
from app.engines.narrative.generator import generate as generate_narrative
from app.engines.setup.detector import SetupDetector
from app.engines.structure.engine import StructureEngine
from app.engines.trade_constructor.constructor import TradeConstructor


class StrategyEngine:
    def __init__(self, config: StrategyConfig, *, strategy_version_id: UUID) -> None:
        self._config = config
        self._strategy_version_id = strategy_version_id
        self._context_engine = ContextEngine(config.context)
        self._structure_engine = StructureEngine(config.structure)
        self._liquidity_engine = LiquidityEngine(config.liquidity)
        self._manipulation_engine = ManipulationEngine(config.manipulation)
        self._setup_detector = SetupDetector(config.setups)
        self._evidence_collector = EvidenceCollector(dict(config.evidence_weights))
        self._confluence_engine = ConfluenceEngine(config.confluence)
        self._trade_constructor = TradeConstructor(config.trade_construction)

    def evaluate(self, state: MarketState) -> Decision:
        context = self._context_engine.evaluate(state)
        structure = self._structure_engine.evaluate(state)
        liquidity = self._liquidity_engine.evaluate(state, structure)
        primary_atr = context.atr.get(state.primary_tf, Decimal(0))
        manipulation = self._manipulation_engine.evaluate(state, liquidity, atr=primary_atr)
        setup = self._setup_detector.evaluate(state, structure, atr=primary_atr)

        if setup is None:
            evidence: tuple[Evidence, ...] = ()
            confluence_score = Decimal(0)
            confluence_band = "WAIT"
        else:
            evidence = self._evidence_collector.evaluate(
                state, context, structure, manipulation, setup
            )
            confluence_score, confluence_band = self._confluence_engine.evaluate(evidence)

        pre_gates = evaluate_pre_construction(
            state,
            context,
            confluence_score,
            setup,
            confluence_cfg=self._config.confluence,
            context_cfg=self._config.context,
            filters_cfg=self._config.filters,
            setups_cfg=self._config.setups,
        )

        entry: Decimal | None = None
        stop_loss: Decimal | None = None
        take_profits: tuple[TakeProfit, ...] = ()
        post_gates: tuple[GateResult, ...] = ()

        if setup is not None:
            constructed = self._trade_constructor.construct(setup, atr=primary_atr, spec=state.spec)
            entry = constructed.entry
            stop_loss = constructed.stop_loss
            take_profits = constructed.take_profits
            post_gates = evaluate_post_construction(
                rr=constructed.rr,
                min_rr=self._config.filters.min_rr,
                stop_distance_valid=constructed.stop_distance_valid,
                stop_detail=constructed.stop_detail,
            )

        gates = pre_gates + post_gates
        outcome = (
            DecisionOutcome.TRADE
            if setup is not None and all(g.passed for g in gates)
            else DecisionOutcome.WAIT
        )
        if outcome is DecisionOutcome.WAIT:
            entry = stop_loss = None
            take_profits = ()

        narrative = generate_narrative(
            state=state,
            context=context,
            outcome=outcome,
            setup=setup,
            entry=entry,
            stop_loss=stop_loss,
            take_profits=take_profits,
            confluence_score=confluence_score,
            confluence_band=confluence_band,
            gates=gates,
        )

        return Decision(
            outcome=outcome,
            symbol=state.symbol,
            as_of=state.as_of,
            strategy_version_id=self._strategy_version_id,
            regime=context.regime,
            setup=setup,
            direction=setup.direction if setup is not None else None,
            entry=entry,
            stop_loss=stop_loss,
            take_profits=take_profits,
            confluence_score=confluence_score,
            confluence_band=confluence_band,
            evidence=evidence,
            gates=gates,
            narrative=narrative,
            engine_duration_ms=0,
        )
