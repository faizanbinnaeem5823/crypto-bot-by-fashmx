"""Signal processor: converts raw strategy signal into complete trade plan.

Combines: signal → strength scoring → stop loss → take profit → position sizing → trade plan
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from .stop_loss import StopLevel, StopLossCalculator
from .take_profit import ProfitLevel, TakeProfitCalculator, TakeProfitPlan
from .signal_strength import SignalStrengthScorer

logger = logging.getLogger(__name__)


@dataclass
class TradePlan:
    """Complete trade plan ready for execution.

    Attributes:
        symbol: Trading pair (e.g. ``"BTC/USDT"``).
        side: ``"BUY"`` or ``"SELL"``.
        quantity: Position size as a Decimal.
        entry_price: Planned entry price.
        stop_loss: Stop-loss price.
        take_profits: List of (:class:`.ProfitLevel`) targets.
        signal_strength: Quality score (0.0-1.0).
        expected_risk: Estimated dollar risk.
        expected_reward: Estimated dollar reward (first TP level).
        r_r_ratio: Risk:reward ratio.
        strategy_name: Name of the originating strategy.
        raw_signal: Original signal value (-1, 0, 1).
        metadata: Additional diagnostic information.
    """

    symbol: str
    side: str
    quantity: Decimal
    entry_price: float
    stop_loss: float
    take_profits: List[ProfitLevel]
    signal_strength: float
    expected_risk: float
    expected_reward: float
    r_r_ratio: float
    strategy_name: str
    raw_signal: int
    metadata: Dict[str, Any] = field(default_factory=dict)


class RiskEngineStub:
    """Minimal stand-in for a full risk engine.

    When a real :class:`risk.risk_engine.RiskEngine` is unavailable this
    stub provides the interface that :class:`SignalProcessor` expects.
    """

    def __init__(
        self,
        max_risk_per_trade_pct: float = 1.0,
        max_daily_loss_pct: float = 3.0,
    ) -> None:
        self.max_risk_per_trade_pct = max_risk_per_trade_pct
        self.max_daily_loss_pct = max_daily_loss_pct

    def calculate_position_size(
        self,
        portfolio_value: Decimal,
        entry_price: float,
        stop_price: float,
        daily_pnl: Decimal,
        signal_strength: float,
    ) -> Decimal:
        """Compute position size in base-asset units.

        Caps dollar risk to ``max_risk_per_trade_pct`` of portfolio.
        Reduces size proportionally if ``daily_pnl`` is near the daily
        loss limit.
        """
        portfolio = float(portfolio_value)
        if portfolio <= 0:
            return Decimal("0")

        risk_per_unit = abs(entry_price - stop_price)
        if risk_per_unit == 0:
            return Decimal("0")

        max_dollar_risk = portfolio * self.max_risk_per_trade_pct / 100.0

        # Scale down if approaching daily loss limit
        daily_pnl_f = float(daily_pnl)
        max_daily_loss = portfolio * self.max_daily_loss_pct / 100.0
        if daily_pnl_f < 0:
            remaining_allowance = max_daily_loss + daily_pnl_f
            if remaining_allowance <= 0:
                return Decimal("0")  # daily loss limit hit
            max_dollar_risk = min(max_dollar_risk, remaining_allowance)

        # Adjust by signal strength (reduce size for weak signals)
        max_dollar_risk *= signal_strength

        quantity = max_dollar_risk / risk_per_unit
        return Decimal(str(quantity))


class SignalProcessor:
    """Process strategy signal into complete trade plan.

    The pipeline is:

    1. Score signal strength (0.0-1.0).
    2. If strength < ``min_strength``, reject the trade.
    3. Calculate stop-loss via :class:`StopLossCalculator`.
    4. Calculate take-profit via :class:`TakeProfitCalculator`.
    5. Calculate position size via the risk engine.
    6. Assemble a :class:`TradePlan`.

    Usage::

        processor = SignalProcessor(
            risk_engine=risk_engine,
            stop_method="atr",
            tp_method="fixed_rr",
        )
        plan = processor.process(
            symbol="BTC/USDT",
            signal=1,
            candles=candles_df,
            current_price=50000.0,
            portfolio_value=Decimal("1000"),
            daily_pnl=Decimal("-10"),
            strategy_name="EMA_Cross",
        )
        if plan:
            print(f"Ready to {plan.side} {plan.quantity} {plan.symbol}")
    """

    def __init__(
        self,
        risk_engine: Optional[Any] = None,
        stop_method: str = "atr",
        stop_atr_period: int = 14,
        stop_atr_mult: float = 2.0,
        stop_fixed_pct: float = 5.0,
        tp_method: str = "fixed_rr",
        tp_risk_reward: str = "1:2",
        tp_partial_levels: Optional[List[dict]] = None,
        min_strength: float = 0.25,
        volume_threshold: float = 1.5,
    ) -> None:
        self.risk_engine = risk_engine or RiskEngineStub()
        self.min_strength = min_strength

        self.stop_calc = StopLossCalculator(
            method=stop_method,
            atr_period=stop_atr_period,
            atr_multiplier=stop_atr_mult,
            fixed_pct=stop_fixed_pct,
        )
        self.tp_calc = TakeProfitCalculator(
            method=tp_method,
            risk_reward=tp_risk_reward,
            partial_levels=(
                tp_partial_levels
                if tp_partial_levels is not None
                else [
                    {"ratio": 1.0, "percentage": 50.0},
                    {"ratio": 2.0, "percentage": 50.0},
                ]
            ),
        )
        self.strength_scorer = SignalStrengthScorer(
            volume_threshold=volume_threshold
        )

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def process(
        self,
        symbol: str,
        signal: int,
        candles: pd.DataFrame,
        current_price: float,
        portfolio_value: Decimal,
        daily_pnl: Decimal,
        strategy_name: str,
        signals_from_other_strategies: Optional[List[int]] = None,
    ) -> Optional[TradePlan]:
        """Convert a raw signal into a fully specified :class:`TradePlan`.

        Args:
            symbol: Trading pair (e.g. ``"BTC/USDT"``).
            signal: Raw strategy signal (-1, 0, 1).
            candles: OHLCV DataFrame.
            current_price: Latest market price.
            portfolio_value: Total portfolio value in quote currency.
            daily_pnl: Today's realised + unrealised PnL.
            strategy_name: Human-readable strategy identifier.
            signals_from_other_strategies: Confluence signals.

        Returns:
            A :class:`TradePlan` if the signal passes all filters,
            otherwise ``None``.
        """
        # 1. Flat signal → no trade
        if signal == 0:
            return None

        side = "long" if signal == 1 else "short"
        order_side = "BUY" if signal == 1 else "SELL"

        # 2. Score signal strength
        strength = self.strength_scorer.score(
            candles,
            signal,
            signals_from_other_strategies,
        )
        logger.info(
            "Signal %d for %s strength=%.3f", signal, symbol, strength
        )

        # 3. Reject if too weak
        if strength < self.min_strength:
            logger.info(
                "Signal rejected: strength %.3f < threshold %.3f",
                strength,
                self.min_strength,
            )
            return None

        # 4. Calculate stop-loss
        try:
            stop_level = self.stop_calc.calculate(
                candles, entry_price=current_price, side=side
            )
        except Exception as exc:
            logger.error("Stop-loss calculation failed: %s", exc)
            return None

        # Validate stop makes sense (must be below entry for long, above for short)
        if side == "long" and stop_level.price >= current_price:
            logger.warning(
                "Invalid long stop %.2f >= entry %.2f", stop_level.price, current_price
            )
            return None
        if side == "short" and stop_level.price <= current_price:
            logger.warning(
                "Invalid short stop %.2f <= entry %.2f", stop_level.price, current_price
            )
            return None

        # 5. Calculate take-profit
        try:
            tp_plan = self.tp_calc.calculate(
                entry=current_price,
                stop=stop_level.price,
                side=side,
                candles=candles,
            )
        except Exception as exc:
            logger.error("Take-profit calculation failed: %s", exc)
            return None

        if not tp_plan.levels:
            logger.warning("No take-profit levels generated")
            return None

        # 6. Position sizing
        try:
            quantity = self.risk_engine.calculate_position_size(
                portfolio_value=portfolio_value,
                entry_price=current_price,
                stop_price=stop_level.price,
                daily_pnl=daily_pnl,
                signal_strength=strength,
            )
        except Exception as exc:
            logger.error("Position sizing failed: %s", exc)
            quantity = Decimal("0")

        if quantity <= Decimal("0"):
            logger.info("Zero or negative position size – skipping trade")
            return None

        # 7. Risk / reward metrics
        risk_per_unit = abs(current_price - stop_level.price)
        reward_per_unit = abs(tp_plan.levels[0].price - current_price)
        r_r = reward_per_unit / risk_per_unit if risk_per_unit else 0.0

        # Dollar-denominated estimates
        qty_float = float(quantity)
        expected_risk = qty_float * risk_per_unit
        expected_reward = qty_float * reward_per_unit

        # 8. Build TradePlan
        plan = TradePlan(
            symbol=symbol,
            side=order_side,
            quantity=quantity,
            entry_price=current_price,
            stop_loss=stop_level.price,
            take_profits=tp_plan.levels,
            signal_strength=strength,
            expected_risk=expected_risk,
            expected_reward=expected_reward,
            r_r_ratio=r_r,
            strategy_name=str(strategy_name),
            raw_signal=signal,
            metadata={
                "stop_method": stop_level.method,
                "stop_distance_pct": stop_level.distance_pct,
                "tp_method": tp_plan.method,
                "tp_trailing_activation": tp_plan.trailing_activation,
                "strength_classification": self.strength_scorer.classify(
                    strength
                ),
            },
        )

        logger.info(
            "TradePlan created: %s %s %.4f @ %.2f  "
            "SL=%.2f  TP=%s  R:R=%.2f  Strength=%.2f",
            plan.side,
            plan.symbol,
            qty_float,
            plan.entry_price,
            plan.stop_loss,
            ", ".join(
                f"{lv.price:.1f}({lv.percentage:g}%)"
                for lv in plan.take_profits
            ),
            plan.r_r_ratio,
            plan.signal_strength,
        )
        return plan

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #

    def process_batch(
        self,
        signals: List[Dict[str, Any]],
        candles: pd.DataFrame,
        current_price: float,
        portfolio_value: Decimal,
        daily_pnl: Decimal,
    ) -> List[TradePlan]:
        """Process multiple signals and return only the accepted trade plans.

        Args:
            signals: List of dicts, each containing at least ``symbol``,
                ``signal``, ``strategy_name``, and optionally
                ``other_signals``.
            candles: Shared OHLCV DataFrame.
            current_price: Latest market price.
            portfolio_value: Portfolio value in quote currency.
            daily_pnl: Current daily PnL.

        Returns:
            List of accepted :class:`TradePlan` objects (may be empty).
        """
        plans: List[TradePlan] = []
        for sig in signals:
            plan = self.process(
                symbol=sig["symbol"],
                signal=sig["signal"],
                candles=candles,
                current_price=current_price,
                portfolio_value=portfolio_value,
                daily_pnl=daily_pnl,
                strategy_name=sig["strategy_name"],
                signals_from_other_strategies=sig.get("other_signals"),
            )
            if plan is not None:
                plans.append(plan)
        return plans

    def update_plan_with_trailing(
        self,
        plan: TradePlan,
        new_stop: float,
        new_tp_levels: Optional[List[ProfitLevel]] = None,
    ) -> TradePlan:
        """Return a *copy* of *plan* with an updated stop (and optionally TP).

        Used when a trailing stop has moved or partial profits have been
        taken.

        Args:
            plan: Original trade plan.
            new_stop: New stop-loss price.
            new_tp_levels: Replacement take-profit levels (optional).

        Returns:
            New :class:`TradePlan` with updated fields.
        """
        updated = TradePlan(
            symbol=plan.symbol,
            side=plan.side,
            quantity=plan.quantity,
            entry_price=plan.entry_price,
            stop_loss=new_stop,
            take_profits=new_tp_levels if new_tp_levels else plan.take_profits,
            signal_strength=plan.signal_strength,
            expected_risk=plan.expected_risk,
            expected_reward=plan.expected_reward,
            r_r_ratio=plan.r_r_ratio,
            strategy_name=plan.strategy_name,
            raw_signal=plan.raw_signal,
            metadata={**plan.metadata, "trailing_updated": True},
        )
        return updated
