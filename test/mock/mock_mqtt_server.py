import logging
from typing import Any, Dict

import ujson
from commlib.serializer import JSONSerializer


class FakeMQTTMessage(object):
    def __init__(self,
                 topic,
                 payload):
        self.topic = topic
        fake_payload = {
            'header': {
                'reply_to': f"test_reply/{topic}"
            },
            'data': payload
        }
        self.payload = ujson.dumps(fake_payload)


class FakeMQTTBroker:
    def __init__(self):
        self._shared_subscriptions = {}
        self._shared_received_msgs = {}
        self._transports = []

    def create_transport(self, *args, **kwargs):
        transport = FakeMQTTTransport(
            self._shared_subscriptions, self._shared_received_msgs,
            *args, **kwargs
        )
        self._transports.append(transport)
        return transport

    def publish_to_subscription(self, topic, payload):
        callback = self._shared_subscriptions[topic]
        msg = FakeMQTTMessage(topic=topic, payload=payload)
        callback(client=None,
                 userdata=None,
                 msg=msg)

    @property
    def subscriptions(self):
        return self._shared_subscriptions

    @property
    def received_msgs(self):
        return self._shared_received_msgs

    def is_msg_received(self, topic, content=None, msg_key = 'msg'):
        msg_found = False
        if topic in self.received_msgs:
            if not content:
                msg_found = True
            else:
                for msg in self.received_msgs[topic]:
                    if str(content) == str(msg[msg_key]):
                        msg_found = True
                        break
        return msg_found

    def clear(self):
        self._shared_received_msgs.clear()
        self._shared_subscriptions.clear()
        self._transports.clear()


class FakeMQTTTransport:

    def __init__(self, shared_subscriptions, shared_received_msgs, *args, **kwargs):
        self._subscriptions = shared_subscriptions
        self._received_msgs = shared_received_msgs
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    # def on_connect(self, *args, **kwargs):
    #     pass

    # def on_disconnect(self, *args, **kwargs):
    #     pass

    # def on_message(self, *args, **kwargs):
    #     pass

    def publish(self, topic: str, payload: Dict[str, Any], qos: Any = "", retain: bool = False):
        logging.info(f"\nFakeMQTT publish on\n> {topic}\n     {payload}\n")
        payload = ujson.loads(JSONSerializer.serialize(payload))
        if not self._received_msgs.get(topic):
            self._received_msgs[topic] = []
        self._received_msgs[topic].append(payload)

    def subscribe(self, topic: str, callback: Any, *args, **kwargs):
        self._subscriptions[topic] = callback
        return topic

    def start(self):
        self._connected = True

    def connect(self):
        # Do NOT set _connected here. RPCService.__init__ calls connect()
        # before run(), and BaseRPCService.run() skips run_forever() if
        # is_connected is True, which would prevent subscription registration.
        # start() is called inside run_forever() AFTER subscribing.
        pass

    def stop(self):
        self._connected = False

    def loop_forever(self):
        self.start()
