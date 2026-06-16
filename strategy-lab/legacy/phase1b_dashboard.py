"""Interactive Phase 1B operator dashboard for InfiniteLoop Strategy Lab.
Run with: streamlit run phase1b_dashboard.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from backtest.engine import backtest_direction
from backtest.metrics import score_results
from backtest.spread_engine import backtest_spreads
from data.loader import load_day_features, split_train_oos
from data.market_data import MarketDataClient
from hermes.client import HermesClient
from strategy.orb_direction import ORBDirectionStrategy

LOGS_DIR = Path(__file__).resolve().parent / "logs"
STRATEGY_STORE_FILE = LOGS_DIR / "strategy_admin_store.json"


def default_strategy_record() -> dict[str, Any]:
    return {
        "name": "orb_default",
        "kind": "orb_direction",
        "params": ORBDirectionStrategy().get_params(),
        "notes": "Baseline ORB strategy.",
    }


def load_strategy_store() -> list[dict[str, Any]]:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    if not STRATEGY_STORE_FILE.exists():
        seed = [default_strategy_record()]
        save_strategy_store(seed)
        return seed

    try:
        payload = json.loads(STRATEGY_STORE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        payload = {}

    strategies = payload.get("strategies", []) if isinstance(payload, dict) else []
    if not strategies:
        strategies = [default_strategy_record()]
        save_strategy_store(strategies)
    return strategies


def save_strategy_store(strategies: list[dict[str, Any]]) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"strategies": strategies}
    STRATEGY_STORE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def find_strategy(strategies: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for item in strategies:
        if item.get("name") == name:
            return item
    return None


def render_param_editor(prefix: str, base_params: dict[str, Any]) -> dict[str, Any]:
    edited = {
        "gap_threshold_pct": st.slider(f"Gap threshold % ({prefix})", 0.01, 2.0, float(base_params["gap_threshold_pct"]), 0.01),
        "orb_breakout_pct": st.slider(f"ORB breakout % ({prefix})", 0.01, 2.0, float(base_params["orb_breakout_pct"]), 0.01),
        "delta_bias_threshold": st.slider(f"Delta bias threshold ({prefix})", 10.0, 1000.0, float(base_params["delta_bias_threshold"]), 10.0),
        "neutral_band_pct": st.slider(f"Neutral band % ({prefix})", 0.01, 2.0, float(base_params["neutral_band_pct"]), 0.01),
        "entry_hour": st.slider(f"Entry hour ({prefix})", 9, 15, int(base_params["entry_hour"])),
        "short_delta": st.slider(f"Short delta ({prefix})", 5, 45, int(base_params["short_delta"])),
        "spread_width_usd": st.slider(f"Spread width ({prefix})", 1, 20, int(base_params["spread_width_usd"])),
        "profit_target_pct": st.slider(f"Profit target % ({prefix})", 10, 90, int(base_params["profit_target_pct"])),
        "stop_loss_pct": st.slider(f"Stop loss % ({prefix})", 50, 400, int(base_params["stop_loss_pct"])),
        "forced_exit_hour": int(base_params.get("forced_exit_hour", 15)),
    }
    return edited


@st.cache_resource
def get_client() -> MarketDataClient:
    return MarketDataClient()


@st.cache_data(show_spinner=False)
def get_features(start_date: str, end_date: str) -> pd.DataFrame:
    client = get_client()
    return load_day_features(client, start_date, end_date)


def render_style() -> None:
    st.markdown(
        """
        <style>
          @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&display=swap');
          html, body, [class*="css"]  { font-family: 'Space Grotesk', sans-serif; }
          .stApp {
            background: radial-gradient(circle at 20% 20%, #f6f7ef 0%, #efe7d2 45%, #e8dcc2 100%);
          }
          .block-container {
            padding-top: 1.2rem;
            padding-bottom: 1.2rem;
          }
          .hero {
            padding: 1rem 1.2rem;
            border-radius: 14px;
            background: linear-gradient(120deg, #13293d 0%, #1b4965 100%);
            color: #f8f5ef;
            margin-bottom: 1rem;
          }
          .hero h1 {
            margin: 0;
            font-size: 1.7rem;
          }
          .hero p {
            margin: 0.2rem 0 0;
            opacity: 0.9;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )


def latest_dashboards(limit: int = 20) -> list[Path]:
    if not LOGS_DIR.exists():
        return []
    files = sorted(LOGS_DIR.glob("dashboard_*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def test_strategy(start_date: str, end_date: str, params: dict) -> tuple[pd.DataFrame, pd.DataFrame, object]:
    client = get_client()
    features = get_features(start_date, end_date)
    train_df, _ = split_train_oos(features)
    if train_df.empty:
        return pd.DataFrame(), pd.DataFrame(), None

    strategy = ORBDirectionStrategy.from_params(params)
    direction_results = backtest_direction(train_df, strategy)
    spx_daily = client.get_spx_daily(start_date, end_date)
    spread_results = backtest_spreads(
        direction_results,
        spx_daily,
        client.vix,
        strategy.get_spread_params(),
        risk_free_rate=client.get_risk_free_rate(end_date),
    )
    scorecard = score_results(direction_results, spread_results)
    return direction_results, spread_results, scorecard


def main() -> None:
    st.set_page_config(page_title="InfiniteLoop Phase 1B", layout="wide")
    render_style()

    if "strategies" not in st.session_state:
        st.session_state.strategies = load_strategy_store()
    if "active_strategy" not in st.session_state:
        st.session_state.active_strategy = st.session_state.strategies[0]["name"]
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    current = find_strategy(st.session_state.strategies, st.session_state.active_strategy)
    if current is None:
        st.session_state.active_strategy = st.session_state.strategies[0]["name"]
        current = st.session_state.strategies[0]

    st.markdown(
        """
        <div class="hero">
          <h1>InfiniteLoop Phase 1B Control Deck</h1>
          <p>Inspect runs, test strategy parameters, and discuss changes with Hermes.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    client = get_client()
    st.caption(client.coverage_report().replace("\n", " | "))

    with st.sidebar:
        st.subheader("Active Strategy")
        strategy_names = [item["name"] for item in st.session_state.strategies]
        selected_sidebar_strategy = st.selectbox("Selected", strategy_names, index=strategy_names.index(st.session_state.active_strategy))
        st.session_state.active_strategy = selected_sidebar_strategy
        current = find_strategy(st.session_state.strategies, st.session_state.active_strategy) or st.session_state.strategies[0]

        st.subheader("Run Window")
        start_date = st.date_input("Start", value=pd.Timestamp("2020-01-01").date())
        end_date = st.date_input("End", value=pd.Timestamp("2026-06-05").date())
        st.subheader("Strategy Params")

        base = current.get("params", ORBDirectionStrategy().get_params())
        params = {
            "gap_threshold_pct": st.slider("Gap threshold %", 0.01, 2.0, float(base["gap_threshold_pct"]), 0.01),
            "orb_breakout_pct": st.slider("ORB breakout %", 0.01, 2.0, float(base["orb_breakout_pct"]), 0.01),
            "delta_bias_threshold": st.slider("Delta bias threshold", 10.0, 1000.0, float(base["delta_bias_threshold"]), 10.0),
            "neutral_band_pct": st.slider("Neutral band %", 0.01, 2.0, float(base["neutral_band_pct"]), 0.01),
            "entry_hour": st.slider("Entry hour", 9, 15, int(base["entry_hour"])),
            "short_delta": st.slider("Short delta", 5, 45, int(base["short_delta"])),
            "spread_width_usd": st.slider("Spread width", 1, 20, int(base["spread_width_usd"])),
            "profit_target_pct": st.slider("Profit target %", 10, 90, int(base["profit_target_pct"])),
            "stop_loss_pct": st.slider("Stop loss %", 50, 400, int(base["stop_loss_pct"])),
            "forced_exit_hour": int(base["forced_exit_hour"]),
        }

        run_test = st.button("Run What-If Backtest", type="primary")

    tab_summary, tab_whatif, tab_hermes, tab_admin = st.tabs(["Run Summary", "What-If Strategy", "Hermes Desk", "Strategy Admin"])

    with tab_summary:
        st.subheader("Latest HTML Dashboards")
        dashboards = latest_dashboards()
        if not dashboards:
            st.info("No dashboard HTML snapshots found yet. Run the loop once to generate one.")
        else:
            rows = []
            for dash in dashboards:
                rows.append(
                    {
                        "file": dash.name,
                        "updated": pd.Timestamp(dash.stat().st_mtime, unit="s"),
                        "path": str(dash),
                    }
                )
            dash_df = pd.DataFrame(rows)
            st.dataframe(dash_df, use_container_width=True, hide_index=True)
            selected = st.selectbox("Preview file", dash_df["file"].tolist())
            selected_path = next(p for p in dashboards if p.name == selected)
            with selected_path.open("r", encoding="utf-8") as handle:
                st.download_button(
                    label="Download selected dashboard HTML",
                    data=handle.read(),
                    file_name=selected_path.name,
                    mime="text/html",
                )
            st.code(str(selected_path), language="text")

    with tab_whatif:
        st.subheader("Parameter Backtest Sandbox")
        if run_test:
            with st.spinner("Running backtest..."):
                direction_results, spread_results, scorecard = test_strategy(str(start_date), str(end_date), params)

            if scorecard is None:
                st.warning("No feature rows available in the selected range.")
            else:
                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Accuracy", f"{scorecard.direction_accuracy:.2%}")
                col2.metric("Profit Factor", f"{scorecard.profit_factor:.2f}")
                col3.metric("Sharpe", f"{scorecard.sharpe_ratio:.2f}")
                col4.metric("Trades", str(scorecard.total_trades))

                pnl = spread_results.get("pnl_per_contract", pd.Series(dtype=float)).fillna(0.0).cumsum()
                if not pnl.empty:
                    pnl_frame = pd.DataFrame({"trade": range(1, len(pnl) + 1), "cum_pnl": pnl.values})
                    fig = px.area(
                        pnl_frame,
                        x="trade",
                        y="cum_pnl",
                        title="Cumulative P&L",
                        template="plotly_white",
                        color_discrete_sequence=["#1b4965"],
                    )
                    st.plotly_chart(fig, use_container_width=True)

                st.subheader("Latest Trades")
                st.dataframe(spread_results.tail(30), use_container_width=True)

    with tab_hermes:
        st.subheader("Discuss with Hermes")
        hermes = HermesClient()
        if not hermes.is_available():
            st.error("Hermes unavailable. Start Ollama and ensure hermes3 is loaded.")
        else:
            st.success(f"Connected to {hermes.model} at {hermes.base_url}")

            active_strategy = find_strategy(st.session_state.strategies, st.session_state.active_strategy) or current
            user_prompt = st.text_area(
                "Prompt",
                value=(
                    "Review this strategy and suggest one change to improve direction accuracy.\n"
                    + json.dumps(
                        {
                            "strategy_name": active_strategy["name"],
                            "strategy_kind": active_strategy.get("kind", "orb_direction"),
                            "strategy_params": params,
                            "notes": active_strategy.get("notes", ""),
                        },
                        indent=2,
                    )
                ),
                height=180,
            )
            c1, c2 = st.columns(2)
            send_chat = c1.button("Ask Hermes", type="primary")
            send_json = c2.button("Ask Hermes (JSON expected)")

            if send_chat and user_prompt.strip():
                response = hermes.generate(user_prompt.strip())
                st.session_state.chat_history.append(("user", user_prompt.strip()))
                st.session_state.chat_history.append(("hermes", response))

            if send_json and user_prompt.strip():
                try:
                    response_json = hermes.generate_json(user_prompt.strip())
                    response = json.dumps(response_json, indent=2)
                except Exception as exc:  # noqa: BLE001
                    response = f"JSON parse failed: {exc}"
                st.session_state.chat_history.append(("user", user_prompt.strip()))
                st.session_state.chat_history.append(("hermes", response))

            for role, text in st.session_state.chat_history[-10:]:
                if role == "user":
                    st.markdown(f"**You:** {text}")
                else:
                    st.markdown(f"**Hermes:**\n```json\n{text}\n```" if text.strip().startswith("{") else f"**Hermes:** {text}")

    with tab_admin:
        st.subheader("Strategy Admin")
        st.caption("Create, edit, activate, or remove strategies. Hermes can then chat against the active strategy context.")

        mode = st.radio("Mode", ["Work on Existing", "Add New"], horizontal=True)

        if mode == "Work on Existing":
            names = [item["name"] for item in st.session_state.strategies]
            selected_name = st.selectbox("Strategy", names, key="admin_existing_strategy")
            selected = find_strategy(st.session_state.strategies, selected_name)
            if selected:
                st.text_input("Name", value=selected["name"], disabled=True)
                notes = st.text_area("Notes", value=selected.get("notes", ""), key=f"notes_{selected_name}")
                edited_params = render_param_editor(f"admin_{selected_name}", selected.get("params", ORBDirectionStrategy().get_params()))

                c1, c2, c3 = st.columns(3)
                save_existing = c1.button("Save Changes", type="primary")
                use_existing = c2.button("Set Active")
                delete_existing = c3.button("Delete Strategy")

                if save_existing:
                    selected["notes"] = notes
                    selected["params"] = edited_params
                    save_strategy_store(st.session_state.strategies)
                    st.success(f"Saved {selected_name}.")

                if use_existing:
                    st.session_state.active_strategy = selected_name
                    st.success(f"Active strategy set to {selected_name}.")

                if delete_existing:
                    if selected_name == "orb_default":
                        st.warning("orb_default is protected and cannot be deleted.")
                    else:
                        st.session_state.strategies = [s for s in st.session_state.strategies if s.get("name") != selected_name]
                        if st.session_state.active_strategy == selected_name:
                            st.session_state.active_strategy = "orb_default"
                        save_strategy_store(st.session_state.strategies)
                        st.success(f"Deleted {selected_name}.")
                        st.rerun()

        else:
            new_name = st.text_input("New strategy name", value="orb_candidate_01")
            new_notes = st.text_area("Notes", value="Candidate strategy for testing.")
            template = st.selectbox("Template", ["orb_direction"])
            template_params = ORBDirectionStrategy().get_params() if template == "orb_direction" else ORBDirectionStrategy().get_params()
            new_params = render_param_editor("admin_new", template_params)
            add_new = st.button("Create Strategy", type="primary")

            if add_new:
                name_clean = new_name.strip()
                if not name_clean:
                    st.error("Strategy name is required.")
                elif find_strategy(st.session_state.strategies, name_clean):
                    st.error("Strategy name already exists.")
                else:
                    st.session_state.strategies.append(
                        {
                            "name": name_clean,
                            "kind": template,
                            "params": new_params,
                            "notes": new_notes,
                        }
                    )
                    save_strategy_store(st.session_state.strategies)
                    st.session_state.active_strategy = name_clean
                    st.success(f"Created {name_clean} and set as active strategy.")
                    st.rerun()


if __name__ == "__main__":
    main()
