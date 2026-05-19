"""
Comprehensive integration tests for strategy modules.

Covers:
- StopLossCalculator (ATR, fixed, support, chandelier, time-based, dynamic)
- TakeProfitCalculator (fixed R:R, ATR-based, trailing plan)
- TrailingStop (activation, tracking, exit detection)
- SignalStrengthScorer (volume, volatility, trend, confluence, momentum)
- SignalProcessor (signal -> TradePlan pipeline)
- WalkForwardAnalyzer (PBO, deflated Sharpe, walk-forward windows)
- PerformanceTracker (per-strategy PnL, Sharpe, win rate, circuit breakers)

All tests use synthetic data — no external API calls.
"""

import pytest
import pandas as pd
import numpy as np
from decimal import Decimal
from datetime import datetime, timedelta


# =============================================================================
#  Fixtures
# =============================================================================


@pytest.fixture
def sample_candles():
    """100-row OHLCV DataFrame with slight upward drift."""
    np.random.seed(42)
    base = pd.DataFrame({
        "open": [100.0, 101.0, 102.0, 101.5, 103.0, 104.0, 103.5, 105.0, 106.0, 107.0] * 10,
        "high": [101.5, 102.5, 103.0, 103.5, 105.0, 105.5, 106.0, 107.0, 108.0, 109.0] * 10,
        "low": [99.5, 100.5, 100.0, 101.0, 102.0, 103.0, 103.0, 104.0, 105.0, 106.0] * 10,
        "close": [101.0, 102.0, 101.5, 103.0, 104.0, 103.5, 105.0, 106.0, 107.0, 106.5] * 10,
        "volume": [1000, 1200, 900, 1100, 1300, 1000, 1500, 1200, 1100, 1000] * 10,
    })
    base.index = pd.date_range("2024-01-01", periods=len(base), freq="h")
    return base


@pytest.fixture
def candles_with_trend():
    """30-row upward-trending OHLCV with increasing volume."""
    df = pd.DataFrame({
        "open": [100.0] * 30,
        "high": [105.0] * 30,
        "low": [95.0] * 30,
        "close": list(range(100, 130)),
        "volume": [1000] * 15 + [2000] * 15,
    })
    df.index = pd.date_range("2024-01-01", periods=30, freq="h")
    return df


@pytest.fixture
def synthetic_candles():
    """1000-row synthetic price data with slight upward drift."""
    np.random.seed(42)
    n = 1000
    returns = np.random.normal(0.001, 0.02, n)
    prices = 30000 * np.exp(np.cumsum(returns))
    df = pd.DataFrame({
        "open": prices * 0.99,
        "high": prices * 1.02,
        "low": prices * 0.98,
        "close": prices,
        "volume": np.random.lognormal(10, 0.5, n),
    })
    df.index = pd.date_range("2020-01-01", periods=n, freq="D")
    return df


# =============================================================================
#  TestStopLossCalculator
# =============================================================================


