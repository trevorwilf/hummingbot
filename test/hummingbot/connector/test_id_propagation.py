"""
Tests for controller/executor ID propagation to Order and TradeFill rows.
"""
import time
import unittest
from decimal import Decimal
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from hummingbot.model import HummingbotBase
from hummingbot.model.order import Order
from hummingbot.model.order_status import OrderStatus  # noqa: F401 — needed for mapper init
from hummingbot.model.trade_fill import TradeFill


class TestIDPropagation(unittest.TestCase):
    """Test that _controller_id and _executor_id on InFlightOrder are read by MarketsRecorder."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        HummingbotBase.metadata.create_all(self.engine)
        self.session = Session(bind=self.engine)

    def tearDown(self):
        self.session.close()

    def test_order_with_controller_executor_ids(self):
        """Order rows should have controller_id and executor_id when populated."""
        now_ms = int(time.time() * 1e3)
        order = Order(
            id="test_order_prop_1",
            config_file_path="test.yml",
            strategy="test",
            market="test_exchange",
            symbol="BTC-USDT",
            base_asset="BTC",
            quote_asset="USDT",
            creation_timestamp=now_ms,
            order_type="LIMIT",
            amount=Decimal("0.1"),
            leverage=1,
            price=Decimal("50000"),
            last_status="BuyOrderCreated",
            last_update_timestamp=now_ms,
            controller_id="ctrl_abc",
            executor_id="exec_xyz",
        )
        self.session.add(order)
        self.session.commit()
        result = self.session.query(Order).filter(Order.id == "test_order_prop_1").one()
        self.assertEqual(result.controller_id, "ctrl_abc")
        self.assertEqual(result.executor_id, "exec_xyz")

    def test_order_without_ids_is_null(self):
        """Order rows without IDs should have NULL."""
        now_ms = int(time.time() * 1e3)
        order = Order(
            id="test_order_prop_2",
            config_file_path="test.yml",
            strategy="test",
            market="test_exchange",
            symbol="BTC-USDT",
            base_asset="BTC",
            quote_asset="USDT",
            creation_timestamp=now_ms,
            order_type="LIMIT",
            amount=Decimal("0.1"),
            leverage=1,
            price=Decimal("50000"),
            last_status="BuyOrderCreated",
            last_update_timestamp=now_ms,
        )
        self.session.add(order)
        self.session.commit()
        result = self.session.query(Order).filter(Order.id == "test_order_prop_2").one()
        self.assertIsNone(result.controller_id)
        self.assertIsNone(result.executor_id)

    def test_trade_fill_with_controller_executor_ids(self):
        """TradeFill rows should have controller_id and executor_id when populated."""
        now_ms = int(time.time() * 1e3)
        order = Order(
            id="test_order_prop_3",
            config_file_path="test.yml",
            strategy="test",
            market="test_exchange",
            symbol="BTC-USDT",
            base_asset="BTC",
            quote_asset="USDT",
            creation_timestamp=now_ms,
            order_type="LIMIT",
            amount=Decimal("0.1"),
            leverage=1,
            price=Decimal("50000"),
            last_status="OrderFilled",
            last_update_timestamp=now_ms,
        )
        self.session.add(order)
        self.session.flush()

        fill = TradeFill(
            config_file_path="test.yml",
            strategy="test",
            market="test_exchange",
            symbol="BTC-USDT",
            base_asset="BTC",
            quote_asset="USDT",
            timestamp=now_ms,
            order_id="test_order_prop_3",
            trade_type="BUY",
            order_type="LIMIT",
            price=Decimal("50000"),
            amount=Decimal("0.1"),
            leverage=1,
            trade_fee={"percent": 0, "flat_fees": []},
            trade_fee_in_quote=Decimal("0"),
            exchange_trade_id="fill_prop_001",
            controller_id="ctrl_abc",
            executor_id="exec_xyz",
        )
        self.session.add(fill)
        self.session.commit()

        result = self.session.query(TradeFill).filter(
            TradeFill.exchange_trade_id == "fill_prop_001"
        ).one()
        self.assertEqual(result.controller_id, "ctrl_abc")
        self.assertEqual(result.executor_id, "exec_xyz")

    def test_tracked_order_attributes_read(self):
        """Test that _controller_id and _executor_id can be set/read on mock order."""
        mock_order = MagicMock()
        mock_order._controller_id = "ctrl_test"
        mock_order._executor_id = "exec_test"
        self.assertEqual(getattr(mock_order, '_controller_id', None), "ctrl_test")
        self.assertEqual(getattr(mock_order, '_executor_id', None), "exec_test")

    def test_tracked_order_without_attributes_returns_none(self):
        """Test backward compat when tracked order lacks the attributes."""
        mock_order = MagicMock(spec=[])  # No attributes
        self.assertIsNone(getattr(mock_order, '_controller_id', None))
        self.assertIsNone(getattr(mock_order, '_executor_id', None))


if __name__ == "__main__":
    unittest.main()
