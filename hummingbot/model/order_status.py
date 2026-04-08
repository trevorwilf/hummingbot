#!/usr/bin/env python
from typing import Any, Dict

from sqlalchemy import BigInteger, Column, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import relationship

from . import HummingbotBase


class OrderStatus(HummingbotBase):
    __tablename__ = "OrderStatus"
    __table_args__ = (Index("os_order_id_timestamp_index",
                            "order_id", "timestamp"),
                      )

    id = Column(Integer, primary_key=True, nullable=False)
    order_id = Column(Text, ForeignKey("Order.id"), nullable=False)
    timestamp = Column(BigInteger, nullable=False)
    status = Column(Text, nullable=False)

    # Provenance columns — all nullable, no default (additive, backward-compatible)
    exchange_timestamp_ms = Column(BigInteger, nullable=True)    # Exchange-reported event time (ms)
    received_timestamp_ms = Column(BigInteger, nullable=True)    # Bot receive time (ms)
    source_channel = Column(Text, nullable=True)                 # "ws" | "rest_poll" | "rest_status_update"

    order = relationship("Order", back_populates="status")

    def __repr__(self) -> str:
        return f"OrderStatus(id={self.id}, order_id='{self.order_id}', timestamp={self.timestamp}, " \
            f"status='{self.status}', exchange_timestamp_ms={self.exchange_timestamp_ms}, " \
            f"received_timestamp_ms={self.received_timestamp_ms}, source_channel='{self.source_channel}')"

    @staticmethod
    def to_bounty_api_json(order_status: "OrderStatus") -> Dict[str, Any]:
        return {
            "order_id": order_status.order_id,
            "timestamp": order_status.timestamp,
            "event_type": order_status.status,
            "raw_json": {
            }
        }