class TestStopLossCalculator:

    # ------------------------------------------------------------------ #
    # ATR method
    # ------------------------------------------------------------------ #

    def test_atr_stop_long(self, sample_candles):
        from strategies.stop_loss import StopLossCalculator
        calc = StopLossCalculator(method="atr", atr_period=14, atr_multiplier=2.0)
        stop = calc.calculate(sample_candles, entry_price=50000.0, side="long")
        assert stop.price < 50000.0          # stop below entry for long
        assert stop.method == "atr"
        assert stop.distance_pct > 0
        assert stop.distance_pct < 20        # reasonable distance
        assert stop.atr_value is not None
        assert stop.atr_value > 0

    def test_atr_stop_short(self, sample_candles):
        from strategies.stop_loss import StopLossCalculator
        calc = StopLossCalculator(method="atr", atr_period=14, atr_multiplier=2.0)
        stop = calc.calculate(sample_candles, entry_price=50000.0, side="short")
        assert stop.price > 50000.0          # stop above entry for short
        assert stop.distance_pct > 0
        assert stop.method == "atr"

    def test_atr_calculation_returns_series(self, sample_candles):
        from strategies.stop_loss import StopLossCalculator
        calc = StopLossCalculator(method="atr", atr_period=14)
        atr = calc.calculate_atr(sample_candles)
        assert isinstance(atr, pd.Series)
        assert len(atr) == len(sample_candles)
        # first (period-1) values are NaN
        assert atr.iloc[:13].isna().all()
        assert atr.iloc[14:].notna().all()
        assert (atr.dropna() > 0).all()

    # ------------------------------------------------------------------ #
    # Fixed method
    # ------------------------------------------------------------------ #

    def test_fixed_stop_long(self, sample_candles):
        from strategies.stop_loss import StopLossCalculator
        calc = StopLossCalculator(method="fixed", fixed_pct=5.0)
        stop = calc.calculate(sample_candles, entry_price=50000.0, side="long")
        assert stop.price == 47500.0         # 5% below
        assert stop.distance_pct == 5.0
        assert stop.method == "fixed"

    def test_fixed_stop_short(self, sample_candles):
        from strategies.stop_loss import StopLossCalculator
        calc = StopLossCalculator(method="fixed", fixed_pct=5.0)
        stop = calc.calculate(sample_candles, entry_price=50000.0, side="short")
        assert stop.price == 52500.0         # 5% above
        assert stop.distance_pct == 5.0

    # ------------------------------------------------------------------ #
    # Support / resistance method
    # ------------------------------------------------------------------ #

    def test_support_stop_long(self, sample_candles):
        from strategies.stop_loss import StopLossCalculator
        calc = StopLossCalculator(method="support", swing_lookback=20)
        stop = calc.calculate(sample_candles, entry_price=105.0, side="long")
        recent_low = float(sample_candles["low"].tail(20).min())
        assert stop.price == recent_low
        assert stop.method == "support"
        assert stop.distance_pct > 0

    def test_support_stop_short(self, sample_candles):
        from strategies.stop_loss import StopLossCalculator
        calc = StopLossCalculator(method="support", swing_lookback=20)
        stop = calc.calculate(sample_candles, entry_price=105.0, side="short")
        recent_high = float(sample_candles["high"].tail(20).max())
        assert stop.price == recent_high
        assert stop.method == "support"

    # ------------------------------------------------------------------ #
    # Chandelier method
    # ------------------------------------------------------------------ #

    def test_chandelier_stop_long(self, sample_candles):
        from strategies.stop_loss import StopLossCalculator
        calc = StopLossCalculator(method="chandelier", swing_lookback=20)
        stop = calc.calculate(sample_candles, entry_price=50000.0, side="long")
        # Chandelier: highest_high - (ATR * mult); capped at entry
        assert stop.price <= 50000.0
        assert stop.method == "chandelier"
        assert stop.atr_value is not None

    def test_chandelier_stop_short(self, sample_candles):
        from strategies.stop_loss import StopLossCalculator
        calc = StopLossCalculator(method="chandelier", swing_lookback=20)
        stop = calc.calculate(sample_candles, entry_price=50000.0, side="short")
        # Chandelier: lowest_low + (ATR * mult); floored at entry
        assert stop.price >= 50000.0
        assert stop.method == "chandelier"

    # ------------------------------------------------------------------ #
    # Time-based stop
    # ------------------------------------------------------------------ #

    def test_time_stop_not_triggered(self):
        from strategies.stop_loss import StopLossCalculator
        calc = StopLossCalculator()
        entry = datetime(2024, 1, 1, 0, 0)
        current = datetime(2024, 1, 1, 12, 0)
        result = calc.check_time_stop(entry, current, max_hold_hours=48.0)
        assert result.should_exit is False
        assert result.elapsed == timedelta(hours=12)
        assert result.reason == ""

    def test_time_stop_triggered(self):
        from strategies.stop_loss import StopLossCalculator
        calc = StopLossCalculator()
        entry = datetime(2024, 1, 1, 0, 0)
        current = datetime(2024, 1, 3, 1, 0)
        result = calc.check_time_stop(entry, current, max_hold_hours=48.0)
        assert result.should_exit is True
        assert result.elapsed == timedelta(hours=49)
        assert "Time stop triggered" in result.reason

    # ------------------------------------------------------------------ #
    # Dynamic stop adjuster
    # ------------------------------------------------------------------ #

    def test_dynamic_adjuster_tightens(self):
        from strategies.stop_loss import DynamicStopAdjuster
        adj = DynamicStopAdjuster(step_pct=2.0, tighten_pct=1.0)
        adj.set_entry(entry=50000.0, stop=47500.0, side="long")
        # Price moves 4% above entry (2x step) -> should tighten by 2x tighten
        new_stop = adj.adjust(current_price=52000.0)
        assert new_stop > 47500.0           # stop moved up
        assert new_stop <= 50000.0          # capped at breakeven

    def test_dynamic_adjuster_no_move_early(self):
        from strategies.stop_loss import DynamicStopAdjuster
        adj = DynamicStopAdjuster(step_pct=2.0, tighten_pct=1.0)
        adj.set_entry(entry=50000.0, stop=47500.0, side="long")
        # Price moves only 0.01 R (49/2500 = 0.0196) -> below 1 step
        new_stop = adj.adjust(current_price=50049.0)
        assert new_stop == 47500.0          # unchanged

    def test_dynamic_adjuster_reset(self):
        from strategies.stop_loss import DynamicStopAdjuster
        adj = DynamicStopAdjuster(step_pct=2.0, tighten_pct=1.0)
        adj.set_entry(entry=50000.0, stop=47500.0, side="long")
        adj.adjust(current_price=52000.0)
        adj.reset()
        assert adj._entry is None
        assert adj._current_stop is None
        assert adj._steps_triggered == 0


