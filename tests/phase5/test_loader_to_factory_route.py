"""
Phase 5 integration test: loader YAML -> controller YAML -> controller config
-> CandlesFactory routes to the correct adapter class with the configured
max_records. No network, no WS, no REST.
"""
import importlib
import inspect
import pathlib
import sys
from unittest.mock import patch

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory  # noqa: E402
from hummingbot.data_feed.candles_feed.mexc_spot_candles.mexc_spot_candles import MexcSpotCandles  # noqa: E402
from hummingbot.data_feed.candles_feed.nonkyc_spot_candles.nonkyc_spot_candles import NonKYCSpotCandles  # noqa: E402
from hummingbot.strategy_v2.controllers.controller_base import ControllerConfigBase  # noqa: E402
from hummingbot.strategy_v2.controllers.directional_trading_controller_base import (  # noqa: E402
    DirectionalTradingControllerConfigBase,
)
from hummingbot.strategy_v2.controllers.market_making_controller_base import (  # noqa: E402
    MarketMakingControllerConfigBase,
)

LOADER_DIR = REPO_ROOT / "conf" / "scripts"
CONTROLLER_DIR = REPO_ROOT / "conf" / "controllers"


def _load(p):
    with open(p) as f:
        return yaml.safe_load(f)


def _instantiate_controller_config(controller_rel_path: str):
    cdata = _load(CONTROLLER_DIR / controller_rel_path)
    module = importlib.import_module(
        f"controllers.{cdata['controller_type']}.{cdata['controller_name']}"
    )
    cfg_cls = next(
        m for _, m in inspect.getmembers(module)
        if inspect.isclass(m)
        and issubclass(m, ControllerConfigBase)
        and m not in (ControllerConfigBase, MarketMakingControllerConfigBase,
                      DirectionalTradingControllerConfigBase)
    )
    return cfg_cls(**cdata)


@pytest.mark.parametrize(
    "loader_name,expected_adapter_cls",
    [
        ("conf_v2_with_controllers_nonkyc_xmr_usdt_mr.yml", NonKYCSpotCandles),
        ("conf_v2_with_controllers_nonkyc_xmr_usdt_ema.yml", NonKYCSpotCandles),
    ],
)
def test_full_route_loader_to_factory(loader_name, expected_adapter_cls):
    loader = _load(LOADER_DIR / loader_name)
    assert len(loader["controllers_config"]) == 1

    cfg = _instantiate_controller_config(loader["controllers_config"][0])

    assert len(cfg.candles_config) >= 1
    for cc in cfg.candles_config:
        with patch.object(expected_adapter_cls, "__init__", return_value=None) as init_mock:
            adapter = CandlesFactory.get_candle(cc)
            assert isinstance(adapter, expected_adapter_cls)
            init_mock.assert_called_once_with(cc.trading_pair, cc.interval, cc.max_records)


def test_mr_candles_config_uses_unified_required_records():
    cfg = _instantiate_controller_config("nonkyc_xmr_usdt_mean_reversion_bb_rsi_v1.yml")
    assert cfg.candles_config[0].max_records == cfg.required_records


def test_ema_candles_config_two_frames_fast_then_slow():
    cfg = _instantiate_controller_config("nonkyc_xmr_usdt_ema_regime_hold_v1.yml")
    assert len(cfg.candles_config) == 2
    assert cfg.candles_config[0].interval == cfg.signal_interval
    assert cfg.candles_config[1].interval == cfg.regime_interval


def test_mexc_is_a_valid_candle_source_after_phase4():
    from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
    cc = CandlesConfig(connector="mexc", trading_pair="XMR-USDT", interval="5m", max_records=700)
    with patch.object(MexcSpotCandles, "__init__", return_value=None) as init_mock:
        adapter = CandlesFactory.get_candle(cc)
        assert isinstance(adapter, MexcSpotCandles)
        init_mock.assert_called_once_with("XMR-USDT", "5m", 700)
