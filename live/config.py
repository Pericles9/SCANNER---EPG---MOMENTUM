"""Load and validate strategy.json. All other modules import CFG from here."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_STRATEGY_JSON = Path(__file__).parent / "strategy.json"
_SENTINEL = "REQUIRED_FROM_BACKTEST"


@dataclass
class ScannerConfig:
    gap_threshold: float
    poll_interval_s: int
    collect_scanner_heat: bool


@dataclass
class ContextFetchConfig:
    session_start_et_hour: int
    timeout_s: float
    tail_replay_sec: float
    full_replay_min_trades: int
    degraded_min_trades: int
    multi_day_runner_lookback_days: int


@dataclass
class HawkesConfig:
    mu_buy: float
    mu_sell: float
    alpha_buy_self: float
    alpha_sell_self: float
    beta: float
    refit_interval_trades: int
    rho: float
    rho_e: float


@dataclass
class EpgConfig:
    t_event_threshold: float
    lambda_v_threshold: float
    window_close_sec: float


@dataclass
class EpgGateConfig:
    variant: str
    tau_sec: float
    L_sec: float
    k_open: float
    k_close: float
    mode: str
    warmup_seconds: float


@dataclass
class SetupFilterConfig:
    q_threshold: float
    admission_bars: int
    removal_bars: int
    warmup_provisional_threshold: float
    warmup_bars: int


@dataclass
class ExitDConfig:
    enabled: bool
    theta: float
    tau_min_sec: float
    pre_market_override: bool


@dataclass
class LuldConfig:
    enabled: bool
    rth_only: bool


@dataclass
class OrderExecutionConfig:
    pre_market_limit_offset: float
    extended_exit_offset: float
    rth_order_type: str
    unfilled_cancel_sec: float


@dataclass
class PositionSizingConfig:
    mode: str
    rth_notional: float
    pre_market_notional: float
    kelly_fraction: float
    kelly_lookback_trades: int
    kelly_min_sample: int


@dataclass
class RiskConfig:
    max_daily_loss: float
    max_concurrent_positions: int
    dead_man_timeout_s: float
    auto_kill_on_daily_loss: bool


@dataclass
class DatabaseConfig:
    batch_flush_interval_s: float


@dataclass
class LoggingConfig:
    log_dir: str
    log_prefix: str
    max_bytes: int
    backup_count: int


@dataclass
class ExportConfig:
    enabled: bool


@dataclass
class TelegramConfig:
    authorised_user_id: int


@dataclass
class Config:
    strategy_id: str
    display_name: str
    scanner: ScannerConfig
    context_fetch: ContextFetchConfig
    hawkes: HawkesConfig
    epg: EpgConfig
    epg_gate: EpgGateConfig
    setup_filter: SetupFilterConfig
    exit_d: ExitDConfig
    luld: LuldConfig
    order_execution: OrderExecutionConfig
    position_sizing: PositionSizingConfig
    risk: RiskConfig
    database: DatabaseConfig
    logging: LoggingConfig
    export: ExportConfig
    telegram: TelegramConfig


def _find_sentinels(obj: dict, path: str = "") -> list[str]:
    bad = []
    for k, v in obj.items():
        if k.startswith("_"):
            continue
        cur = f"{path}.{k}" if path else k
        if isinstance(v, str) and v == _SENTINEL:
            bad.append(cur)
        elif isinstance(v, dict):
            bad.extend(_find_sentinels(v, cur))
    return bad


def load_config(path: Path = _STRATEGY_JSON) -> Config:
    with open(path) as f:
        raw = json.load(f)

    sentinels = _find_sentinels(raw)
    if sentinels:
        raise RuntimeError(
            f"strategy.json has unfilled REQUIRED_FROM_BACKTEST fields: {sentinels}. "
            "Fill from backtest calibration results before running live."
        )

    # EPG gate validation.
    eg = raw.get("epg_gate")
    if eg is None:
        raise RuntimeError("strategy.json missing required 'epg_gate' block.")
    variant = eg.get("variant")
    if variant not in ("participation_gate", "slope_gate_fss"):
        raise RuntimeError(
            f"epg_gate.variant must be 'participation_gate' or 'slope_gate_fss', got {variant!r}."
        )
    if eg.get("warmup_seconds", 0) <= 0:
        raise RuntimeError(f"epg_gate.warmup_seconds must be positive, got {eg.get('warmup_seconds')}.")
    # SlopeGate-specific constraints — only checked when that variant is active.
    if variant == "slope_gate_fss":
        if eg.get("mode") not in ("ss", "sl"):
            raise RuntimeError(f"epg_gate.mode must be 'ss' or 'sl', got {eg.get('mode')!r}.")
        if eg["mode"] == "ss" and not (eg["k_close"] < eg["k_open"]):
            raise RuntimeError(
                f"epg_gate.k_close ({eg['k_close']}) must be < k_open ({eg['k_open']}) in mode='ss'."
            )
        for _k in ("tau_sec", "L_sec"):
            if eg[_k] <= 0:
                raise RuntimeError(f"epg_gate.{_k} must be positive, got {eg[_k]}.")

    cf = raw["context_fetch"]
    ft = cf["fallback_tiers"]

    return Config(
        strategy_id=raw["strategy_id"],
        display_name=raw["display_name"],
        scanner=ScannerConfig(
            gap_threshold=raw["scanner"]["gap_threshold"],
            poll_interval_s=raw["scanner"]["poll_interval_s"],
            collect_scanner_heat=raw["scanner"]["collect_scanner_heat"],
        ),
        context_fetch=ContextFetchConfig(
            session_start_et_hour=cf["session_start_et_hour"],
            timeout_s=cf["timeout_s"],
            tail_replay_sec=cf["tail_replay_sec"],
            full_replay_min_trades=ft["full_replay_min_trades"],
            degraded_min_trades=ft["degraded_min_trades"],
            multi_day_runner_lookback_days=cf["multi_day_runner_lookback_days"],
        ),
        hawkes=HawkesConfig(
            mu_buy=raw["hawkes"]["mu_buy"],
            mu_sell=raw["hawkes"]["mu_sell"],
            alpha_buy_self=raw["hawkes"]["alpha_buy_self"],
            alpha_sell_self=raw["hawkes"]["alpha_sell_self"],
            beta=raw["hawkes"]["beta"],
            refit_interval_trades=raw["hawkes"]["refit_interval_trades"],
            rho=raw["hawkes"]["rho"],
            rho_e=raw["hawkes"]["rho_e"],
        ),
        epg=EpgConfig(
            t_event_threshold=raw["epg"]["t_event_threshold"],
            lambda_v_threshold=raw["epg"]["lambda_v_threshold"],
            window_close_sec=raw["epg"]["window_close_sec"],
        ),
        epg_gate=EpgGateConfig(
            variant=raw["epg_gate"]["variant"],
            tau_sec=raw["epg_gate"]["tau_sec"],
            L_sec=raw["epg_gate"]["L_sec"],
            k_open=raw["epg_gate"]["k_open"],
            k_close=raw["epg_gate"]["k_close"],
            mode=raw["epg_gate"]["mode"],
            warmup_seconds=raw["epg_gate"]["warmup_seconds"],
        ),
        setup_filter=SetupFilterConfig(
            q_threshold=raw["setup_filter"]["q_threshold"],
            admission_bars=raw["setup_filter"]["admission_bars"],
            removal_bars=raw["setup_filter"]["removal_bars"],
            warmup_provisional_threshold=raw["setup_filter"]["warmup_provisional_threshold"],
            warmup_bars=raw["setup_filter"]["warmup_bars"],
        ),
        exit_d=ExitDConfig(
            enabled=raw["exit_d"]["enabled"],
            theta=raw["exit_d"]["theta"],
            tau_min_sec=raw["exit_d"]["tau_min_sec"],
            pre_market_override=raw["exit_d"]["pre_market_override"],
        ),
        luld=LuldConfig(
            enabled=raw["luld"]["enabled"],
            rth_only=raw["luld"]["rth_only"],
        ),
        order_execution=OrderExecutionConfig(
            pre_market_limit_offset=raw["order_execution"]["pre_market_limit_offset"],
            extended_exit_offset=raw["order_execution"]["extended_exit_offset"],
            rth_order_type=raw["order_execution"]["rth_order_type"],
            unfilled_cancel_sec=raw["order_execution"]["unfilled_cancel_sec"],
        ),
        position_sizing=PositionSizingConfig(
            mode=raw["position_sizing"]["mode"],
            rth_notional=raw["position_sizing"]["rth_notional"],
            pre_market_notional=raw["position_sizing"]["pre_market_notional"],
            kelly_fraction=raw["position_sizing"]["kelly_fraction"],
            kelly_lookback_trades=raw["position_sizing"]["kelly_lookback_trades"],
            kelly_min_sample=raw["position_sizing"]["kelly_min_sample"],
        ),
        risk=RiskConfig(
            max_daily_loss=raw["risk"]["max_daily_loss"],
            max_concurrent_positions=raw["risk"]["max_concurrent_positions"],
            dead_man_timeout_s=raw["risk"]["dead_man_timeout_s"],
            auto_kill_on_daily_loss=raw["risk"]["auto_kill_on_daily_loss"],
        ),
        database=DatabaseConfig(
            batch_flush_interval_s=raw["database"]["batch_flush_interval_s"],
        ),
        logging=LoggingConfig(
            log_dir=raw["logging"]["log_dir"],
            log_prefix=raw["logging"]["log_prefix"],
            max_bytes=raw["logging"]["max_bytes"],
            backup_count=raw["logging"]["backup_count"],
        ),
        export=ExportConfig(
            enabled=raw["export"]["enabled"],
        ),
        telegram=TelegramConfig(
            authorised_user_id=raw["telegram"]["authorised_user_id"],
        ),
    )


CFG: Config = load_config()