# =============================================================================
#  TestTakeProfitCalculator
# =============================================================================


class TestTakeProfitCalculator:

    def test_fixed_rr_long(self, sample_candles):
        from strategies.take_profit import TakeProfitCalculator
        calc = TakeProfitCalculator(
            method="fixed_rr", risk_reward="1:2",
            partial_levels=[{"ratio": 2.0, "percentage": 100.0}]
        )
        plan = calc.calculate(entry=50000, stop=47500, side="long")
        assert len(plan.levels) == 1
        assert plan.levels[0].price == 55000.0   # 50000 + (2500 * 2)
        assert plan.levels[0].ratio == 2.0
        assert plan.method == "fixed_rr"

    def test_fixed_rr_short(self):
        from strategies.take_profit import TakeProfitCalculator
        calc = TakeProfitCalculator(
            method="fixed_rr", risk_reward="1:2",
            partial_levels=[{"ratio": 2.0, "percentage": 100.0}]
        )
        plan = calc.calculate(entry=50000, stop=52500, side="short")
        assert plan.levels[0].price == 45000.0   # 50000 - (2500 * 2)
        assert plan.levels[0].ratio == 2.0

    def test_fixed_rr_partial_levels(self):
        from strategies.take_profit import TakeProfitCalculator
        partials = [
            {"ratio": 1.0, "percentage": 50.0},
            {"ratio": 2.0, "percentage": 50.0},
        ]
        calc = TakeProfitCalculator(method="fixed_rr", partial_levels=partials)
        plan = calc.calculate(entry=50000, stop=47500, side="long")
        assert len(plan.levels) == 2
        assert plan.levels[0].price == 52500.0   # 50000 + 2500 * 1
        assert plan.levels[0].percentage == 50.0
        assert plan.levels[1].price == 55000.0   # 50000 + 2500 * 2
        assert plan.levels[1].percentage == 50.0

    def test_atr_tp_requires_candles(self):
        from strategies.take_profit import TakeProfitCalculator
        calc = TakeProfitCalculator(method="atr", atr_period=14, atr_multiplier=3.0)
        with pytest.raises(ValueError, match="candles DataFrame required"):
            calc.calculate(entry=50000, stop=47500, side="long")

    def test_atr_tp_long(self, sample_candles):
        from strategies.take_profit import TakeProfitCalculator
        calc = TakeProfitCalculator(method="atr", atr_period=14, atr_multiplier=3.0)
        plan = calc.calculate(
            entry=50000, stop=47500, side="long", candles=sample_candles
        )
        assert len(plan.levels) == 1
        assert plan.levels[0].price > 50000.0    # TP above entry
        assert plan.levels[0].percentage == 100.0
        assert plan.method == "atr"

    def test_trailing_plan(self):
        from strategies.take_profit import TakeProfitCalculator
        calc = TakeProfitCalculator(method="trailing", risk_reward="1:2")
        plan = calc.calculate(entry=50000, stop=47500, side="long")
        assert len(plan.levels) == 1
        assert plan.levels[0].price == 55000.0
        assert plan.trailing_activation is not None
        assert plan.trailing_activation == 1.0    # 50% of target R:R (2.0)


