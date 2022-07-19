# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2022 Valory AG
#   Copyright 2018-2019 Fetch.AI Limited
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------
"""This test module contains tests for Libp2p tcp client connection."""
import asyncio
import os
import time
from itertools import permutations
from unittest import mock
from unittest.mock import Mock, call

import pytest

from aea.mail.base import Empty, Envelope
from aea.multiplexer import Multiplexer

from packages.fetchai.protocols.default.message import DefaultMessage
from packages.fetchai.protocols.default.serialization import DefaultSerializer
from packages.valory.connections.p2p_libp2p_client.connection import NodeClient, Uri

from tests.common.mocks import RegexComparator
from tests.common.utils import wait_for_condition
from tests.conftest import (
    BaseP2PLibp2pTest,
    DEFAULT_LEDGER,
    DEFAULT_LEDGER_LIBP2P_NODE,
    _make_libp2p_client_connection,
    _make_libp2p_connection,
    default_ports,
    libp2p_log_on_failure_all,
)


DEFAULT_CLIENTS_PER_NODE = 1

MockDefaultMessageProtocol = Mock()
MockDefaultMessageProtocol.protocol_id = DefaultMessage.protocol_id
MockDefaultMessageProtocol.protocol_specification_id = (
    DefaultMessage.protocol_specification_id
)


@pytest.mark.asyncio
class TestLibp2pClientConnectionConnectDisconnect(BaseP2PLibp2pTest):
    """Test that connection is established and torn down correctly"""

    @classmethod
    def setup_class(cls):
        """Set the test up"""
        super().setup_class()

        cls.delegate_port = next(default_ports)

        temp_dir = os.path.join(cls.t, "temp_dir_node")
        cls.connection_node = _make_libp2p_connection(
            data_dir=temp_dir, delegate=True, delegate_port=cls.delegate_port
        )
        temp_dir_client = os.path.join(cls.t, "temp_dir_client")
        cls.connection = _make_libp2p_client_connection(
            data_dir=temp_dir_client,
            peer_public_key=cls.connection_node.node.pub,
            node_port=cls.delegate_port,
        )

    @pytest.mark.asyncio
    async def test_libp2pclientconnection_connect_disconnect(self):
        """Test connnect then disconnect."""
        assert self.connection.is_connected is False
        try:
            await self.connection_node.connect()
            await self.connection.connect()
            assert self.connection.is_connected is True
            await self.connection.disconnect()
            assert self.connection.is_connected is False
        except Exception:
            raise
        finally:
            await self.connection_node.disconnect()


