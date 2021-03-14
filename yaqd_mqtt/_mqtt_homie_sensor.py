import asyncio
from typing import Dict, Any, List, Union
from dataclasses import dataclass

import paho.mqtt.client as mqtt  # type: ignore
from yaqd_core import IsSensor, IsDaemon


def my_on_msg(client, userdata, msg):
    print(msg)


@dataclass
class HomieChannel:
    device_id: str
    node: str
    property_: str
    value: float = float("nan")
    units: Union[str, None] = None
    updated: bool = True

    def feed(self, topic, payload):
        if topic == f"{self.topic}/$unit":
            self.units = payload
        elif topic == self.topic:
            self.value = float(payload)
            self.updated = True

    @property
    def identifier(self):
        return "_".join([self.node, self.property_])

    @property
    def topic(self):
        return f"homie/{self.device_id}/{self.node}/{self.property_}"


class MQTTHomieSensor(IsSensor, IsDaemon):
    _kind = "mqtt-homie-sensor"

    def __init__(self, name, config, config_filepath):
        super().__init__(name, config, config_filepath)
        self._busy = True  # should never be released for daemons with no inputs
        self._mqtt_client = mqtt.Client()
        self._mqtt_client.on_connect = self._on_connect
        self._mqtt_client.on_disconnect = self._on_disconnect
        self._mqtt_client.on_message = self._on_message
        self._mqtt_client.connect_async(
            host=config["mqtt_host"], port=config["mqtt_port"], keepalive=config["mqtt_keepalive"]
        )
        self._mqtt_client.loop_start()
        self._mqtt_cache: Dict[str, str] = dict()
        # initialize channels
        self._homie_channels: Dict[str, HomieChannel] = dict()

    def _on_connect(self, client, userdata, flags, rc):
        self.logger.info("Connected to broker.")
        self._mqtt_client.subscribe(topic=f"homie/{self._config['device_id']}/#")

    def _on_disconnect(self, client, userdata, rc):
        self.logger.info("Disconnected from broker.")

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode()
        self._mqtt_cache[topic] = payload

        if not self._homie_channels:
            # we must wait to recieve all of the metadata before continuing
            # nodes
            if not f"homie/{self._config['device_id']}/$nodes" in self._mqtt_cache:
                return
            # properties
            for node in self._mqtt_cache[f"homie/{self._config['device_id']}/$nodes"].split(","):
                if not f"homie/{self._config['device_id']}/{node}/$properties" in self._mqtt_cache:
                    return
            # datatypes
            for node in self._mqtt_cache[f"homie/{self._config['device_id']}/$nodes"].split(","):
                for property in self._mqtt_cache[
                    f"homie/{self._config['device_id']}/{node}/$properties"
                ].split(","):
                    if (
                        f"homie/{self._config['device_id']}/{node}/{property}/$datatype"
                        not in self._mqtt_cache
                    ):
                        return
            # channels
            for node in self._mqtt_cache[f"homie/{self._config['device_id']}/$nodes"].split(","):
                for property in self._mqtt_cache[
                    f"homie/{self._config['device_id']}/{node}/$properties"
                ].split(","):
                    if (
                        not self._mqtt_cache[
                            f"homie/{self._config['device_id']}/{node}/{property}/$datatype"
                        ]
                        == "float"
                    ):
                        continue
                    channel = HomieChannel(
                        device_id=self._config["device_id"], node=node, property_=property
                    )
                    for topic, payload in self._mqtt_cache.items():
                        channel.feed(topic, payload)
                    self._homie_channels[channel.topic] = channel
            self._channel_names = [c.identifier for c in self._homie_channels.values()]
            self._channel_units = {c.identifier: c.units for c in self._homie_channels.values()}
            self.logger.info(
                f"Recognized {len(self._channel_names)} channels: {self._channel_names}."
            )
        else:
            # once everything is initialized, we can simply feed our channel objects
            for channel in self._homie_channels.values():
                channel.feed(topic, payload)

        if self._online:
            for channel in self._homie_channels.values():
                if channel.updated:
                    self._channel_units[channel.identifier] = channel.units
                    self._measured[channel.identifier] = channel.value
                    self._measurement_id += 1
                    self._measured["measurement_id"] = self._measurement_id
                    channel.updated = False
        else:
            for channel in self._homie_channels.values():
                self._measured[channel.identifier] = float("nan")
                self._measurement_id += 1
                self._measured["measurement_id"] = self._measurement_id
                channel.updated = False

    @property
    def _online(self):
        return self._mqtt_cache[f"homie/{self._config['device_id']}/$state"] == "ready"