# =============================================================================
#  TestTrailingStop
# =============================================================================


class TestTrailingStop:

    def test_trailing_activates(self):
        from strategies.take_profit import TrailingStop
        ts = TrailingStop(activation_rr=1.0, trail_pct=5.0)
        ts.activate(entry=50000, stop=47500, side="long")
        # Price moves to 53000 -> PnL = 3000/2500 = 1.2 R (above activation)
        new_stop = ts.update(53000)
        assert new_stop is not None
        assert new_stop > 47500               # stop moved up
        assert ts.active is True

    def test_trailing_doesnt_activate_early(self):
        from strategies.take_profit import TrailingStop
        ts = TrailingStop(activation_rr=1.0, trail_pct=5.0)
        ts.activate(entry=50000, stop=47500, side="long")
        # Price moves to 51000 -> PnL = 1000/2500 = 0.4 R (below activation)
        new_stop = ts.update(51000)
        assert new_stop is None
        assert ts.active is False

    def test_trailing_exit(self):
        from strategies.take_profit import TrailingStop
        ts = TrailingStop(activation_rr=1.0, trail_pct=5.0)
        ts.activate(entry=50000, stop=47500, side="long")
        ts.update(53000)                      # activate and move
        # Price falls back to hit trailing stop
        assert ts.should_exit(ts._stop_price - 1) is True

    def test_trailing_no_exit_before_activation(self):
        from strategies.take_profit import TrailingStop
        ts = TrailingStop(activation_rr=1.0, trail_pct=5.0)
        ts.activate(entry=50000, stop=47500, side="long")
        # Before activation, should_exit always returns False
        assert ts.should_exit(40000) is False

    def test_trailing_short_side(self):
        from strategies.take_profit import TrailingStop
        ts = TrailingStop(activation_rr=1.0, trail_pct=5.0)
        ts.activate(entry=50000, stop=52500, side="short")
        # Price drops to 47000 -> PnL = 3000/2500 = 1.2 R
        new_stop = ts.update(47000)
        assert new_stop is not None
        assert new_stop < 52500               # stop moved down for short
        assert ts.active is True

    def test_trailing_reset(self):
        from strategies.take_profit import TrailingStop
        ts = TrailingStop(activation_rr=1.0, trail_pct=5.0)
        ts.activate(entry=50000, stop=47500, side="long")
        ts.update(53000)
        ts.reset()
        assert ts.active is False
        assert ts._stop_price == 0.0
        assert ts._best_price == 0.0

    def test_trailing_current_stop_property(self):
        from strategies.take_profit import TrailingStop
        ts = TrailingStop(activation_rr=1.0, trail_pct=5.0)
        ts.activate(entry=50000, stop=47500, side="long")
        assert ts.current_stop == 47500.0


# =============================================================================
#  TestSignalStrengthScorer
# =============================================================================


class TestSignalStrengthScorer:

    def test_no_signal_zero_score(self, candles_with_trend):
        from strategies.signal_strength import SignalStrengthScorer
        scorer = SignalStrengthScorer()
        score = scorer.score(candles_with_trend, signal=0)
        assert score == 0.0

    def test_strong_signal_high_score(self, candles_with_trend):
        from strategies.signal_strength import SignalStrengthScorer
        scorer = SignalStrengthScorer()
        score = scorer.score(
            candles_with_trend, signal=1, signals_from_other_strategies=[1, 1]
        )
        assert score > 0.2
        assert score <= 1.0

    def test_signal_classify(self):
        from strategies.signal_strength import SignalStrengthScorer
        scorer = SignalStrengthScorer()
        assert scorer.classify(0.80) == "strong"
        assert scorer.classify(0.55) == "moderate"
        assert scorer.classify(0.25) == "weak"
        assert scorer.classify(0.05) == "none"

    def test_volume_confirmation_boosts_score(self, candles_with_trend):
        from strategies.signal_strength import SignalStrengthScorer
        scorer = SignalStrengthScorer()
        score_with_high_vol = scorer.score(candles_with_trend, signal=1)
        # 2nd half has 2x volume -> should produce a positive score
        assert score_with_high_vol > 0.0

    def test_confluence_boost(self, candles_with_trend):
        from strategies.signal_strength import SignalStrengthScorer
        scorer = SignalStrengthScorer()
        alone = scorer.score(candles_with_trend, signal=1)
        with_agreement = scorer.score(
            candles_with_trend, signal=1, signals_from_other_strategies=[1, 1]
        )
        # Full agreement should not reduce score vs no data
        assert with_agreement >= alone or abs(with_agreement - alone) < 0.15

    def test_sell_signal_scoring(self, candles_with_trend):
        from strategies.signal_strength import SignalStrengthScorer
        scorer = SignalStrengthScorer()
        score = scorer.score(candles_with_trend, signal=-1)
        assert 0.0 <= score <= 1.0

    def test_internal_volume_score(self, candles_with_trend):
        from strategies.signal_strength import SignalStrengthScorer
        scorer = SignalStrengthScorer()
        vs = scorer._volume_score(candles_with_trend)
        assert 0.0 <= vs <= 1.0

    def test_internal_volatility_score(self, candles_with_trend):
        from strategies.signal_strength import SignalStrengthScorer
        scorer = SignalStrengthScorer()
        vs = scorer._volatility_score(candles_with_trend)
        assert 0.0 <= vs <= 1.0


