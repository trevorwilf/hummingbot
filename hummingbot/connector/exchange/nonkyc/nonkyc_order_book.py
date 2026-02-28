from typing import Dict, Optional

from hummingbot.connector.exchange.nonkyc.nonkyc_utils import convert_fromiso_to_unix_timestamp
from hummingbot.core.data_type.common import TradeType
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage, OrderBookMessageType


class NonkycOrderBook(OrderBook):

    @classmethod
    def snapshot_message_from_exchange(cls,
                                       msg: Dict[str, any],
                                       timestamp: float,
                                       metadata: Optional[Dict] = None) -> OrderBookMessage:
        """
        Creates a snapshot message with the order book snapshot message
        :param msg: the response from the exchange when requesting the order book snapshot
        :param timestamp: the snapshot timestamp
        :param metadata: a dictionary with extra information to add to the snapshot data
        :return: a snapshot message with the snapshot information received from the exchange
        """
        if metadata:
            msg.update(metadata)

        bids = msg["bids"]
        asks = msg["asks"]
        # Convert dict-format entries to list format if needed
        if bids and isinstance(bids[0], dict):
            bids = [[b["price"], str(b["quantity"])] for b in bids]
        if asks and isinstance(asks[0], dict):
            asks = [[a["price"], str(a["quantity"])] for a in asks]

        return OrderBookMessage(OrderBookMessageType.SNAPSHOT, {
            "trading_pair": msg["trading_pair"],
            "update_id": int(msg.get("sequence", 0)),
            "bids": bids,
            "asks": asks
        }, timestamp=timestamp)

    @classmethod
    def diff_message_from_exchange(cls,
                                   msg: Dict[str, any],
                                   timestamp: Optional[float] = None,
                                   metadata: Optional[Dict] = None) -> OrderBookMessage:
        """
        Creates a diff message with the changes in the order book received from the exchange
        :param msg: the changes in the order book
        :param timestamp: the timestamp of the difference
        :param metadata: a dictionary with extra information to add to the difference data
        :return: a diff message with the changes in the order book notified by the exchange
        """
        if metadata:
            msg.update(metadata)
        orderbookdata = msg["params"]

        formatted_asks = [[ask['price'], str(ask['quantity'])] for ask in orderbookdata["asks"]]
        formatted_bids = [[ask['price'], str(ask['quantity'])] for ask in orderbookdata["bids"]]

        return OrderBookMessage(OrderBookMessageType.DIFF, {
            "trading_pair": msg["trading_pair"],
            "update_id": int(orderbookdata.get("sequence", 0)),
            "bids": formatted_bids,
            "asks": formatted_asks
        }, timestamp=timestamp)

    @classmethod
    def trade_message_from_exchange(cls, msg: Dict[str, any], metadata: Optional[Dict] = None,
                                     trade_index: int = 0):
        """
        Creates a trade message from a single trade entry in the exchange data.
        :param msg: the trade event details sent by the exchange
        :param metadata: a dictionary with extra information to add to trade message
        :param trade_index: index into the data array (default 0 for backward compatibility)
        :return: a trade message with the details of the trade as provided by the exchange
        """
        if metadata:
            msg.update(metadata)

        data_list = msg["params"]["data"]
        if trade_index >= len(data_list):
            trade_index = 0  # fallback to first if index out of bounds
        tradedata = data_list[trade_index]
        if "timestampms" in tradedata:
            ts = float(tradedata["timestampms"])
        else:
            ts = convert_fromiso_to_unix_timestamp(tradedata["timestamp"])
        return OrderBookMessage(OrderBookMessageType.TRADE, {
            "trading_pair": msg["trading_pair"],
            "trade_type": float(TradeType.SELL.value) if tradedata["side"] == "sell" else float(TradeType.BUY.value),
            "trade_id": tradedata["id"],
            "update_id": ts,
            "price": tradedata["price"],
            "amount": tradedata["quantity"]
        }, timestamp=ts * 1e-3)

    @classmethod
    def trade_messages_from_exchange(cls, msg: Dict[str, any], metadata: Optional[Dict] = None):
        """
        Creates trade messages for ALL trade entries in the exchange data.
        Used for snapshotTrades (up to 50 entries) and batched updateTrades.
        :param msg: the trade event details sent by the exchange
        :param metadata: a dictionary with extra information to add to trade messages
        :return: list of trade messages
        """
        if metadata:
            msg.update(metadata)
        data_list = msg.get("params", {}).get("data", [])
        messages = []
        for i in range(len(data_list)):
            messages.append(cls.trade_message_from_exchange(msg, trade_index=i))
        return messages
