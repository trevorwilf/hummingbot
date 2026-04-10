from sqlalchemy import BigInteger, Column, JSON, Text

from hummingbot.model import HummingbotBase


class BotRun(HummingbotBase):
    __tablename__ = "BotRun"

    id = Column(Text, primary_key=True, nullable=False)       # UUID
    started_ts_ms = Column(BigInteger, nullable=False)
    ended_ts_ms = Column(BigInteger, nullable=True)            # NULL until stopped
    strategy_name = Column(Text, nullable=True)
    config_file_path = Column(Text, nullable=True)
    config_hash = Column(Text, nullable=True)                  # SHA256 of effective config
    connectors = Column(JSON, nullable=True)                   # ["nonkyc", "mexc"]
    trading_pairs = Column(JSON, nullable=True)                # ["BTC-USDT", ...]
    db_backend = Column(Text, nullable=True)                   # "sqlite" or "postgresql"
    stop_reason = Column(Text, nullable=True)                  # "operator_stop", "crash", "restart"
    host_name = Column(Text, nullable=True)
    git_sha = Column(Text, nullable=True)                      # optional

    def __repr__(self) -> str:
        return (f"BotRun(id='{self.id}', started_ts_ms={self.started_ts_ms}, "
                f"ended_ts_ms={self.ended_ts_ms}, strategy_name='{self.strategy_name}', "
                f"stop_reason='{self.stop_reason}')")