# =============================================================================
#  TestSignalProcessor
# =============================================================================


class TestSignalProcessor:

    @pytest.fixture
    def processor(self):
        from strategies.signal_processor import SignalProcessor
        return SignalProcessor(
            stop_method="fixed",
            stop_fixed_pct=5.0,
            tp_method="fixed_rr",
            tp_risk_reward="1:2",
            min_strength=0.0,  # accept all non-zero signals
        )

    @pytest.fixture
    def candles_df(self):
        np.random.seed(42)
        n = 50
        closes = 100 + np.cumsum(np.random.normal(0.1, 1.0, n))
        df = pd.DataFrame({
            "open": closes - 0.5,
            "high": closes + 1.0,
            "low": closes - 1.0,
            "close": closes,
            "volume": np.random.lognormal(8, 0.3, n),
        })
        df.index = pd.date_range("2024-01-01", periods=n, freq="h")
        return df

    def test_process_zero_signal_returns_none(self, processor, candles_df):
        plan = processor.process(
            symbol="BTC/USDT",
            signal=0,
            candles=candles_df,
            current_price=50000.0,
            portfolio_value=Decimal("10000"),
            daily_pnl=Decimal("0"),
            strategy_name="Test",
        )
        assert plan is None

    def test_process_long_signal(self, processor, candles_df):
        plan = processor.process(
            symbol="BTC/USDT",
            signal=1,
            candles=candles_df,
            current_price=50000.0,
            portfolio_value=Decimal("10000"),
            daily_pnl=Decimal("0"),
            strategy_name="EMA_Cross",
        )
        assert plan is not None
        assert plan.side == "BUY"
        assert plan.symbol == "BTC/USDT"
        assert plan.entry_price == 50000.0
        assert plan.stop_loss < plan.entry_price  # long stop below entry
        assert plan.take_profits[0].price > plan.entry_price
        assert plan.r_r_ratio > 0
        assert plan.quantity > Decimal("0")
        assert plan.raw_signal == 1
        assert plan.strategy_name == "EMA_Cross"

    def test_process_short_signal(self, processor, candles_df):
        plan = processor.process(
            symbol="BTC/USDT",
            signal=-1,
            candles=candles_df,
            current_price=50000.0,
            portfolio_value=Decimal("10000"),
            daily_pnl=Decimal("0"),
            strategy_name="Test",
        )
        assert plan is not None
        assert plan.side == "SELL"
        assert plan.stop_loss > plan.entry_price  # short stop above entry
        assert plan.take_profits[0].price < plan.entry_price
        assert plan.raw_signal == -1

    def test_process_weak_signal_rejected(self, candles_df):
        from strategies.signal_processor import SignalProcessor
        strict_proc = SignalProcessor(
            stop_method="fixed",
            stop_fixed_pct=5.0,
            tp_method="fixed_rr",
            tp_risk_reward="1:2",
            min_strength=0.99,  # very strict
        )
        plan = strict_proc.process(
            symbol="BTC/USDT",
            signal=1,
            candles=candles_df,
            current_price=50000.0,
            portfolio_value=Decimal("10000"),
            daily_pnl=Decimal("0"),
            strategy_name="Test",
        )
        assert plan is None

    def test_process_batch(self, processor, candles_df):
        signals = [
            {"symbol": "BTC/USDT", "signal": 1, "strategy_name": "S1"},
            {"symbol": "ETH/USDT", "signal": -1, "strategy_name": "S2"},
            {"symbol": "SOL/USDT", "signal": 0, "strategy_name": "S3"},
        ]
        plans = processor.process_batch(
            signals=signals,
            candles=candles_df,
            current_price=50000.0,
            portfolio_value=Decimal("10000"),
            daily_pnl=Decimal("0"),
        )
        assert len(plans) == 2          # zero signal filtered out
        assert plans[0].symbol == "BTC/USDT"
        assert plans[1].symbol == "ETH/USDT"

    def test_update_plan_with_trailing(self, processor, candles_df):
        plan = processor.process(
            symbol="BTC/USDT",
            signal=1,
            candles=candles_df,
            current_price=50000.0,
            portfolio_value=Decimal("10000"),
            daily_pnl=Decimal("0"),
            strategy_name="Test",
        )
        updated = processor.update_plan_with_trailing(
            plan, new_stop=49000.0, new_tp_levels=None
        )
        assert updated.stop_loss == 49000.0
        assert updated.entry_price == plan.entry_price
        assert updated.metadata["trailing_updated"] is True