@libp2p_log_on_failure_all
class TestLibp2pClientConnectionEchoEnvelope(BaseP2PLibp2pTest):
    """Test that connection will route envelope to destination through the same libp2p node"""

    @classmethod
    def setup_class(cls):
        """Set the test up"""
        super().setup_class()

        cls.delegate_port = next(default_ports)

        temp_dir = os.path.join(cls.t, "temp_dir_node")
        cls.connection_node = _make_libp2p_connection(
            data_dir=temp_dir,
            delegate=True,
            delegate_port=cls.delegate_port,
        )
        cls.multiplexer_node = Multiplexer(
            [cls.connection_node], protocols=[MockDefaultMessageProtocol]
        )
        cls.log_files.append(cls.connection_node.node.log_file)
        cls.multiplexer_node.connect()

        try:
            temp_dir_client_1 = os.path.join(cls.t, "temp_dir_client_1")
            cls.connection_client_1 = _make_libp2p_client_connection(
                data_dir=temp_dir_client_1,
                peer_public_key=cls.connection_node.node.pub,
                ledger_api_id=DEFAULT_LEDGER_LIBP2P_NODE,
                node_port=cls.delegate_port,
            )
            cls.multiplexer_client_1 = Multiplexer(
                [cls.connection_client_1], protocols=[MockDefaultMessageProtocol]
            )
            cls.multiplexer_client_1.connect()

            temp_dir_client_2 = os.path.join(cls.t, "temp_dir_client_2")
            cls.connection_client_2 = _make_libp2p_client_connection(
                data_dir=temp_dir_client_2,
                peer_public_key=cls.connection_node.node.pub,
                ledger_api_id=DEFAULT_LEDGER,
                node_port=cls.delegate_port,
            )
            cls.multiplexer_client_2 = Multiplexer(
                [cls.connection_client_2], protocols=[MockDefaultMessageProtocol]
            )
            cls.multiplexer_client_2.connect()

            wait_for_condition(lambda: cls.connection_client_1.is_connected is True, 10)
            wait_for_condition(lambda: cls.connection_client_2.is_connected is True, 10)
        except Exception:
            cls.multiplexer_node.disconnect()
            raise

    def test_connection_is_established(self):
        """Test connection is established."""
        assert self.connection_client_1.is_connected is True
        assert self.connection_client_2.is_connected is True

    def test_envelope_routed(self):
        """Test the envelope is routed."""
        addr_1 = self.connection_client_1.address
        addr_2 = self.connection_client_2.address

        msg = DefaultMessage(
            dialogue_reference=("", ""),
            message_id=1,
            target=0,
            performative=DefaultMessage.Performative.BYTES,
            content=b"hello",
        )
        envelope = Envelope(
            to=addr_2,
            sender=addr_1,
            protocol_specification_id=DefaultMessage.protocol_specification_id,
            message=DefaultSerializer().encode(msg),
        )

        self.multiplexer_client_1.put(envelope)
        delivered_envelope = self.multiplexer_client_2.get(block=True, timeout=20)

        assert delivered_envelope is not None
        assert delivered_envelope.to == envelope.to
        assert delivered_envelope.sender == envelope.sender
        assert (
            delivered_envelope.protocol_specification_id
            == envelope.protocol_specification_id
        )
        assert delivered_envelope.message == envelope.message

    def test_envelope_echoed_back(self):
        """Test the envelope is echoed back."""
        addr_1 = self.connection_client_1.address
        addr_2 = self.connection_client_2.address

        msg = DefaultMessage(
            dialogue_reference=("", ""),
            message_id=1,
            target=0,
            performative=DefaultMessage.Performative.BYTES,
            content=b"hello",
        )
        original_envelope = Envelope(
            to=addr_2,
            sender=addr_1,
            protocol_specification_id=DefaultMessage.protocol_specification_id,
            message=DefaultSerializer().encode(msg),
        )

        self.multiplexer_client_1.put(original_envelope)
        delivered_envelope = self.multiplexer_client_2.get(block=True, timeout=10)
        assert delivered_envelope is not None

        delivered_envelope.to = addr_1
        delivered_envelope.sender = addr_2

        self.multiplexer_client_2.put(delivered_envelope)
        echoed_envelope = self.multiplexer_client_1.get(block=True, timeout=5)

        assert echoed_envelope is not None
        assert echoed_envelope.to == original_envelope.sender
        assert delivered_envelope.sender == original_envelope.to
        assert (
            delivered_envelope.protocol_specification_id
            == original_envelope.protocol_specification_id
        )
        assert delivered_envelope.message == original_envelope.message

    def test_envelope_echoed_back_node_agent(self):
        """Test the envelope is echoed back."""
        addr_1 = self.connection_client_1.address
        addr_n = self.connection_node.address

        msg = DefaultMessage(
            dialogue_reference=("", ""),
            message_id=1,
            target=0,
            performative=DefaultMessage.Performative.BYTES,
            content=b"hello",
        )
        original_envelope = Envelope(
            to=addr_n,
            sender=addr_1,
            protocol_specification_id=DefaultMessage.protocol_specification_id,
            message=DefaultSerializer().encode(msg),
        )

        self.multiplexer_client_1.put(original_envelope)
        delivered_envelope = self.multiplexer_node.get(block=True, timeout=10)
        assert delivered_envelope is not None

        delivered_envelope.to = addr_1
        delivered_envelope.sender = addr_n

        self.multiplexer_node.put(delivered_envelope)
        echoed_envelope = self.multiplexer_client_1.get(block=True, timeout=5)

        assert echoed_envelope is not None
        assert echoed_envelope.to == original_envelope.sender
        assert delivered_envelope.sender == original_envelope.to
        assert (
            delivered_envelope.protocol_specification_id
            == original_envelope.protocol_specification_id
        )
        assert delivered_envelope.message == original_envelope.message

    @classmethod
    def teardown_class(cls):
        """Tear down the test"""
        cls.multiplexer_client_1.disconnect()
        cls.multiplexer_client_2.disconnect()
        cls.multiplexer_node.disconnect()
        super().teardown_class()


