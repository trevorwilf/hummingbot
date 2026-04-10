# Quick Phase 2 verification
import sys
sys.path.insert(0, r"E:\tradingsoftware\hummingbot")

# 1. Check new models exist
from hummingbot.model.bot_run import BotRun
from hummingbot.model.order_lifecycle_event import OrderLifecycleEvent
print("✓ BotRun model imported")
print(f"  Columns: {[c.name for c in BotRun.__table__.columns]}")
print("✓ OrderLifecycleEvent model imported")
print(f"  Columns: {[c.name for c in OrderLifecycleEvent.__table__.columns]}")

# 2. Check LifecycleEvent dataclass
from hummingbot.persistence.lifecycle_event import LifecycleEvent
evt = LifecycleEvent(event_type="test", connector="nonkyc", trading_pair="BTC-USDT")
print(f"✓ LifecycleEvent created: event_id={evt.event_id}, bot_run_id={evt.bot_run_id}")
print(f"  to_dict keys: {list(evt.to_dict().keys())}")

# 3. Check new columns on existing models
from hummingbot.model.order import Order
from hummingbot.model.trade_fill import TradeFill
from hummingbot.model.order_status import OrderStatus
for model, name in [(Order, "Order"), (TradeFill, "TradeFill"), (OrderStatus, "OrderStatus")]:
    cols = [c.name for c in model.__table__.columns]
    has_bot_run = "bot_run_id" in cols
    has_level = "level_id" in cols
    print(f"{'✓' if has_bot_run else '✗'} {name}.bot_run_id  {'✓' if has_level else '✗'} {name}.level_id")

# 4. Check InFlightOrder has new fields
from hummingbot.core.data_type.in_flight_order import InFlightOrder, TradeUpdate, OrderUpdate
from hummingbot.core.data_type.common import OrderType, TradeType
from decimal import Decimal
ifo = InFlightOrder("test", "BTC-USDT", OrderType.LIMIT, TradeType.BUY, Decimal("1"), 1.0)
print(f"✓ InFlightOrder.level_id = {ifo.level_id}")
print(f"✓ InFlightOrder.bot_run_id = {ifo.bot_run_id}")

# 5. Check TradeUpdate/OrderUpdate have new fields
import inspect
tu_fields = [f for f in TradeUpdate._fields]
ou_fields = [f for f in OrderUpdate._fields]
print(f"TradeUpdate fields: {tu_fields}")
print(f"OrderUpdate fields: {ou_fields}")

# 6. Check StructuredEventLogger has bot_run_id support
from hummingbot.logger.structured_event_logger import StructuredEventLogger
sel = StructuredEventLogger()
has_set = hasattr(sel, 'set_bot_run_id')
print(f"{'✓' if has_set else '✗'} StructuredEventLogger.set_bot_run_id exists")

print("\n=== Phase 2 verification complete ===")