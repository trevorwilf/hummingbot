import inspect

from sqlalchemy import BigInteger, Integer, JSON, Column, Index, Text

from hummingbot.model import HummingbotBase
from hummingbot.model.decimal_type_decorator import SqliteDecimal


class MarketData(HummingbotBase):
    __tablename__ = "MarketData"
    __table_args__ = (
        Index("md_timestamp_exchange_pair", "timestamp", "exchange", "trading_pair"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(SqliteDecimal(6), nullable=False)
    exchange = Column(Text, nullable=False)
    trading_pair = Column(Text, nullable=False)
    mid_price = Column(SqliteDecimal(6), nullable=False)
    best_bid = Column(SqliteDecimal(6), nullable=False)
    best_ask = Column(SqliteDecimal(6), nullable=False)
    order_book = Column(JSON)

    def __repr__(self) -> str:
        list_of_fields = [f"{name}: {value}" for name, value in inspect.getmembers(self) if isinstance(value, Column)]
        return ','.join(list_of_fields)