@libp2p_log_on_failure_all
class TestLibp2pClientConnectionEchoEnvelopeTwoDHTNode(BaseP2PLibp2pTest):
    """Test that connection will route envelope to destination connected to different node"""

    @classmethod
    def setup_class(cls):
        """Set the test up"""
        super().setup_class()

        cls.ports = next(default_ports), next(default_ports)

        temp_dir_node_1 = os.path.join(cls.t, "temp_dir_node_1")
        cls.connection_node_1 = _make_libp2p_connection(
            data_dir=temp_dir_node_1,
            delegate_port=cls.ports[0],
            delegate=True,
        )
        cls.multiplexer_node_1 = Multiplexer(
            [cls.connection_node_1], protocols=[MockDefaultMessageProtocol]
        )
        cls.log_files.append(cls.connection_node_1.node.log_file)
        cls.multiplexer_node_1.connect()
        cls.multiplexers.append(cls.multiplexer_node_1)

        genesis_peer = cls.connection_node_1.node.multiaddrs[0]

        temp_dir_node_2 = os.path.join(cls.t, "temp_dir_node_2")
        try:
            cls.connection_node_2 = _make_libp2p_connection(
                data_dir=temp_dir_node_2,
                delegate_port=cls.ports[1],
                entry_peers=[genesis_peer],
                delegate=True,
            )
            cls.multiplexer_node_2 = Multiplexer(
                [cls.connection_node_2], protocols=[MockDefaultMessageProtocol]
            )
            cls.log_files.append(cls.connection_node_2.node.log_file)
            cls.multiplexer_node_2.connect()
            cls.multiplexers.append(cls.multiplexer_node_2)

            temp_dir_client_1 = os.path.join(cls.t, "temp_dir_client_1")
            cls.connection_client_1 = _make_libp2p_client_connection(
                data_dir=temp_dir_client_1,
                peer_public_key=cls.connection_node_1.node.pub,
                node_port=cls.ports[0],
            )
            cls.multiplexer_client_1 = Multiplexer(
                [cls.connection_client_1], protocols=[MockDefaultMessageProtocol]
            )
            cls.multiplexer_client_1.connect()
            cls.multiplexers.append(cls.multiplexer_client_1)

            temp_dir_client_2 = os.path.join(cls.t, "temp_dir_client_2")
            cls.connection_client_2 = _make_libp2p_client_connection(
                data_dir=temp_dir_client_2,
                peer_public_key=cls.connection_node_2.node.pub,
                node_port=cls.ports[1],
            )
            cls.multiplexer_client_2 = Multiplexer(
                [cls.connection_client_2], protocols=[MockDefaultMessageProtocol]
            )
            cls.multiplexer_client_2.connect()
            cls.multiplexers.append(cls.multiplexer_client_2)

            wait_for_condition(lambda: cls.connection_node_1.is_connected is True, 10)
            wait_for_condition(lambda: cls.connection_node_2.is_connected is True, 10)
            wait_for_condition(lambda: cls.connection_client_1.is_connected is True, 10)
            wait_for_condition(lambda: cls.connection_client_2.is_connected is True, 10)

        except Exception:
            cls.teardown_class()
            raise

    def test_connection_is_established(self):
        """Test the connection is established."""
        assert self.connection_node_1.is_connected is True
        assert self.connection_node_2.is_connected is True
        assert self.connection_client_1.is_connected is True
        assert self.connection_client_2.is_connected is True

    def test_envelope_routed(self):
        """Test the envelope is routed."""
        addr_1 = self.connection_client_1.address
        addr_2 = self.connection_client_2.address

        msg = DefaultMessage(
            dialogue_reference=("", ""),
            message_id=1,
            target=0,
            performative=DefaultMessage.Performative.BYTES,
            content=b"hello",
        )
        envelope = Envelope(
            to=addr_2,
            sender=addr_1,
            protocol_specification_id=DefaultMessage.protocol_specification_id,
            message=DefaultSerializer().encode(msg),
        )

        self.multiplexer_client_1.put(envelope)
        delivered_envelope = self.multiplexer_client_2.get(block=True, timeout=20)

        assert delivered_envelope is not None
        assert delivered_envelope.to == envelope.to
        assert delivered_envelope.sender == envelope.sender
        assert (
            delivered_envelope.protocol_specification_id
            == envelope.protocol_specification_id
        )
        assert delivered_envelope.message == envelope.message

    def test_envelope_echoed_back(self):
        """Test the envelope is echoed back."""
        addr_1 = self.connection_client_1.address
        addr_2 = self.connection_client_2.address

        msg = DefaultMessage(
            dialogue_reference=("", ""),
            message_id=1,
            target=0,
            performative=DefaultMessage.Performative.BYTES,
            content=b"hello",
        )
        original_envelope = Envelope(
            to=addr_2,
            sender=addr_1,
            protocol_specification_id=DefaultMessage.protocol_specification_id,
            message=DefaultSerializer().encode(msg),
        )

        self.multiplexer_client_1.put(original_envelope)
        delivered_envelope = self.multiplexer_client_2.get(block=True, timeout=10)
        assert delivered_envelope is not None

        delivered_envelope.to = addr_1
        delivered_envelope.sender = addr_2

        self.multiplexer_client_2.put(delivered_envelope)
        echoed_envelope = self.multiplexer_client_1.get(block=True, timeout=5)

        assert echoed_envelope is not None
        assert echoed_envelope.to == original_envelope.sender
        assert delivered_envelope.sender == original_envelope.to
        assert (
            delivered_envelope.protocol_specification_id
            == original_envelope.protocol_specification_id
        )
        assert delivered_envelope.message == original_envelope.message

    def test_envelope_echoed_back_node_agent(self):
        """Test the envelope is echoed back node agent."""
        addr_1 = self.connection_client_1.address
        addr_n = self.connection_node_2.address

        msg = DefaultMessage(
            dialogue_reference=("", ""),
            message_id=1,
            target=0,
            performative=DefaultMessage.Performative.BYTES,
            content=b"hello",
        )
        original_envelope = Envelope(
            to=addr_n,
            sender=addr_1,
            protocol_specification_id=DefaultMessage.protocol_specification_id,
            message=DefaultSerializer().encode(msg),
        )

        self.multiplexer_client_1.put(original_envelope)
        delivered_envelope = self.multiplexer_node_2.get(block=True, timeout=10)
        assert delivered_envelope is not None

        delivered_envelope.to = addr_1
        delivered_envelope.sender = addr_n

        self.multiplexer_node_2.put(delivered_envelope)
        echoed_envelope = self.multiplexer_client_1.get(block=True, timeout=5)

        assert echoed_envelope is not None
        assert echoed_envelope.to == original_envelope.sender
        assert delivered_envelope.sender == original_envelope.to
        assert (
            delivered_envelope.protocol_specification_id
            == original_envelope.protocol_specification_id
        )
        assert delivered_envelope.message == original_envelope.message


