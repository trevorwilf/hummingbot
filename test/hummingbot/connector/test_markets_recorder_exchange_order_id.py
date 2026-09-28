"""Order identity survives asynchronous creation and later fill/status recording."""
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from hummingbot.client.config.client_config_map import MarketDataCollectionConfigMap
from hummingbot.connector.markets_recorder import MarketsRecorder
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder
from hummingbot.core.data_type.trade_fee import AddedToCostTradeFee
from hummingbot.core.event.events import BuyOrderCreatedEvent, MarketEvent, OrderCancelledEvent, OrderFilledEvent
from hummingbot.model import HummingbotBase
from hummingbot.model.order import Order
from hummingbot.model.order_status import OrderStatus
from hummingbot.model.trade_fill import TradeFill


class MarketsRecorderExchangeOrderIdTests(IsolatedAsyncioWrapperTestCase):
    def setUp(self):
        super().setUp()
        self.engine = create_engine("sqlite:///:memory:")
        HummingbotBase.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.manager = SimpleNamespace(get_new_session=lambda: Session(bind=self.engine))
        self.market = SimpleNamespace(
            display_name="kraken", tracking_states={},
            _order_tracker=SimpleNamespace(all_orders={}, _lost_orders={}),
            add_trade_fills_from_market_recorder=MagicMock(),
            add_exchange_order_ids_from_market_recorder=MagicMock(),
            get_order_book=MagicMock(return_value=None),
        )
        shared = patch.object(MarketsRecorder, "_shared_instance", None)
        shared.start()
        self.addCleanup(shared.stop)
        self.recorder = MarketsRecorder(
            sql=self.manager, markets=[self.market], config_file_path="identity-test.yml",
            strategy_name="test", market_data_collection=MarketDataCollectionConfigMap(
                market_data_collection_enabled=False))
        self.recorder._lifecycle_writer = MagicMock()

    def _create(self, client_id="OID1", exchange_id=None):
        event = BuyOrderCreatedEvent(
            timestamp=1, type=OrderType.LIMIT, trading_pair="XPL-USD", amount=Decimal("10"),
            price=Decimal("0.1"), order_id=client_id, creation_timestamp=1, exchange_order_id=exchange_id)
        self.recorder._did_create_order(MarketEvent.BuyOrderCreated.value, self.market, event)

    def _tracked(self, client_id="OID1", exchange_id="EX-1", lost=False):
        tracked = InFlightOrder(client_order_id=client_id, exchange_order_id=exchange_id,
                                trading_pair="XPL-USD", order_type=OrderType.LIMIT,
                                trade_type=TradeType.BUY, amount=Decimal("10"),
                                price=Decimal("0.1"), creation_timestamp=1)
        orders = self.market._order_tracker._lost_orders if lost else self.market._order_tracker.all_orders
        orders[client_id] = tracked

    def _fill(self, exchange_id="EX-1"):
        event = OrderFilledEvent(timestamp=2, order_id="OID1", trading_pair="XPL-USD",
                                 trade_type=TradeType.BUY, order_type=OrderType.LIMIT,
                                 price=Decimal("0.1"), amount=Decimal("1"),
                                 trade_fee=AddedToCostTradeFee(), exchange_trade_id="TRADE-1",
                                 exchange_order_id=exchange_id)
        self.recorder._did_fill_order(MarketEvent.OrderFilled.value, self.market, event)

    def test_creation_resolves_id_from_exact_tracked_order(self):
        self._tracked()
        self._create()
        with self.manager.get_new_session() as session:
            self.assertEqual("EX-1", session.get(Order, "OID1").exchange_order_id)
            self.assertEqual("EX-1", session.query(OrderStatus).one().exchange_order_id)
        self.assertEqual("EX-1", self.recorder._lifecycle_writer.write.call_args.args[0].exchange_order_id)

    def test_later_fill_backfills_missing_id_without_duplicate_fill(self):
        self._create()
        self._fill()
        self._fill()
        with self.manager.get_new_session() as session:
            self.assertEqual("EX-1", session.get(Order, "OID1").exchange_order_id)
            self.assertEqual("EX-1", session.query(TradeFill).one().exchange_order_id)
            self.assertEqual(2, session.query(OrderStatus).count())
        self.market.add_exchange_order_ids_from_market_recorder.assert_any_call({"EX-1": "OID1"})

    def test_fill_uses_tracker_id_when_event_omits_it(self):
        self._create()
        self._tracked()
        self._fill(exchange_id=None)
        with self.manager.get_new_session() as session:
            self.assertEqual("EX-1", session.get(Order, "OID1").exchange_order_id)
            self.assertEqual("EX-1", session.query(TradeFill).one().exchange_order_id)

    def test_terminal_event_backfills_even_after_tracker_eviction(self):
        self._create()
        event = OrderCancelledEvent(timestamp=2, order_id="OID1", exchange_order_id="EX-1")
        self.recorder._update_order_status(MarketEvent.OrderCancelled.value, self.market, event)
        with self.manager.get_new_session() as session:
            self.assertEqual("EX-1", session.get(Order, "OID1").exchange_order_id)
            status = session.query(OrderStatus).filter_by(status="OrderCancelled").one()
            self.assertEqual("EX-1", status.exchange_order_id)
        self.assertEqual("EX-1", self.recorder._lifecycle_writer.write.call_args.args[0].exchange_order_id)

    def test_terminal_event_can_resolve_cached_or_lost_order_id(self):
        for lost in (False, True):
            with self.subTest(lost=lost):
                client_id = f"OID-{lost}"
                self._create(client_id)
                self._tracked(client_id, lost=lost)
                event = OrderCancelledEvent(timestamp=2, order_id=client_id)
                self.recorder._update_order_status(MarketEvent.OrderCancelled.value, self.market, event)
                with self.manager.get_new_session() as session:
                    self.assertEqual("EX-1", session.get(Order, client_id).exchange_order_id)

    def test_existing_order_id_is_not_overwritten_by_fill_or_terminal_event(self):
        self._create(exchange_id="EX-ORIGINAL")
        self._fill(exchange_id="EX-CONFLICT")
        event = OrderCancelledEvent(timestamp=3, order_id="OID1", exchange_order_id="EX-CONFLICT")
        self.recorder._update_order_status(MarketEvent.OrderCancelled.value, self.market, event)
        with self.manager.get_new_session() as session:
            self.assertEqual("EX-ORIGINAL", session.get(Order, "OID1").exchange_order_id)

    def test_unknown_id_stays_missing_and_other_orders_are_not_used(self):
        self._tracked(client_id="OTHER")
        self._create()
        event = OrderCancelledEvent(timestamp=2, order_id="OID1")
        self.recorder._update_order_status(MarketEvent.OrderCancelled.value, self.market, event)
        with self.manager.get_new_session() as session:
            self.assertIsNone(session.get(Order, "OID1").exchange_order_id)
            self.assertTrue(all(status.exchange_order_id is None for status in session.query(OrderStatus)))
        self.assertNotIn(None, {
            key for call in self.market.add_exchange_order_ids_from_market_recorder.call_args_list
            for key in call.args[0]})