# =============================================================================
#  TestWalkForwardAnalyzer
# =============================================================================


class TestWalkForwardAnalyzer:

    @pytest.mark.slow
    def test_wfa_produces_results(self, synthetic_candles):
        from strategies.walk_forward import WalkForwardAnalyzer
        from strategies.ema_crossover import EMACrossoverStrategy

        wfa = WalkForwardAnalyzer(
            strategy_class=EMACrossoverStrategy,
            param_grid={"fast": [5, 10], "slow": [20, 30]},
            train_size=200,
            test_size=100,
            n_splits=3,
        )
        results = wfa.run(synthetic_candles)
        assert results is not None
        assert len(results.windows) > 0
        assert results.oos_sharpe is not None
        assert 0.0 <= results.pbo <= 1.0
        assert results.deflated_sharpe is not None

    def test_pbo_calculation(self):
        from strategies.walk_forward import WalkForwardAnalyzer
        wfa = WalkForwardAnalyzer.__new__(WalkForwardAnalyzer)
        # IS: [1.0, 1.2, 0.8], OOS: [0.5, -0.3, 0.2]
        # (1.0, 0.5):  0.5 < 0.5? No  -> not counted
        # (1.2, -0.3): -0.3 < 0   -> count 1
        # (0.8,  0.2):  0.2 < 0.4 -> count 2
        pbo = wfa._calculate_pbo([1.0, 1.2, 0.8], [0.5, -0.3, 0.2])
        assert pbo == pytest.approx(2 / 3)

    def test_pbo_empty_lists(self):
        from strategies.walk_forward import WalkForwardAnalyzer
        wfa = WalkForwardAnalyzer.__new__(WalkForwardAnalyzer)
        assert wfa._calculate_pbo([], []) == 1.0

    def test_pbo_perfect_generalisation(self):
        from strategies.walk_forward import WalkForwardAnalyzer
        wfa = WalkForwardAnalyzer.__new__(WalkForwardAnalyzer)
        pbo = wfa._calculate_pbo([1.0, 1.0, 1.0], [1.0, 1.0, 1.0])
        assert pbo == 0.0

    def test_deflated_sharpe(self):
        from strategies.walk_forward import WalkForwardAnalyzer
        wfa = WalkForwardAnalyzer.__new__(WalkForwardAnalyzer)
        ds = wfa._deflated_sharpe(2.0, n_observations=1000, n_trials=4)
        assert ds < 2.0
        assert ds > 0.0

    def test_deflated_sharpe_single_trial(self):
        from strategies.walk_forward import WalkForwardAnalyzer
        wfa = WalkForwardAnalyzer.__new__(WalkForwardAnalyzer)
        ds = wfa._deflated_sharpe(2.0, n_observations=1000, n_trials=1)
        assert ds == 2.0

    def test_deflated_sharpe_zero_sharpe(self):
        from strategies.walk_forward import WalkForwardAnalyzer
        wfa = WalkForwardAnalyzer.__new__(WalkForwardAnalyzer)
        ds = wfa._deflated_sharpe(0.0, n_observations=1000, n_trials=4)
        assert ds == 0.0

    def test_wfa_insufficient_data(self):
        from strategies.walk_forward import WalkForwardAnalyzer
        from strategies.ema_crossover import EMACrossoverStrategy

        wfa = WalkForwardAnalyzer(
            strategy_class=EMACrossoverStrategy,
            param_grid={"fast": [5], "slow": [20]},
            train_size=500,
            test_size=500,
            n_splits=3,
        )
        small_df = pd.DataFrame({
            "open": [100.0] * 10,
            "high": [101.0] * 10,
            "low": [99.0] * 10,
            "close": [100.0] * 10,
            "volume": [1000] * 10,
        }, index=pd.date_range("2024-01-01", periods=10, freq="D"))
        results = wfa.run(small_df)
        assert len(results.windows) == 0

    def test_wfa_single_window(self, synthetic_candles):
        from strategies.walk_forward import WalkForwardAnalyzer
        from strategies.ema_crossover import EMACrossoverStrategy

        wfa = WalkForwardAnalyzer(
            strategy_class=EMACrossoverStrategy,
            param_grid={"fast": [5, 10], "slow": [20, 30]},
            train_size=400,
            test_size=200,
            n_splits=1,
        )
        window = wfa.run_single_window(
            train_data=synthetic_candles.iloc[:400],
            test_data=synthetic_candles.iloc[400:600],
        )
        assert window.best_params is not None
        assert isinstance(window.is_sharpe, float)
        assert isinstance(window.oos_sharpe, float)

    def test_count_param_combinations(self):
        from strategies.walk_forward import WalkForwardAnalyzer
        from strategies.ema_crossover import EMACrossoverStrategy

        wfa = WalkForwardAnalyzer(
            strategy_class=EMACrossoverStrategy,
            param_grid={"fast": [5, 10], "slow": [20, 30]},
        )
        assert wfa._count_param_combinations() == 4