@libp2p_log_on_failure_all
class TestLibp2pClientConnectionRouting(BaseP2PLibp2pTest):
    """Test that libp2p DHT network will reliably route envelopes from clients connected to different nodes"""

    @classmethod
    def setup_class(cls):
        """Set the test up"""
        super().setup_class()

        cls.ports = next(default_ports), next(default_ports)

        try:
            temp_dir_node_1 = os.path.join(cls.t, "temp_dir_node_1")
            cls.connection_node_1 = _make_libp2p_connection(
                data_dir=temp_dir_node_1,
                delegate_port=cls.ports[0],
                delegate=True,
            )
            cls.multiplexer_node_1 = Multiplexer(
                [cls.connection_node_1], protocols=[MockDefaultMessageProtocol]
            )
            cls.log_files.append(cls.connection_node_1.node.log_file)
            cls.multiplexer_node_1.connect()
            cls.multiplexers.append(cls.multiplexer_node_1)

            entry_peer = cls.connection_node_1.node.multiaddrs[0]

            temp_dir_node_2 = os.path.join(cls.t, "temp_dir_node_2")
            cls.connection_node_2 = _make_libp2p_connection(
                data_dir=temp_dir_node_2,
                delegate_port=cls.ports[1],
                entry_peers=[entry_peer],
                delegate=True,
            )
            cls.multiplexer_node_2 = Multiplexer(
                [cls.connection_node_2], protocols=[MockDefaultMessageProtocol]
            )
            cls.log_files.append(cls.connection_node_2.node.log_file)
            cls.multiplexer_node_2.connect()

            cls.multiplexers.append(cls.multiplexer_node_2)

            wait_for_condition(lambda: cls.multiplexer_node_1.is_connected, 10)
            wait_for_condition(lambda: cls.multiplexer_node_2.is_connected, 10)
            wait_for_condition(lambda: cls.connection_node_1.is_connected, 10)
            wait_for_condition(lambda: cls.connection_node_2.is_connected, 10)
            cls.connections = [cls.connection_node_1, cls.connection_node_2]
            cls.addresses = [
                cls.connection_node_1.address,
                cls.connection_node_2.address,
            ]

            for j in range(DEFAULT_CLIENTS_PER_NODE):
                peers_public_keys = [
                    cls.connection_node_1.node.pub,
                    cls.connection_node_2.node.pub,
                ]
                for i, port in enumerate(cls.ports):
                    peer_public_key = peers_public_keys[i]
                    temp_dir_client = os.path.join(cls.t, f"temp_dir_client__{j}_{i}")
                    conn = _make_libp2p_client_connection(
                        data_dir=temp_dir_client,
                        peer_public_key=peer_public_key,
                        node_port=port,
                    )
                    mux = Multiplexer([conn], protocols=[MockDefaultMessageProtocol])

                    cls.connections.append(conn)
                    cls.addresses.append(conn.address)

                    mux.connect()
                    wait_for_condition(lambda: mux.is_connected, 10)
                    wait_for_condition(lambda: conn.is_connected, 10)
                    cls.multiplexers.append(mux)
                break

        except Exception:
            cls.teardown_class()
            raise

    def test_connection_is_established(self):
        """Test connection is established."""
        for conn in self.connections:
            assert conn.is_connected is True

    def test_star_routing_connectivity(self):
        """Test routing with star connectivity."""
        msg = DefaultMessage(
            dialogue_reference=("", ""),
            message_id=1,
            target=0,
            performative=DefaultMessage.Performative.BYTES,
            content=b"hello",
        )
        nodes = range(len(self.multiplexers))
        for u, v in permutations(nodes, 2):
            envelope = Envelope(
                to=self.addresses[v],
                sender=self.addresses[u],
                protocol_specification_id=DefaultMessage.protocol_specification_id,
                message=DefaultSerializer().encode(msg),
            )

            self.multiplexers[u].put(envelope)
            delivered_envelope = self.multiplexers[v].get(block=True, timeout=10)
            assert delivered_envelope is not None
            assert delivered_envelope.to == envelope.to
            assert delivered_envelope.sender == envelope.sender
            assert (
                delivered_envelope.protocol_specification_id
                == envelope.protocol_specification_id
            )
            assert delivered_envelope.message == envelope.message


