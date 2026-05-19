"""
CryptoBot Monitor Dashboard V1
READ-ONLY monitoring dashboard for dual-bot crypto trading system.

Connects to:
  * DuckDB (data/cryptobot.duckdb)  -- trades, equity, heartbeat, P&L
  * Redis (optional)                -- kill-switch state
  * File fallback (kill_switch.txt) -- kill-switch without Redis

Gracefully degrades when the database does not yet exist (first run).
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb
import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Resolve DB path relative to project root
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = _PROJECT_ROOT / "data" / "cryptobot.duckdb"

BOT_IDS = ["bot_a", "bot_b"]
DEFAULT_EQUITY: float = 500.0

# Kill-switch state file (works without Redis)
KILL_SWITCH_FILE = Path(tempfile.gettempdir()) / "cryptobot_kill_switch.txt"

# Redis keys (mirror src/risk/kill_switch.py)
REDIS_KEY_STATE = "cryptobot:kill:state"


# ---------------------------------------------------------------------------
# DuckDB helpers -- read-only
# ---------------------------------------------------------------------------

def _db_exists() -> bool:
    return DB_PATH.exists()


def _connect() -> Optional[duckdb.DuckDBPyConnection]:
    if not _db_exists():
        return None
    try:
        return duckdb.connect(str(DB_PATH), read_only=True)
    except Exception:
        return None


def get_default_health(bot_id: str) -> Dict[str, Any]:
    """Return a sensible default when no DB row is present."""
    return {
        "bot_id": bot_id,
        "status": "unknown",
        "last_heartbeat_age": float("inf"),
        "daily_pnl": 0.0,
        "total_trades": 0,
        "winning_trades": 0,
        "equity": DEFAULT_EQUITY,
        "cash": DEFAULT_EQUITY,
        "open_positions": 0,
        "win_rate": 0.0,
    }


def load_bot_health(bot_id: str) -> Dict[str, Any]:
    """Aggregate bot health from heartbeat, equity, daily_pnl and trades."""
    conn = _connect()
    if conn is None:
        return get_default_health(bot_id)

    health = get_default_health(bot_id)
    health["bot_id"] = bot_id

    try:
        # ---- heartbeat age ----
        row = conn.execute(
            "SELECT EXTRACT(EPOCH FROM (?::TIMESTAMP - last_beat)) "
            "FROM heartbeat WHERE bot_id = ?",
            [datetime.now(timezone.utc), bot_id],
        ).fetchone()
        if row and row[0] is not None:
            age = float(row[0])
            health["last_heartbeat_age"] = age
            if age < 60:
                health["status"] = "healthy"
            elif age < 300:
                health["status"] = "degraded"
            else:
                health["status"] = "halted"
        else:
            health["status"] = "unknown"

        # ---- latest equity ----
        row = conn.execute(
            "SELECT equity, cash FROM equity WHERE bot_id = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            [bot_id],
        ).fetchone()
        if row:
            health["equity"] = float(row[0]) if row[0] is not None else DEFAULT_EQUITY
            health["cash"] = float(row[1]) if row[1] is not None else DEFAULT_EQUITY

        # ---- daily P&L ----
        row = conn.execute(
            "SELECT daily_pnl FROM daily_pnl WHERE bot_id = ?",
            [bot_id],
        ).fetchone()
        if row and row[0] is not None:
            health["daily_pnl"] = float(row[0])

        # ---- trade counts ----
        row = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE bot_id = ?",
            [bot_id],
        ).fetchone()
        health["total_trades"] = int(row[0]) if row else 0

        row = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE bot_id = ? AND pnl > 0",
            [bot_id],
        ).fetchone()
        health["winning_trades"] = int(row[0]) if row else 0

        total = health["total_trades"]
        wins = health["winning_trades"]
        health["win_rate"] = round((wins / total) * 100, 1) if total > 0 else 0.0

        # ---- open positions derived from open-status trades ----
        row = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE bot_id = ? AND status = 'open'",
            [bot_id],
        ).fetchone()
        health["open_positions"] = int(row[0]) if row else 0

        conn.close()
    except Exception as exc:
        logger.error("Dashboard health query error for %s: %s", bot_id, exc)
        st.error(f"Database health query error: {exc}")
        try:
            conn.close()
        except Exception:
            pass

    return health


def load_equity_curve(bot_id: str, limit: int = 1000) -> pd.DataFrame:
    """Load equity history for charting."""
    conn = _connect()
    if conn is None:
        return pd.DataFrame()
    try:
        df = conn.execute(
            "SELECT timestamp, equity, cash FROM equity "
            "WHERE bot_id = ? ORDER BY timestamp DESC LIMIT ?",
            [bot_id, limit],
        ).df()
        conn.close()
        if not df.empty:
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values("timestamp").reset_index(drop=True)
            # compute running drawdown
            cummax = df["equity"].cummax()
            df["drawdown"] = ((df["equity"] - cummax) / cummax * 100).round(2)
        return df
    except Exception as exc:
        logger.error("Equity query error: %s", exc)
        st.error(f"Equity query error: {exc}")
        try:
            conn.close()
        except Exception:
            pass
        return pd.DataFrame()


def load_recent_trades(bot_id: str, limit: int = 50) -> pd.DataFrame:
    """Load recent closed/open trades."""
    conn = _connect()
    if conn is None:
        return pd.DataFrame()
    try:
        df = conn.execute(
            "SELECT id, symbol, side, quantity, price, pnl, timestamp, status "
            "FROM trades WHERE bot_id = ? ORDER BY timestamp DESC LIMIT ?",
            [bot_id, limit],
        ).df()
        conn.close()
        if not df.empty:
            df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df
    except Exception as exc:
        logger.error("Trades query error: %s", exc)
        try:
            conn.close()
        except Exception:
            pass
        return pd.DataFrame()


def load_open_positions(bot_id: str) -> pd.DataFrame:
    """Derive open positions from trades with status='open'."""
    conn = _connect()
    if conn is None:
        return pd.DataFrame()
    try:
        df = conn.execute(
            "SELECT id, symbol, side, quantity as size, price as entry_price, "
            "pnl, timestamp as entry_time, status "
            "FROM trades WHERE bot_id = ? AND status = 'open' "
            "ORDER BY timestamp DESC",
            [bot_id],
        ).df()
        conn.close()
        if not df.empty:
            df["entry_time"] = pd.to_datetime(df["entry_time"])
            # current_price placeholder = entry_price (no live feed in dashboard)
            df["current_price"] = df["entry_price"]
            df["pnl_pct"] = 0.0
        return df
    except Exception as exc:
        logger.error("Positions query error: %s", exc)
        try:
            conn.close()
        except Exception:
            pass
        return pd.DataFrame()


def load_kill_state() -> str:
    """Check file fallback first, then try Redis."""
    # 1) file fallback
    try:
        if KILL_SWITCH_FILE.exists():
            content = KILL_SWITCH_FILE.read_text().strip()
            if content == "triggered":
                return "triggered"
            elif content == "armed":
                return "armed"
    except Exception:
        pass

    # 2) Redis (optional)
    try:
        import redis
        r = redis.Redis(host="localhost", port=6379, decode_responses=True, socket_connect_timeout=1)  # noqa: E501
        state = r.get(REDIS_KEY_STATE)
        if state:
            return str(state)
    except Exception:
        pass

    return "safe"


def set_kill_state(state: str) -> None:
    """Persist kill state to file fallback (always works) and Redis if available."""
    try:
        KILL_SWITCH_FILE.write_text(state)
    except Exception as exc:
        logger.error("Failed to write kill-switch file: %s", exc)

    try:
        import redis
        r = redis.Redis(host="localhost", port=6379, decode_responses=True, socket_connect_timeout=1)  # noqa: E501
        if state == "safe":
            r.delete(REDIS_KEY_STATE)
        else:
            r.set(REDIS_KEY_STATE, state)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------

def _apply_dark_theme() -> None:
    """Inject dark-theme CSS overrides."""
    st.markdown(
        """
        <style>
        .stApp {
            background-color: #0e1117;
            color: #fafafa;
        }
        /* Metric cards */
        div[data-testid="stMetricValue"] {
            font-size: 1.5rem !important;
            font-weight: 700 !important;
        }
        div[data-testid="stMetricDelta"] {
            font-size: 0.85rem !important;
        }
        /* Dataframes */
        .stDataFrame {
            font-size: 0.85rem;
        }
        /* Section headers */
        h1, h2, h3, h4 {
            color: #fafafa !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _status_color(status: str) -> str:
    return {"healthy": "green", "degraded": "orange", "halted": "red"}.get(
        status, "gray"
    )


# ---------------------------------------------------------------------------
# UI sections
# ---------------------------------------------------------------------------

def _render_header(kill_state: str) -> None:
    """Top bar: title + kill-switch pill."""
    col_title, col_kill = st.columns([4, 1])
    with col_title:
        st.title("Crypto Trading Bot Dashboard")
        st.caption(f"Last refresh: `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}`")  # noqa: E501

    with col_kill:
        st.markdown("###")
        if kill_state == "triggered":
            st.error("KILL SWITCH: TRIGGERED", icon="🛑")
        elif kill_state == "armed":
            st.warning("KILL SWITCH: ARMED", icon="⚠️")
        else:
            st.success("KILL SWITCH: SAFE", icon="✅")


def _render_kill_switch_controls(kill_state: str) -> None:
    """Kill-switch buttons top-right."""
    st.markdown("---")
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("🔴 TRIGGER Kill Switch", use_container_width=True, type="primary"):  # noqa: E501
            set_kill_state("triggered")
            st.rerun()
    with c2:
        if st.button("🟡 ARM Kill Switch", use_container_width=True):
            set_kill_state("armed")
            st.rerun()
    with c3:
        if st.button("🟢 RESET to SAFE", use_container_width=True):
            set_kill_state("safe")
            st.rerun()
    st.markdown("---")


def _render_bot_summary(bot_id: str, health: Dict[str, Any]) -> None:
    """Summary KPI cards for one bot."""
    status = health.get("status", "unknown")
    color = _status_color(status)

    st.subheader(f"Bot: `{bot_id}`  —  Status: :{color}[{status.upper()}]")

    cols = st.columns(4)
    with cols[0]:
        age = health.get("last_heartbeat_age", float("inf"))
        if age == float("inf"):
            st.metric("Heartbeat", "—", "no data")
        else:
            val = f"{age:.0f}s" if age < 120 else f"{age / 60:.1f}m"
            delta_color = "normal" if age < 60 else "inverse" if age < 300 else "off"
            st.metric("Heartbeat Age", val, None, delta_color=delta_color)
    with cols[1]:
        eq = health.get("equity", DEFAULT_EQUITY)
        st.metric("Equity", f"${eq:,.2f}")
    with cols[2]:
        pnl = health.get("daily_pnl", 0.0)
        delta_color = "normal" if pnl >= 0 else "inverse"
        st.metric("Daily P&L", f"${pnl:+.2f}", delta_color=delta_color)
    with cols[3]:
        wr = health.get("win_rate", 0.0)
        tt = health.get("total_trades", 0)
        st.metric("Win Rate", f"{wr:.1f}%", f"{tt} trades")

    # secondary row
    cols2 = st.columns(4)
    with cols2[0]:
        st.metric("Open Positions", health.get("open_positions", 0))
    with cols2[1]:
        cash = health.get("cash", DEFAULT_EQUITY)
        st.metric("Cash", f"${cash:,.2f}")
    with cols2[2]:
        wins = health.get("winning_trades", 0)
        st.metric("Wins / Total", f"{wins} / {tt}")
    with cols2[3]:
        open_p = health.get("open_positions", 0)
        st.metric("Exposure", f"{open_p} active")


def _render_equity_chart(bot_id: str) -> None:
    """Equity curve + drawdown chart."""
    df = load_equity_curve(bot_id)
    if df.empty:
        st.info("📭 No equity data yet. Trades will appear here once the bot runs.")
        return

    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.7, 0.3],
        vertical_spacing=0.08,
        subplot_titles=("Equity Curve", "Drawdown %"),
    )

    fig.add_trace(
        go.Scatter(
            x=df["timestamp"],
            y=df["equity"],
            mode="lines",
            name="Equity",
            line=dict(color="#00cc96", width=2),
            fill="tozeroy",
            fillcolor="rgba(0,204,150,0.1)",
        ),
        row=1, col=1,
    )

    fig.add_trace(
        go.Scatter(
            x=df["timestamp"],
            y=df["drawdown"],
            mode="lines",
            name="Drawdown",
            line=dict(color="#ff4b4b", width=1.5),
            fill="tozeroy",
            fillcolor="rgba(255,75,75,0.1)",
        ),
        row=2, col=1,
    )

    fig.update_layout(
        template="plotly_dark",
        height=450,
        margin=dict(l=40, r=40, t=50, b=40),
        showlegend=False,
        hovermode="x unified",
    )
    fig.update_yaxes(title_text="Equity ($)", row=1, col=1)
    fig.update_yaxes(title_text="DD %", row=2, col=1)

    st.plotly_chart(fig, use_container_width=True)