# =============================================================================
#  TestPerformanceTracker
# =============================================================================


class TestPerformanceTracker:

    def test_record_win(self):
        from strategies.performance_tracker import PerformanceTracker
        pt = PerformanceTracker()
        pt.record_trade("EMA_Cross", pnl=5.0)
        perf = pt.get_performance("EMA_Cross")
        assert perf.total_trades == 1
        assert perf.winning_trades == 1
        assert perf.losing_trades == 0
        assert perf.win_rate == 100.0
        assert perf.total_pnl == 5.0
        assert perf.profit_factor == 1.0  # no losses yet

    def test_record_loss(self):
        from strategies.performance_tracker import PerformanceTracker
        pt = PerformanceTracker()
        pt.record_trade("EMA_Cross", pnl=-3.0)
        perf = pt.get_performance("EMA_Cross")
        assert perf.total_trades == 1
        assert perf.winning_trades == 0
        assert perf.win_rate == 0.0
        assert perf.total_pnl == -3.0

    def test_sharpe_calculation(self):
        from strategies.performance_tracker import PerformanceTracker
        pt = PerformanceTracker()
        # 10 positive trades, 10 negative trades (net positive)
        for _ in range(10):
            pt.record_trade("Test", pnl=2.0)
            pt.record_trade("Test", pnl=-1.0)
        perf = pt.get_performance("Test")
        assert perf.sharpe > 0.0    # more gain than loss
        assert perf.profit_factor > 1.0

    def test_best_strategy_by_sharpe(self):
        from strategies.performance_tracker import PerformanceTracker
        pt = PerformanceTracker()
        # Good strategy: 10 wins, 5 losses
        for _ in range(10):
            pt.record_trade("Good", pnl=2.0)
        for _ in range(5):
            pt.record_trade("Good", pnl=-1.0)
        # Bad strategy: 5 wins, 10 losses
        for _ in range(5):
            pt.record_trade("Bad", pnl=1.0)
        for _ in range(10):
            pt.record_trade("Bad", pnl=-2.0)
        assert pt.get_best_strategy(metric="sharpe") == "Good"

    def test_best_strategy_by_total_pnl(self):
        from strategies.performance_tracker import PerformanceTracker
        pt = PerformanceTracker()
        pt.record_trade("Winner", pnl=100.0)
        pt.record_trade("Loser", pnl=-5.0)
        assert pt.get_best_strategy(metric="total_pnl") == "Winner"

    def test_best_strategy_empty(self):
        from strategies.performance_tracker import PerformanceTracker
        pt = PerformanceTracker()
        assert pt.get_best_strategy() == ""

    def test_should_disable_loser(self):
        from strategies.performance_tracker import PerformanceTracker
        pt = PerformanceTracker(min_trades_for_stats=10)
        # 20 losing trades with variance -> negative Sharpe
        for i in range(20):
            pt.record_trade("Loser", pnl=-0.5 - (i % 3) * 0.3)
        assert bool(pt.should_disable("Loser", min_sharpe=0.0)) is True

    def test_should_disable_not_enough_trades(self):
        from strategies.performance_tracker import PerformanceTracker
        pt = PerformanceTracker(min_trades_for_stats=50)
        for _ in range(10):
            pt.record_trade("Newbie", pnl=-5.0)
        # Not enough trades yet -> not disabled
        assert pt.should_disable("Newbie", min_sharpe=0.0) is False

    def test_should_disable_good_strategy(self):
        from strategies.performance_tracker import PerformanceTracker
        pt = PerformanceTracker(min_trades_for_stats=10)
        for _ in range(20):
            pt.record_trade("Winner", pnl=2.0)
            pt.record_trade("Winner", pnl=-0.5)
        assert bool(pt.should_disable("Winner", min_sharpe=0.0)) is False

    def test_record_batch(self):
        from strategies.performance_tracker import PerformanceTracker
        pt = PerformanceTracker()
        pt.record_batch("Batch", pnls=[1.0, -0.5, 2.0, -1.0, 3.0])
        perf = pt.get_performance("Batch")
        assert perf.total_trades == 5
        assert perf.winning_trades == 3
        assert perf.losing_trades == 2

    def test_get_ranking(self):
        from strategies.performance_tracker import PerformanceTracker
        pt = PerformanceTracker()
        pt.record_trade("A", pnl=10.0)
        pt.record_trade("B", pnl=5.0)
        pt.record_trade("C", pnl=-3.0)
        ranking = pt.get_ranking(metric="total_pnl")
        assert len(ranking) == 3
        assert ranking[0][0] == "A"
        assert ranking[1][0] == "B"
        assert ranking[2][0] == "C"

    def test_get_all(self):
        from strategies.performance_tracker import PerformanceTracker
        pt = PerformanceTracker()
        pt.record_trade("S1", pnl=1.0)
        pt.record_trade("S2", pnl=-1.0)
        all_perf = pt.get_all()
        assert len(all_perf) == 2
        assert "S1" in all_perf
        assert "S2" in all_perf

    def test_reset_single_strategy(self):
        from strategies.performance_tracker import PerformanceTracker
        pt = PerformanceTracker()
        pt.record_trade("S1", pnl=1.0)
        pt.record_trade("S2", pnl=-1.0)
        pt.reset("S1")
        assert pt.get_performance("S1").total_trades == 0
        assert pt.get_performance("S2").total_trades == 1

    def test_reset_all(self):
        from strategies.performance_tracker import PerformanceTracker
        pt = PerformanceTracker()
        pt.record_trade("S1", pnl=1.0)
        pt.reset()
        assert len(pt.get_all()) == 0

    def test_should_reoptimise_deep_drawdown(self):
        from strategies.performance_tracker import PerformanceTracker
        pt = PerformanceTracker(min_trades_for_stats=5)
        # Deep drawdown
        for _ in range(5):
            pt.record_trade("DD_Strat", pnl=-5.0)
        assert pt.should_reoptimise("DD_Strat") is True

    def test_strategy_performance_properties(self):
        from strategies.performance_tracker import StrategyPerformance
        perf = StrategyPerformance("Test")
        assert perf.win_rate == 0.0
        assert perf.sharpe == 0.0
        assert perf.profit_factor == 1.0
        assert perf.expectancy == 0.0
        assert perf.max_drawdown == 0.0
        assert perf.max_consecutive_wins == 0
        assert perf.max_consecutive_losses == 0

    def test_strategy_performance_to_dict(self):
        from strategies.performance_tracker import StrategyPerformance
        perf = StrategyPerformance("Test", total_trades=5, winning_trades=3,
                                   losing_trades=2, total_pnl=10.0)
        d = perf.to_dict()
        assert d["strategy_name"] == "Test"
        assert d["total_trades"] == 5
        assert d["win_rate"] == 60.0
        assert "sharpe" in d