def test_libp2pclientconnection_uri():
    """Test the uri."""
    uri = Uri(host="127.0.0.1")
    uri = Uri(host="127.0.0.1", port=10000)
    assert uri.host == "127.0.0.1" and uri.port == 10000


@libp2p_log_on_failure_all
class BaseTestLibp2pClientSamePeer(BaseP2PLibp2pTest):
    """Base test class for reconnection tests."""

    @classmethod
    def setup_class(cls):
        """Set the test up"""
        super().setup_class()

        cls.delegate_port = next(default_ports)

        MockDefaultMessageProtocol = Mock()
        MockDefaultMessageProtocol.protocol_id = DefaultMessage.protocol_id
        MockDefaultMessageProtocol.protocol_specification_id = (
            DefaultMessage.protocol_specification_id
        )

        temp_dir = os.path.join(cls.t, "temp_dir_node")
        cls.connection_node = _make_libp2p_connection(
            data_dir=temp_dir,
            delegate=True,
            delegate_port=cls.delegate_port,
        )
        cls.multiplexer_node = Multiplexer(
            [cls.connection_node], protocols=[MockDefaultMessageProtocol]
        )
        cls.log_files.append(cls.connection_node.node.log_file)
        cls.multiplexer_node.connect()

        try:
            temp_dir_client_1 = os.path.join(cls.t, "temp_dir_client_1")
            cls.connection_client_1 = _make_libp2p_client_connection(
                data_dir=temp_dir_client_1,
                peer_public_key=cls.connection_node.node.pub,
                ledger_api_id=DEFAULT_LEDGER_LIBP2P_NODE,
                node_port=cls.delegate_port,
            )
            cls.multiplexer_client_1 = Multiplexer(
                [cls.connection_client_1], protocols=[MockDefaultMessageProtocol]
            )
            cls.multiplexer_client_1.connect()

            temp_dir_client_2 = os.path.join(cls.t, "temp_dir_client_2")
            cls.connection_client_2 = _make_libp2p_client_connection(
                data_dir=temp_dir_client_2,
                peer_public_key=cls.connection_node.node.pub,
                ledger_api_id=DEFAULT_LEDGER,
                node_port=cls.delegate_port,
            )
            cls.multiplexer_client_2 = Multiplexer(
                [cls.connection_client_2], protocols=[MockDefaultMessageProtocol]
            )
            cls.multiplexer_client_2.connect()
            wait_for_condition(lambda: cls.multiplexer_client_2.is_connected, 20)
            wait_for_condition(lambda: cls.multiplexer_client_1.is_connected, 20)
            wait_for_condition(lambda: cls.connection_client_2.is_connected, 20)
            wait_for_condition(lambda: cls.connection_client_1.is_connected, 20)
            wait_for_condition(lambda: cls.connection_node.is_connected, 20)
        except Exception:
            cls.multiplexer_node.disconnect()
            raise

    @classmethod
    def teardown_class(cls):
        """Tear down the test"""
        cls.multiplexer_client_1.disconnect()
        cls.multiplexer_client_2.disconnect()
        cls.multiplexer_node.disconnect()
        super().teardown_class()

    def _make_envelope(
        self,
        sender_address: str,
        receiver_address: str,
        message_id: int = 1,
        target: int = 0,
    ):
        """Make an envelope for testing purposes."""
        msg = DefaultMessage(
            dialogue_reference=("", ""),
            message_id=message_id,
            target=target,
            performative=DefaultMessage.Performative.BYTES,
            content=b"hello",
        )
        envelope = Envelope(
            to=receiver_address,
            sender=sender_address,
            protocol_specification_id=DefaultMessage.protocol_specification_id,
            message=DefaultSerializer().encode(msg),
        )
        return envelope


