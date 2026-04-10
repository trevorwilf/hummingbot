from sqlalchemy import BigInteger, Column, Index, Integer, JSON, Text

from hummingbot.model import HummingbotBase


class OrderLifecycleEvent(HummingbotBase):
    __tablename__ = "OrderLifecycleEvent"
    __table_args__ = (
        Index("ole_client_order_id_idx", "client_order_id"),
        Index("ole_bot_run_id_idx", "bot_run_id"),
        Index("ole_event_type_idx", "event_type"),
        Index("ole_connector_pair_idx", "connector", "trading_pair"),
        Index("ole_emitted_ts_idx", "emitted_ts_ms"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Text, nullable=False, unique=True)            # UUID — dedup key
    bot_run_id = Column(Text, nullable=True)
    event_type = Column(Text, nullable=False)                       # submit_requested, submit_acked, fill, etc.
    event_version = Column(Integer, nullable=False, default=1)
    emitted_ts_ms = Column(BigInteger, nullable=False)              # local emit time
    exchange_ts_ms = Column(BigInteger, nullable=True)              # exchange-reported time
    received_ts_ms = Column(BigInteger, nullable=True)              # local receive time
    connector = Column(Text, nullable=True)
    trading_pair = Column(Text, nullable=True)
    client_order_id = Column(Text, nullable=True)
    exchange_order_id = Column(Text, nullable=True)
    exchange_trade_id = Column(Text, nullable=True)                 # only for fill events
    controller_id = Column(Text, nullable=True)
    executor_id = Column(Text, nullable=True)
    level_id = Column(Text, nullable=True)
    trade_type = Column(Text, nullable=True)                        # BUY/SELL
    order_type = Column(Text, nullable=True)                        # LIMIT/MARKET
    position_action = Column(Text, nullable=True)                   # OPEN/CLOSE/NIL
    price = Column(Text, nullable=True)                             # string-encoded
    amount = Column(Text, nullable=True)                            # string-encoded
    cum_fill_qty = Column(Text, nullable=True)                      # cumulative base filled
    fee_json = Column(JSON, nullable=True)
    fee_in_quote = Column(Text, nullable=True)
    liquidity_role = Column(Text, nullable=True)                    # maker/taker/unknown
    source_channel = Column(Text, nullable=True)                    # ws/rest_poll/etc
    best_bid = Column(Text, nullable=True)                          # quote context
    best_ask = Column(Text, nullable=True)
    mid_price = Column(Text, nullable=True)
    spread_bps = Column(Text, nullable=True)
    payload = Column(JSON, nullable=True)                           # event-specific extras

    def __repr__(self) -> str:
        return (f"OrderLifecycleEvent(id={self.id}, event_id='{self.event_id}', "
                f"event_type='{self.event_type}', client_order_id='{self.client_order_id}', "
                f"connector='{self.connector}', trading_pair='{self.trading_pair}')")
