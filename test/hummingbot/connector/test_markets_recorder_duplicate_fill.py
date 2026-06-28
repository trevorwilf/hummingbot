"""Belt-and-suspenders safety test: a duplicate TradeFill insert (TradeFill_pkey UniqueViolation)
inside markets_recorder._did_fill_order must be caught, the transaction rolled back, and the
save_market_states checkpoint must still be written (previously the violation aborted the whole
transaction, silently skipping the checkpoint)."""
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from unittest.mock import patch

from sqlalchemy import create_engine

from hummingbot.client.config.client_config_map import ClientConfigMap, MarketDataCollectionConfigMap
from hummingbot.client.config.config_helpers import ClientConfigAdapter
from hummingbot.connector.markets_recorder import MarketsRecorder
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.trade_fee import AddedToCostTradeFee
from hummingbot.core.event.events import BuyOrderCreatedEvent, MarketEvent, OrderFilledEvent
from hummingbot.model.market_state import MarketState
from hummingbot.model.sql_connection_manager import SQLConnectionManager, SQLConnectionType
from hummingbot.model.trade_fill import TradeFill


class DuplicateFillSafetyTests(IsolatedAsyncioWrapperTestCase):
    # This test object doubles as the `market` passed to MarketsRecorder.

    @patch("hummingbot.model.sql_connection_manager.create_engine")
    def setUp(self, engine_mock) -> None:
        super().setUp()
        self.display_name = "test_market"
        self.config_file_path = "test_config"
        self.strategy_name = "test_strategy"
        self.trading_pair = "COINALPHA-HBOT"
        self.tracking_states = {"checkpoint": "v1"}
        engine_mock.return_value = create_engine("sqlite:///:memory:")
        self.manager = SQLConnectionManager(
            ClientConfigAdapter(ClientConfigMap()), SQLConnectionType.TRADE_FILLS, db_name="dup_fill_DB")

    # market-interface methods used by the recorder
    def add_trade_fills_from_market_recorder(self, current_trade_fills):
        pass

    def add_exchange_order_ids_from_market_recorder(self, current_exchange_order_ids):
        pass

    def _recorder(self):
        return MarketsRecorder(
            sql=self.manager,
            markets=[self],
            config_file_path=self.config_file_path,
            strategy_name=self.strategy_name,
            market_data_collection=MarketDataCollectionConfigMap(
                market_data_collection_enabled=False,
                market_data_collection_interval=60,
                market_data_collection_depth=20,
            ),
        )

    @staticmethod
    def _fill_event(trading_pair):
        return OrderFilledEvent(
            timestamp=2.0, order_id="OID1", trading_pair=trading_pair, trade_type=TradeType.BUY,
            order_type=OrderType.LIMIT, price=Decimal(1010), amount=Decimal(1),
            trade_fee=AddedToCostTradeFee(), exchange_trade_id="T1")

    def test_duplicate_fill_is_caught_and_checkpoint_still_saved(self):
        recorder = self._recorder()
        recorder._did_create_order(
            MarketEvent.BuyOrderCreated.value, self,
            BuyOrderCreatedEvent(timestamp=1.0, type=OrderType.LIMIT, trading_pair=self.trading_pair,
                                 amount=Decimal(1), price=Decimal(1000), order_id="OID1",
                                 creation_timestamp=1.0, exchange_order_id="EOID1"))

        # First fill: persists the TradeFill + the v1 checkpoint.
        recorder._did_fill_order(MarketEvent.OrderFilled.value, self, self._fill_event(self.trading_pair))

        # Advance the would-be checkpoint, then replay the SAME fill (same market/order_id/exchange_trade_id).
        # The duplicate insert raises IntegrityError; it MUST be caught (no propagation) and the v2
        # checkpoint MUST still be written by the recovery path.
        self.tracking_states = {"checkpoint": "v2"}
        try:
            recorder._did_fill_order(MarketEvent.OrderFilled.value, self, self._fill_event(self.trading_pair))
        except Exception as e:  # pragma: no cover - the whole point is that this never happens
            self.fail(f"duplicate fill must be non-fatal, but raised: {e!r}")

        with self.manager.get_new_session() as session:
            fills = session.query(TradeFill).all()
            states = session.query(MarketState).all()

        self.assertEqual(1, len(fills), "the duplicate must NOT insert a second TradeFill row")
        self.assertEqual(1, len(states))
        self.assertEqual({"checkpoint": "v2"}, states[0].saved_state,
                         "save_market_states must still run on the duplicate path (checkpoint preserved)")

    def test_non_duplicate_integrity_error_propagates(self):
        # SAFETY for all connectors (MEXC/Kraken/...): the handler must ONLY swallow the duplicate
        # TradeFill case. A different IntegrityError (e.g. a NOT NULL / FK violation on another table)
        # MUST propagate unchanged -- never be silently masked as a duplicate.
        from sqlalchemy.exc import IntegrityError

        recorder = self._recorder()
        recorder._did_create_order(
            MarketEvent.BuyOrderCreated.value, self,
            BuyOrderCreatedEvent(timestamp=1.0, type=OrderType.LIMIT, trading_pair=self.trading_pair,
                                 amount=Decimal(1), price=Decimal(1000), order_id="OID1",
                                 creation_timestamp=1.0, exchange_order_id="EOID1"))

        non_duplicate = IntegrityError(
            "INSERT INTO \"Order\"", {}, Exception("NOT NULL constraint failed: Order.bot_run_id"))
        with patch.object(recorder, "save_market_states", side_effect=non_duplicate):
            with self.assertRaises(IntegrityError):
                recorder._did_fill_order(MarketEvent.OrderFilled.value, self,
                                         self._fill_event(self.trading_pair))