@libp2p_log_on_failure_all
class TestLibp2pClientReconnectionSendEnvelope(BaseTestLibp2pClientSamePeer):
    """Test that connection will send envelope with error, and that reconnection fixes it."""

    def test_envelope_sent(self):
        """Test the envelope is routed."""
        addr_1 = self.connection_client_1.address
        addr_2 = self.connection_client_2.address
        envelope = self._make_envelope(addr_1, addr_2)

        # make the send to fail
        with mock.patch.object(
            self.connection_client_1.logger, "exception"
        ) as _mock_logger, mock.patch.object(
            self.connection_client_1._node_client, "_write", side_effect=Exception
        ):
            self.multiplexer_client_1.put(envelope)
            delivered_envelope = self.multiplexer_client_2.get(block=True, timeout=20)
            _mock_logger.assert_has_calls(
                [
                    call(
                        "Exception raised on message send. Try reconnect and send again."
                    )
                ]
            )

        assert delivered_envelope is not None
        assert delivered_envelope.to == envelope.to
        assert delivered_envelope.sender == envelope.sender
        assert (
            delivered_envelope.protocol_specification_id
            == envelope.protocol_specification_id
        )
        assert delivered_envelope.message == envelope.message


@libp2p_log_on_failure_all
class TestLibp2pClientReconnectionReceiveEnvelope(BaseTestLibp2pClientSamePeer):
    """Test that connection will receive envelope with error, and that reconnection fixes it."""

    def test_envelope_received(self):
        """Test the envelope is routed."""
        sender = self.connection_client_2.address
        receiver = self.connection_client_1.address
        envelope = self._make_envelope(sender, receiver)

        # make the receive to fail
        with mock.patch.object(
            self.connection_client_1.logger, "error"
        ) as _mock_logger, mock.patch.object(
            self.connection_client_1._node_client,
            "_read",
            side_effect=ConnectionError(),
        ):
            # this envelope will be lost.
            self.multiplexer_client_2.put(envelope)
            # give time to reconnect
            time.sleep(2.0)
            _mock_logger.assert_has_calls(
                [
                    call(
                        RegexComparator(
                            "Connection error:.*Try to reconnect and read again"
                        )
                    )
                ]
            )
        # proceed as usual. Now we expect the connection to have reconnected successfully
        delivered_envelope = self.multiplexer_client_1.get(block=True, timeout=20)

        assert delivered_envelope is not None
        assert delivered_envelope.to == envelope.to
        assert delivered_envelope.sender == envelope.sender
        assert (
            delivered_envelope.protocol_specification_id
            == envelope.protocol_specification_id
        )
        assert delivered_envelope.message == envelope.message


