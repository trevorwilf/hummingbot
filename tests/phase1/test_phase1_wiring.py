"""
Phase 1 wiring smoke test. No network, no bot startup, no MQTT, no DB.
Mirrors the logic of StrategyV2Base.load_controller_configs() to prove that
a loader YAML -> controller YAML -> controller config class round-trip works.
"""
import importlib
import inspect
import pathlib
import sys

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from hummingbot.strategy_v2.controllers.controller_base import ControllerConfigBase  # noqa: E402
from hummingbot.strategy_v2.controllers.directional_trading_controller_base import (  # noqa: E402
    DirectionalTradingControllerConfigBase,
)
from hummingbot.strategy_v2.controllers.market_making_controller_base import (  # noqa: E402
    MarketMakingControllerConfigBase,
)

LOADER_YAMLS = [
    REPO_ROOT / "conf" / "scripts" / "conf_v2_with_controllers_nonkyc_xmr_usdt_mr.yml",
    REPO_ROOT / "conf" / "scripts" / "conf_v2_with_controllers_nonkyc_xmr_usdt_ema.yml",
]


def _load_yaml(path: pathlib.Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


@pytest.mark.parametrize("loader_path", LOADER_YAMLS, ids=lambda p: p.name)
def test_loader_yaml_shape(loader_path):
    data = _load_yaml(loader_path)
    assert data.get("script_file_name") == "v2_with_controllers.py"
    cc = data.get("controllers_config")
    assert isinstance(cc, list) and len(cc) == 1
    assert cc[0].endswith(".yml")


@pytest.mark.parametrize("loader_path", LOADER_YAMLS, ids=lambda p: p.name)
def test_controller_yaml_resolves_and_instantiates(loader_path):
    loader = _load_yaml(loader_path)
    for controller_rel in loader["controllers_config"]:
        controller_path = REPO_ROOT / "conf" / "controllers" / controller_rel
        assert controller_path.exists(), f"Controller YAML not found: {controller_path}"

        cdata = _load_yaml(controller_path)
        ctype = cdata["controller_type"]
        cname = cdata["controller_name"]
        module = importlib.import_module(f"controllers.{ctype}.{cname}")

        config_class = next(
            (
                member
                for _, member in inspect.getmembers(module)
                if inspect.isclass(member)
                and issubclass(member, ControllerConfigBase)
                and member
                not in (
                    ControllerConfigBase,
                    MarketMakingControllerConfigBase,
                    DirectionalTradingControllerConfigBase,
                )
            ),
            None,
        )
        assert config_class is not None, f"No config class in controllers.{ctype}.{cname}"
        instance = config_class(**cdata)

        assert instance.id == cdata["id"]
        assert instance.controller_name == cname
        assert instance.connector_name == cdata["connector_name"]
        assert instance.trading_pair == cdata["trading_pair"]


def test_mr_and_ema_have_distinct_ids():
    mr = _load_yaml(REPO_ROOT / "conf" / "controllers" / "nonkyc_xmr_usdt_mean_reversion_bb_rsi_v1.yml")
    ema = _load_yaml(REPO_ROOT / "conf" / "controllers" / "nonkyc_xmr_usdt_ema_regime_hold_v1.yml")
    assert mr["id"] != ema["id"]
    assert mr["controller_name"] == "mean_reversion_bb_rsi_v1"
    assert ema["controller_name"] == "ema_regime_hold_v1"


def test_loader_yamls_reference_distinct_controller_configs():
    mr_loader = _load_yaml(REPO_ROOT / "conf" / "scripts" / "conf_v2_with_controllers_nonkyc_xmr_usdt_mr.yml")
    ema_loader = _load_yaml(REPO_ROOT / "conf" / "scripts" / "conf_v2_with_controllers_nonkyc_xmr_usdt_ema.yml")
    assert mr_loader["controllers_config"] != ema_loader["controllers_config"]
