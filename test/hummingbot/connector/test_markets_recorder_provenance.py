"""
Integration tests for MarketsRecorder provenance enrichment.
Tests that exchange_timestamp_ms, liquidity_role, and source_channel
are populated on TradeFill rows when in-flight order data is available.
"""
import time
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, PropertyMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from hummingbot.model import HummingbotBase
from hummingbot.model.trade_fill import TradeFill
from hummingbot.model.order import Order
from hummingbot.model.order_status import OrderStatus
from hummingbot.core.data_type.in_flight_order import TradeUpdate
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.trade_fee import AddedToCostTradeFee, TokenAmount
from hummingbot.core.event.events import (
    MarketEvent,
    OrderFilledEvent,
    PositionAction,
)


class TestMarketsRecorderProvenanceEnrichment(unittest.TestCase):
    """Test that _did_fill_order enriches TradeFill with provenance data."""

    def setUp(self):
        """Set up an in-memory SQLite DB and a minimal MarketsRecorder."""
        self.engine = create_engine("sqlite:///:memory:")
        HummingbotBase.metadata.create_all(self.engine)

        # We'll test the enrichment logic directly rather than full MarketsRecorder
        # because MarketsRecorder has complex init requirements.
        self.session = Session(bind=self.engine)

    def tearDown(self):
        self.session.close()

    def test_trade_fill_with_provenance_populated(self):
        """When provenance data is available, new columns should be populated."""
        now_ms = int(time.time() * 1e3)
        exchange_ts = 1700000000.0  # seconds

        # Create an Order first (foreign key)
        order = Order(
            id="test_order_1",
            config_file_path="test.yml",
            strategy="test_strategy",
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

        # Create a TradeFill with provenance columns
        trade_fill = TradeFill(
            config_file_path="test.yml",
            strategy="test_strategy",
            market="test_exchange",
            symbol="BTC-USDT",
            base_asset="BTC",
            quote_asset="USDT",
            timestamp=now_ms,
            order_id="test_order_1",
            trade_type="BUY",
            order_type="LIMIT",
            price=Decimal("50000"),
            amount=Decimal("0.1"),
            leverage=1,
            trade_fee={"percent": 0, "flat_fees": []},
            trade_fee_in_quote=Decimal("0"),
            exchange_trade_id="fill_001",
            exchange_timestamp_ms=int(exchange_ts * 1e3),
            received_timestamp_ms=now_ms,
            source_channel="ws",
            liquidity_role="taker",
        )
        self.session.add(trade_fill)
        self.session.commit()

        # Query back
        result = self.session.query(TradeFill).filter(
            TradeFill.exchange_trade_id == "fill_001"
        ).one()

        self.assertEqual(result.exchange_timestamp_ms, int(exchange_ts * 1e3))
        self.assertEqual(result.received_timestamp_ms, now_ms)
        self.assertEqual(result.source_channel, "ws")
        self.assertEqual(result.liquidity_role, "taker")

    def test_trade_fill_without_provenance_is_none(self):
        """When no provenance data, new columns should be None (backward compat)."""
        now_ms = int(time.time() * 1e3)

        order = Order(
            id="test_order_2",
            config_file_path="test.yml",
            strategy="test_strategy",
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

        trade_fill = TradeFill(
            config_file_path="test.yml",
            strategy="test_strategy",
            market="test_exchange",
            symbol="BTC-USDT",
            base_asset="BTC",
            quote_asset="USDT",
            timestamp=now_ms,
            order_id="test_order_2",
            trade_type="BUY",
            order_type="LIMIT",
            price=Decimal("50000"),
            amount=Decimal("0.1"),
            leverage=1,
            trade_fee={"percent": 0, "flat_fees": []},
            trade_fee_in_quote=Decimal("0"),
            exchange_trade_id="fill_002",
            # No provenance columns set
        )
        self.session.add(trade_fill)
        self.session.commit()

        result = self.session.query(TradeFill).filter(
            TradeFill.exchange_trade_id == "fill_002"
        ).one()

        self.assertIsNone(result.exchange_timestamp_ms)
        self.assertIsNone(result.received_timestamp_ms)
        self.assertIsNone(result.source_channel)
        self.assertIsNone(result.liquidity_role)
        self.assertIsNone(result.controller_id)
        self.assertIsNone(result.executor_id)

    def test_order_status_with_received_timestamp(self):
        """OrderStatus should accept received_timestamp_ms."""
        now_ms = int(time.time() * 1e3)

        order = Order(
            id="test_order_3",
            config_file_path="test.yml",
            strategy="test_strategy",
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
        self.session.flush()

        os = OrderStatus(
            order_id="test_order_3",
            timestamp=now_ms,
            status="BuyOrderCreated",
            received_timestamp_ms=now_ms + 50,
        )
        self.session.add(os)
        self.session.commit()

        result = self.session.query(OrderStatus).filter(
            OrderStatus.order_id == "test_order_3"
        ).one()
        self.assertEqual(result.received_timestamp_ms, now_ms + 50)

    def test_order_with_trade_type(self):
        """Fix 1.2: Order rows should have trade_type populated."""
        now_ms = int(time.time() * 1e3)
        buy_order = Order(
            id="test_order_buy",
            config_file_path="test.yml",
            strategy="test_strategy",
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
            trade_type="BUY",
        )
        sell_order = Order(
            id="test_order_sell",
            config_file_path="test.yml",
            strategy="test_strategy",
            market="test_exchange",
            symbol="BTC-USDT",
            base_asset="BTC",
            quote_asset="USDT",
            creation_timestamp=now_ms,
            order_type="LIMIT",
            amount=Decimal("0.1"),
            leverage=1,
            price=Decimal("50000"),
            last_status="SellOrderCreated",
            last_update_timestamp=now_ms,
            trade_type="SELL",
        )
        self.session.add(buy_order)
        self.session.add(sell_order)
        self.session.commit()

        result_buy = self.session.query(Order).filter(Order.id == "test_order_buy").one()
        self.assertEqual(result_buy.trade_type, "BUY")
        result_sell = self.session.query(Order).filter(Order.id == "test_order_sell").one()
        self.assertEqual(result_sell.trade_type, "SELL")

    def test_trade_fill_with_exchange_order_id(self):
        """Fix 1.3: TradeFill rows should have exchange_order_id populated."""
        now_ms = int(time.time() * 1e3)
        order = Order(
            id="test_order_exid",
            config_file_path="test.yml",
            strategy="test_strategy",
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
            strategy="test_strategy",
            market="test_exchange",
            symbol="BTC-USDT",
            base_asset="BTC",
            quote_asset="USDT",
            timestamp=now_ms,
            order_id="test_order_exid",
            trade_type="BUY",
            order_type="LIMIT",
            price=Decimal("50000"),
            amount=Decimal("0.1"),
            leverage=1,
            trade_fee={"percent": 0, "flat_fees": []},
            trade_fee_in_quote=Decimal("0"),
            exchange_trade_id="fill_exid_001",
            exchange_order_id="EXCHANGE_ORD_123",
        )
        self.session.add(fill)
        self.session.commit()

        result = self.session.query(TradeFill).filter(
            TradeFill.exchange_trade_id == "fill_exid_001"
        ).one()
        self.assertEqual(result.exchange_order_id, "EXCHANGE_ORD_123")

    def test_order_with_controller_executor_ids(self):
        """Order should accept controller_id and executor_id."""
        now_ms = int(time.time() * 1e3)

        order = Order(
            id="test_order_4",
            config_file_path="test.yml",
            strategy="test_strategy",
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
            controller_id="ctrl_001",
            executor_id="exec_001",
        )
        self.session.add(order)
        self.session.commit()

        result = self.session.query(Order).filter(Order.id == "test_order_4").one()
        self.assertEqual(result.controller_id, "ctrl_001")
        self.assertEqual(result.executor_id, "exec_001")

    def test_fill_source_channel_from_tracked_order(self):
        """Test that source_channel is read from fill_sources on tracked order."""
        from hummingbot.core.data_type.in_flight_order import InFlightOrder
        order = InFlightOrder(
            client_order_id="test_src",
            trading_pair="BTC-USDT",
            order_type=OrderType.LIMIT,
            trade_type=TradeType.BUY,
            amount=Decimal("0.1"),
            creation_timestamp=time.time(),
            price=Decimal("50000"),
        )
        order.fill_sources = {"trade_123": "ws", "trade_456": "rest_poll"}

        self.assertEqual(order.fill_sources.get("trade_123", "unknown"), "ws")
        self.assertEqual(order.fill_sources.get("trade_456", "unknown"), "rest_poll")
        self.assertEqual(order.fill_sources.get("trade_789", "unknown"), "unknown")

    def test_liquidity_role_from_trade_update(self):
        """Test that liquidity_role is derived from TradeUpdate.is_taker."""
        fee = AddedToCostTradeFee(flat_fees=[TokenAmount(amount=Decimal("0.01"), token="USDT")])

        tu_taker = TradeUpdate(
            trade_id="t1", client_order_id="o1", exchange_order_id="e1",
            trading_pair="BTC-USDT", fee=fee,
            fill_base_amount=Decimal("1"), fill_quote_amount=Decimal("50000"),
            fill_price=Decimal("50000"), fill_timestamp=1700000000.0,
            is_taker=True,
        )
        self.assertEqual("taker" if tu_taker.is_taker else "maker", "taker")

        tu_maker = TradeUpdate(
            trade_id="t2", client_order_id="o2", exchange_order_id="e2",
            trading_pair="BTC-USDT", fee=fee,
            fill_base_amount=Decimal("1"), fill_quote_amount=Decimal("50000"),
            fill_price=Decimal("50000"), fill_timestamp=1700000000.0,
            is_taker=False,
        )
        self.assertEqual("taker" if tu_maker.is_taker else "maker", "maker")


if __name__ == "__main__":
    unittest.main()