class TestLibp2pClientEnvelopeOrderSamePeer(BaseTestLibp2pClientSamePeer):
    """Test that the order of envelope is the guaranteed to be the same."""

    NB_ENVELOPES = 1000

    def test_burst_order(self):
        """Test order of envelope burst is guaranteed on receiving end."""
        addr_1 = self.connection_client_1.address
        addr_2 = self.connection_client_2.address

        sent_envelopes = [
            self._make_envelope(addr_1, addr_2, i + 1, i)
            for i in range(self.NB_ENVELOPES)
        ]
        for envelope in sent_envelopes:
            self.multiplexer_client_1.put(envelope)

        received_envelopes = []
        for _ in range(self.NB_ENVELOPES):
            envelope = self.multiplexer_client_2.get(block=True, timeout=20)
            received_envelopes.append(envelope)

        # test no new message is "created"
        with pytest.raises(Empty):
            self.multiplexer_client_2.get(block=True, timeout=1)

        assert len(sent_envelopes) == len(
            received_envelopes
        ), f"expected number of envelopes {len(sent_envelopes)}, got {len(received_envelopes)}"
        for expected, actual in zip(sent_envelopes, received_envelopes):
            assert expected.message == actual.message, (
                "message content differ; probably a wrong message "
                "ordering on the receiving end"
            )


@pytest.mark.asyncio
async def test_nodeclient_pipe_connect():
    """Test pipe.connect called on NodeClient.connect."""
    f = asyncio.Future()
    f.set_result(None)
    pipe = Mock()
    pipe.connect.return_value = f
    node_client = NodeClient(pipe, Mock())
    await node_client.connect()
    pipe.connect.assert_called_once()


@pytest.mark.asyncio
async def test_write_acn_error():
    """Test pipe.connect called on NodeClient.connect."""
    f = asyncio.Future()
    f.set_result(None)
    pipe = Mock()
    pipe.write.return_value = f
    node_client = NodeClient(pipe, Mock())
    await node_client.write_acn_status_error("some message")
    pipe.write.assert_called_once()