def _render_open_positions(bot_id: str) -> None:
    """Table of currently open positions."""
    df = load_open_positions(bot_id)
    if df.empty:
        st.info("No open positions.")
        return

    display = df.copy()
    display["entry_price"] = display["entry_price"].apply(lambda x: f"${x:,.4f}")
    display["size"] = display["size"].apply(lambda x: f"{x:,.4f}")
    display["pnl"] = display["pnl"].apply(
        lambda x: f"${x:+.2f}" if pd.notna(x) else "—"
    )

    st.dataframe(
        display[["symbol", "side", "entry_price", "current_price", "size", "pnl", "entry_time"]],  # noqa: E501
        use_container_width=True,
        hide_index=True,
    )


def _render_recent_trades(bot_id: str) -> None:
    """Table of recent trades."""
    df = load_recent_trades(bot_id)
    if df.empty:
        st.info("No trade history yet.")
        return

    display = df.copy()
    display["price"] = display["price"].apply(lambda x: f"${x:,.4f}")
    display["quantity"] = display["quantity"].apply(lambda x: f"{x:,.4f}")
    display["pnl"] = display["pnl"].apply(
        lambda x: f"${x:+.2f}" if pd.notna(x) else "—"
    )
    display["timestamp"] = display["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")

    # colour-code P&L column via styled HTML is tricky in Streamlit,
    # so we keep it simple and rely on the +/- prefix.
    st.dataframe(
        display[["timestamp", "symbol", "side", "price", "quantity", "pnl", "status"]],  # noqa: E501
        use_container_width=True,
        hide_index=True,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> str:
    """Main entry point for the Streamlit dashboard."""
    # Page config
    st.set_page_config(
        page_title="CryptoBot Dashboard",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    _apply_dark_theme()

    # ---- auto-refresh ----
    st_autorefresh = getattr(st, "autorefresh", None)
    if st_autorefresh is not None:
        try:
            st_autorefresh(interval=10_000, limit=100_000, key="auto-refresh")
        except Exception:
            pass

    # ---- kill switch state ----
    kill_state = load_kill_state()

    # ---- header ----
    _render_header(kill_state)

    # ---- kill switch controls ----
    _render_kill_switch_controls(kill_state)

    # ---- database warning ----
    if not _db_exists():
        st.warning(
            f"⚠️ Database not found at `{DB_PATH}`.  "
            "The dashboard is running in fallback mode.  "
            "Start a trading bot to populate the database.",
            icon="📭",
        )

    # ---- per-bot tabs ----
    tabs = st.tabs([f"🤖 {bid.upper()}" for bid in BOT_IDS] + ["📊 Overview"])

    for idx, bot_id in enumerate(BOT_IDS):
        with tabs[idx]:
            health = load_bot_health(bot_id)
            _render_bot_summary(bot_id, health)

            st.markdown("---")
            st.subheader("Equity & Drawdown")
            _render_equity_chart(bot_id)

            st.markdown("---")
            st.subheader("Open Positions")
            _render_open_positions(bot_id)

            st.markdown("---")
            st.subheader("Recent Trades")
            _render_recent_trades(bot_id)

    # ---- overview tab ----
    with tabs[-1]:
        st.subheader("Fleet Overview")
        health_rows: List[Dict[str, Any]] = []
        for bot_id in BOT_IDS:
            h = load_bot_health(bot_id)
            health_rows.append({
                "Bot": bot_id,
                "Status": h.get("status", "unknown"),
                "Equity ($)": f"{h.get('equity', 0):,.2f}",
                "Daily P&L ($)": f"{h.get('daily_pnl', 0):+.2f}",
                "Trades": h.get("total_trades", 0),
                "Win Rate (%)": f"{h.get('win_rate', 0):.1f}",
                "Open Pos": h.get("open_positions", 0),
                "Heartbeat": "OK" if h.get("last_heartbeat_age", float("inf")) < 60 else "STALE",  # noqa: E501
            })
        overview_df = pd.DataFrame(health_rows)
        if overview_df.empty:
            st.info("No data available yet.")
        else:
            st.dataframe(overview_df, use_container_width=True, hide_index=True)

        st.markdown("---")
        # Combined equity chart
        st.subheader("Combined Equity Curves")
        import plotly.graph_objects as go
        fig = go.Figure()
        for bot_id in BOT_IDS:
            df = load_equity_curve(bot_id)
            if not df.empty:
                fig.add_trace(go.Scatter(
                    x=df["timestamp"],
                    y=df["equity"],
                    mode="lines",
                    name=bot_id,
                ))
        if not fig.data:
            st.info("No equity data to plot.")
        else:
            fig.update_layout(
                template="plotly_dark",
                height=350,
                margin=dict(l=40, r=40, t=40, b=40),
                hovermode="x unified",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),  # noqa: E501
            )
            fig.update_yaxes(title_text="Equity ($)")
            st.plotly_chart(fig, use_container_width=True)

        st.markdown("---")
        st.caption(
            "Database path: `{db}`  |  Kill-switch file: `{kf}`".format(
                db=DB_PATH, kf=KILL_SWITCH_FILE
            )
        )

    return "OK"


if __name__ == "__main__":
    main()
