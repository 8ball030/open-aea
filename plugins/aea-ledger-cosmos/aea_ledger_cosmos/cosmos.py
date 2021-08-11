# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
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
"""Cosmos module wrapping the public and private key cryptography and ledger api."""
import base64
import gzip
import hashlib
import json
import logging
import os
import subprocess  # nosec
import tempfile
import time
from collections import namedtuple
from json.decoder import JSONDecodeError
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

from Crypto.Cipher import AES  # nosec
from Crypto.Protocol.KDF import scrypt  # nosec
from Crypto.Random import get_random_bytes  # nosec
from bech32 import (  # pylint: disable=wrong-import-order
    bech32_decode,
    bech32_encode,
    convertbits,
)
from cosm.auth.rest_client import AuthRestClient
from cosm.bank.rest_client import BankRestClient, QueryBalanceRequest
from cosm.common.rest_client import RestClient
from cosm.wasm.rest_client import WasmRestClient
from cosmos.auth.v1beta1.auth_pb2 import BaseAccount
from cosmos.auth.v1beta1.query_pb2 import QueryAccountRequest
from cosmos.bank.v1beta1.tx_pb2 import MsgSend
from cosmos.base.v1beta1.coin_pb2 import Coin
from cosmos.crypto.secp256k1.keys_pb2 import PubKey as ProtoPubKey
from cosmos.tx.signing.v1beta1.signing_pb2 import SignMode
from cosmos.tx.v1beta1.tx_pb2 import AuthInfo, Fee, ModeInfo, SignerInfo, Tx, TxBody
from cosmwasm.wasm.v1beta1.query_pb2 import QuerySmartContractStateRequest
from ecdsa import (  # type: ignore # pylint: disable=wrong-import-order
    SECP256k1,
    SigningKey,
    VerifyingKey,
)
from ecdsa.util import (  # type: ignore # pylint: disable=wrong-import-order
    sigencode_string_canonize,
)
from google.protobuf.any_pb2 import Any as ProtoAny
from google.protobuf.json_format import MessageToDict

from aea.common import Address, JSONLike
from aea.crypto.base import Crypto, FaucetApi, Helper, LedgerApi
from aea.crypto.helpers import KeyIsIncorrect, hex_to_bytes_for_key
from aea.exceptions import AEAEnforceError
from aea.helpers import http_requests as requests
from aea.helpers.base import try_decorator
from aea.helpers.io import open_file


_default_logger = logging.getLogger(__name__)

_COSMOS = "cosmos"
TESTNET_NAME = "testnet"
DEFAULT_FAUCET_URL = "INVALID_URL"
DEFAULT_ADDRESS = "https://cosmos.bigdipper.live"
DEFAULT_CURRENCY_DENOM = "uatom"
DEFAULT_CHAIN_ID = "cosmoshub-3"
_BYTECODE = "wasm_byte_code"
DEFAULT_CLI_COMMAND = "wasmcli"
DEFAULT_COSMWASM_VERSIONS = "<0.10"
MESSAGE_FORMAT_BY_VERSION = {
    "store": {"<0.10": "wasm/store-code", ">=0.10": "wasm/MsgStoreCode"},
    "instantiate": {
        "<0.10": "wasm/instantiate",
        ">=0.10": "wasm/MsgInstantiateContract",
    },
    "execute": {"<0.10": "wasm/execute", ">=0.10": "wasm/MsgExecuteContract"},
}


class DataEncrypt:
    """Class to encrypt/decrypt data strings with password provided."""

    @classmethod
    def _aes_encrypt(
        cls, password: str, data: bytes
    ) -> Tuple[bytes, bytes, bytes, bytes]:
        """
        Encryption schema for private keys

        :param password: plaintext password to use for encryption
        :param data: plaintext data to encrypt

        :return: encrypted data, nonce, tag, salt
        """
        key, salt = cls._password_to_key_and_salt(password)
        cipher = AES.new(key, AES.MODE_EAX)
        ciphertext, tag = cipher.encrypt_and_digest(data)  # type:ignore

        return ciphertext, cipher.nonce, tag, salt  # type:ignore

    @staticmethod
    def _password_to_key_and_salt(
        password: str, salt: Optional[bytes] = None
    ) -> Tuple[bytes, bytes]:
        salt = salt or get_random_bytes(16)
        key = scrypt(password, salt, 16, N=2 ** 14, r=8, p=1)  # type: ignore
        return key, salt  # type: ignore

    @classmethod
    def _aes_decrypt(
        cls, password: str, encrypted_data: bytes, nonce: bytes, tag: bytes, salt: bytes
    ) -> bytes:
        """
        Decryption schema for private keys.

        :param password: plaintext password used for encryption
        :param encrypted_data: data to decrypt
        :param nonce:  bytes
        :param tag:  bytes
        :param salt: bytes
        :return: decrypted data as plaintext
        """
        # Hash password
        key, _ = cls._password_to_key_and_salt(password, salt)
        cipher = AES.new(key, AES.MODE_EAX, nonce)
        try:
            decrypted_data = cipher.decrypt_and_verify(  # type:ignore
                encrypted_data, tag
            )
        except ValueError as e:
            if e.args[0] == "MAC check failed":
                raise ValueError("Decrypt error! Bad password?") from e
            raise  # pragma: nocover
        return decrypted_data

    @classmethod
    def encrypt(cls, data: bytes, password: str) -> bytes:
        """Encrypt data with password."""
        if not isinstance(data, bytes):  # pragma: nocover
            raise ValueError(f"data has to be bytes! not {type(data)}")

        encrypted_data, nonce, tag, salt = cls._aes_encrypt(password, data)

        json_data = {
            "encrypted_data": cls.bytes_encode(encrypted_data),
            "nonce": cls.bytes_encode(nonce),
            "tag": cls.bytes_encode(tag),
            "salt": cls.bytes_encode(salt),
        }
        return json.dumps(json_data).encode()

    @staticmethod
    def bytes_encode(data: bytes) -> str:
        """Encode bytes to ascii friendly string."""
        return base64.b64encode(data).decode()

    @staticmethod
    def bytes_decode(data: str) -> bytes:
        """Decode ascii friendly string to bytes."""
        return base64.b64decode(data)

    @classmethod
    def decrypt(cls, encrypted_data: bytes, password: str) -> bytes:
        """Decrypt data with password provided."""
        if not isinstance(encrypted_data, bytes):  # pragma: nocover
            raise ValueError(
                f"encrypted_data has to be str! not {type(encrypted_data)}"
            )

        try:
            json_data = json.loads(encrypted_data)
            decrypted_data = cls._aes_decrypt(
                password,
                encrypted_data=cls.bytes_decode(json_data["encrypted_data"]),
                nonce=cls.bytes_decode(json_data["nonce"]),
                tag=cls.bytes_decode(json_data["tag"]),
                salt=cls.bytes_decode(json_data["salt"]),
            )
            return decrypted_data
        except (KeyError, JSONDecodeError) as e:
            raise ValueError(f"Bad encrypted key format!: {str(e)}") from e


class CosmosHelper(Helper):
    """Helper class usable as Mixin for CosmosApi or as standalone class."""

    address_prefix = _COSMOS

    @staticmethod
    def is_transaction_settled(tx_receipt: JSONLike) -> bool:
        """
        Check whether a transaction is settled or not.

        :param tx_receipt: the receipt of the transaction.
        :return: True if the transaction has been settled, False o/w.
        """
        is_successful = False
        if tx_receipt is not None:
            code = tx_receipt.get("code", None)
            is_successful = code is None
            if not is_successful:
                _default_logger.warning(  # pragma: nocover
                    f"Transaction not settled. Raw log: {tx_receipt.get('raw_log')}"
                )
        return is_successful

    @staticmethod
    def get_code_id(tx_receipt: JSONLike) -> Optional[int]:
        """
        Retrieve the `code_id` from a transaction receipt.

        :param tx_receipt: the receipt of the transaction.
        :return: the code id, if present
        """
        code_id: Optional[int] = None
        try:
            res = [
                dic_["value"]
                for dic_ in tx_receipt["logs"][0]["events"][0]["attributes"]
                if dic_["key"] == "code_id"
            ]  # type: ignore
            code_id = int(res[0])
        except (KeyError, IndexError):  # pragma: nocover
            code_id = None
        return code_id

    @staticmethod
    def get_contract_address(tx_receipt: JSONLike) -> Optional[str]:
        """
        Retrieve the `contract_address` from a transaction receipt.

        :param tx_receipt: the receipt of the transaction.
        :return: the contract address, if present
        """
        contract_address: Optional[str] = None
        try:
            res = [
                dic_["value"]
                for dic_ in tx_receipt["logs"][0]["events"][0]["attributes"]
                if dic_["key"] == "contract_address"
            ]  # type: ignore
            contract_address = res[0]
        except (KeyError, IndexError):  # pragma: nocover
            contract_address = None
        return contract_address

    @staticmethod
    def is_transaction_valid(
        tx: JSONLike, seller: Address, client: Address, tx_nonce: str, amount: int,
    ) -> bool:
        """
        Check whether a transaction is valid or not.

        :param tx: the transaction.
        :param seller: the address of the seller.
        :param client: the address of the client.
        :param tx_nonce: the transaction nonce.
        :param amount: the amount we expect to get from the transaction.
        :return: True if the random_message is equals to tx['input']
        """
        if tx is None:
            return False  # pragma: no cover

        try:
            _tx = cast(dict, tx.get("tx", {})).get("value", {}).get("msg", [])[0]
            recovered_amount = int(_tx.get("value").get("amount")[0].get("amount"))
            sender = _tx.get("value").get("from_address")
            recipient = _tx.get("value").get("to_address")
            is_valid = (
                recovered_amount == amount and sender == client and recipient == seller
            )
        except (KeyError, IndexError):  # pragma: no cover
            is_valid = False
        return is_valid

    @staticmethod
    def generate_tx_nonce(seller: Address, client: Address) -> str:
        """
        Generate a unique hash to distinguish transactions with the same terms.

        :param seller: the address of the seller.
        :param client: the address of the client.
        :return: return the hash in hex.
        """
        time_stamp = int(time.time())
        aggregate_hash = hashlib.sha256(
            b"".join([seller.encode(), client.encode(), time_stamp.to_bytes(32, "big")])
        )
        return aggregate_hash.hexdigest()

    @classmethod
    def get_address_from_public_key(cls, public_key: str) -> str:
        """
        Get the address from the public key.

        :param public_key: the public key
        :return: str
        """
        public_key_bytes = bytes.fromhex(public_key)
        s = hashlib.new("sha256", public_key_bytes).digest()
        r = hashlib.new("ripemd160", s).digest()
        five_bit_r = convertbits(r, 8, 5)
        if five_bit_r is None:  # pragma: nocover
            raise AEAEnforceError("Unsuccessful bech32.convertbits call")
        address = bech32_encode(cls.address_prefix, five_bit_r)
        return address

    @classmethod
    def recover_message(
        cls, message: bytes, signature: str, is_deprecated_mode: bool = False
    ) -> Tuple[Address, ...]:
        """
        Recover the addresses from the hash.

        :param message: the message we expect
        :param signature: the transaction signature
        :param is_deprecated_mode: if the deprecated signing was used
        :return: the recovered addresses
        """
        public_keys = cls.recover_public_keys_from_message(message, signature)
        addresses = [
            cls.get_address_from_public_key(public_key) for public_key in public_keys
        ]
        return tuple(addresses)

    @classmethod
    def recover_public_keys_from_message(
        cls, message: bytes, signature: str, is_deprecated_mode: bool = False
    ) -> Tuple[str, ...]:
        """
        Get the public key used to produce the `signature` of the `message`

        :param message: raw bytes used to produce signature
        :param signature: signature of the message
        :param is_deprecated_mode: if the deprecated signing was used
        :return: the recovered public keys
        """
        signature_b64 = base64.b64decode(signature)
        verifying_keys = VerifyingKey.from_public_key_recovery(
            signature_b64, message, SECP256k1, hashfunc=hashlib.sha256,
        )
        public_keys = [
            verifying_key.to_string("compressed").hex()
            for verifying_key in verifying_keys
        ]
        return tuple(public_keys)

    @staticmethod
    def get_hash(message: bytes) -> str:
        """
        Get the hash of a message.

        :param message: the message to be hashed.
        :return: the hash of the message.
        """
        digest = hashlib.sha256(message).hexdigest()
        return digest

    @classmethod
    def is_valid_address(cls, address: Address) -> bool:
        """
        Check if the address is valid.

        :param address: the address to validate
        :return: whether address is valid or not
        """
        result = bech32_decode(address)
        return result != (None, None) and result[0] == cls.address_prefix

    @classmethod
    def load_contract_interface(cls, file_path: Path) -> Dict[str, str]:
        """
        Load contract interface.

        :param file_path: the file path to the interface
        :return: the interface
        """
        with open(file_path, "rb") as interface_file_cosmos:
            contract_interface = {
                _BYTECODE: str(
                    base64.b64encode(
                        gzip.compress(interface_file_cosmos.read(), 6)
                    ).decode()
                )
            }
        return contract_interface


class CosmosCrypto(Crypto[SigningKey]):
    """Class wrapping the Account Generation from Ethereum ledger."""

    identifier = _COSMOS
    helper = CosmosHelper

    def __init__(
        self, private_key_path: Optional[str] = None, password: Optional[str] = None
    ) -> None:
        """
        Instantiate an ethereum crypto object.

        :param private_key_path: the private key path of the agent
        :param password: the password to encrypt/decrypt the private key.
        """
        super().__init__(private_key_path=private_key_path, password=password)
        self._public_key = self.entity.get_verifying_key().to_string("compressed").hex()
        self._address = self.helper.get_address_from_public_key(self.public_key)

    @property
    def private_key(self) -> str:
        """
        Return a private key.

        :return: a private key string
        """
        return self.entity.to_string().hex()

    @property
    def public_key(self) -> str:
        """
        Return a public key in hex format.

        :return: a public key string in hex format
        """
        return self._public_key

    @property
    def address(self) -> str:
        """
        Return the address for the key pair.

        :return: a display_address str
        """
        return self._address

    @classmethod
    def load_private_key_from_path(
        cls, file_name: str, password: Optional[str] = None
    ) -> SigningKey:
        """
        Load a private key in hex format from a file.

        :param file_name: the path to the hex file.
        :param password: the password to encrypt/decrypt the private key.
        :return: the Entity.
        """
        private_key = cls.load(file_name, password)
        try:
            signing_key = SigningKey.from_string(
                hex_to_bytes_for_key(private_key), curve=SECP256k1
            )
        except KeyIsIncorrect as e:
            if not password:
                raise KeyIsIncorrect(
                    f"Error on key `{file_name}` load! Try to specify `password`: Error: {repr(e)} "
                ) from e
            raise KeyIsIncorrect(
                f"Error on key `{file_name}` load! Wrong password?: Error: {repr(e)} "
            ) from e
        return signing_key

    def sign_message(  # pylint: disable=unused-argument
        self, message: bytes, is_deprecated_mode: bool = False
    ) -> str:
        """
        Sign a message in bytes string form.

        :param message: the message to be signed
        :param is_deprecated_mode: if the deprecated signing is used
        :return: signature of the message in string form
        """
        signature_compact = self.entity.sign_deterministic(
            message, hashfunc=hashlib.sha256, sigencode=sigencode_string_canonize,
        )
        signature_base64_str = base64.b64encode(signature_compact).decode("utf-8")
        return signature_base64_str

    @staticmethod
    def _format_default_transaction(
        transaction: JSONLike, signature: str, base64_pbk: str
    ) -> JSONLike:
        """
        Format default CosmosSDK transaction and add signature.

        :param transaction: the transaction to be formatted
        :param signature: the transaction signature
        :param base64_pbk: the base64 formatted public key

        :return: formatted transaction with signature
        """
        pushable_tx: JSONLike = {
            "tx": {
                "msg": transaction["msgs"],
                "fee": transaction["fee"],
                "memo": transaction["memo"],
                "signatures": [
                    {
                        "signature": signature,
                        "pub_key": {
                            "type": "tendermint/PubKeySecp256k1",
                            "value": base64_pbk,
                        },
                        "account_number": transaction["account_number"],
                        "sequence": transaction["sequence"],
                    }
                ],
            },
            "mode": "async",
        }
        return pushable_tx

    @staticmethod
    def _format_wasm_transaction(
        transaction: JSONLike, signature: str, base64_pbk: str
    ) -> JSONLike:
        """
        Format CosmWasm transaction and add signature.

        :param transaction: the transaction to be formatted
        :param signature: the transaction signature
        :param base64_pbk: the base64 formatted public key

        :return: formatted transaction with signature
        """
        pushable_tx: JSONLike = {
            "type": "cosmos-sdk/StdTx",
            "value": {
                "msg": transaction["msgs"],
                "fee": transaction["fee"],
                "signatures": [
                    {
                        "pub_key": {
                            "type": "tendermint/PubKeySecp256k1",
                            "value": base64_pbk,
                        },
                        "signature": signature,
                    }
                ],
                "memo": transaction["memo"],
            },
        }
        return pushable_tx

    def sign_transaction(self, transaction: JSONLike) -> JSONLike:
        """
        Sign a transaction in bytes string form.

        :param transaction: the transaction to be signed
        :return: signed transaction
        """
        transaction_str = json.dumps(transaction, separators=(",", ":"), sort_keys=True)
        transaction_bytes = transaction_str.encode("utf-8")
        signed_transaction = self.sign_message(transaction_bytes)
        base64_pbk = base64.b64encode(bytes.fromhex(self.public_key)).decode("utf-8")

        msgs = cast(list, transaction.get("msgs", []))
        if len(msgs) == 1 and "type" in msgs[0] and "wasm" in msgs[0]["type"]:
            return self._format_wasm_transaction(
                transaction, signed_transaction, base64_pbk
            )
        return self._format_default_transaction(
            transaction, signed_transaction, base64_pbk
        )

    @classmethod
    def generate_private_key(cls) -> SigningKey:
        """Generate a key pair for cosmos network."""
        signing_key = SigningKey.generate(curve=SECP256k1)
        return signing_key

    def encrypt(self, password: str) -> str:
        """
        Encrypt the private key and return in json.

        :param password: the password to decrypt.
        :return: json string containing encrypted private key.
        """
        return DataEncrypt.encrypt(self.private_key.encode(), password).decode()

    @classmethod
    def decrypt(cls, keyfile_json: str, password: str) -> str:
        """
        Decrypt the private key and return in raw form.

        :param keyfile_json: json string containing encrypted private key.
        :param password: the password to decrypt.
        :return: the raw private key.
        """
        try:
            return DataEncrypt.decrypt(keyfile_json.encode(), password).decode()
        except UnicodeDecodeError as e:
            raise ValueError(
                "key file data can not be translated to string! bad password?"
            ) from e


class _CosmosApi(LedgerApi):
    """Class to interact with the Cosmos SDK via a HTTP APIs."""

    identifier = _COSMOS

    def __init__(self, **kwargs: Any) -> None:
        """Initialize the Cosmos ledger APIs."""
        self._api = None
        self.network_address = kwargs.pop("address", DEFAULT_ADDRESS)
        self.denom = kwargs.pop("denom", DEFAULT_CURRENCY_DENOM)
        self.chain_id = kwargs.pop("chain_id", DEFAULT_CHAIN_ID)
        self.cli_command = kwargs.pop("cli_command", DEFAULT_CLI_COMMAND)
        self.cosmwasm_version = kwargs.pop(
            "cosmwasm_version", DEFAULT_COSMWASM_VERSIONS
        )
        self.rest_client = RestClient(self.network_address)

        valid_specifiers = list(MESSAGE_FORMAT_BY_VERSION["instantiate"].keys())
        if self.cosmwasm_version not in valid_specifiers:
            raise ValueError(  # pragma: nocover
                f"Valid CosmWasm version specifiers: {valid_specifiers}. Received invalid specifier={self.cosmwasm_version}!"
            )

    @property
    def api(self) -> Any:
        """Get the underlying API object."""
        return self._api

    def get_balance(self, address: Address) -> Optional[int]:
        """Get the balance of a given account."""
        balance = self._try_get_balance(address)
        return balance

    @try_decorator(
        "Encountered exception when trying get balance: {}",
        logger_method=_default_logger.warning,
    )
    def _try_get_balance(self, address: Address) -> Optional[int]:
        bank = BankRestClient(self.rest_client)
        res = bank.Balance(QueryBalanceRequest(address=address, denom=self.denom))
        return int(res.balance.amount)

    def get_state(
        self, callable_name: str, *args: Any, **kwargs: Any
    ) -> Optional[JSONLike]:
        """
        Call a specified function on the ledger API.

        Based on the cosmos REST
        API specification, which takes a path (strings separated by '/'). The
        convention here is to define the root of the path (txs, blocks, etc.)
        as the callable_name and the rest of the path as args.

        :param callable_name: name of the callable
        :param args: positional arguments
        :param kwargs: keyword arguments
        :return: the transaction dictionary
        """
        response = self._try_get_state(callable_name, *args, **kwargs)
        return response

    @try_decorator(
        "Encountered exception when trying get state: {}",
        logger_method=_default_logger.warning,
    )
    def _try_get_state(  # pylint: disable=unused-argument
        self, callable_name: str, *args: Any, **kwargs: Any
    ) -> Optional[JSONLike]:
        """Try to call a function on the ledger API."""
        result: Optional[JSONLike] = None
        query = "/".join(args)
        url = self.network_address + f"/{callable_name}/{query}"
        response = requests.get(url=url)
        if response.status_code == 200:
            result = response.json()
        else:  # pragma: nocover
            raise ValueError("Cannot get state: {}".format(response.json()))
        return result

    def get_deploy_transaction(
        self,
        contract_interface: Dict[str, str],
        deployer_address: Address,
        **kwargs: Any,
    ) -> Optional[JSONLike]:
        """
        Get the transaction to deploy the smart contract.

        Dispatches to _get_storage_transaction and _get_init_transaction based on kwargs.

        :param contract_interface: the contract interface.
        :param deployer_address: The address that will deploy the contract.
        :param kwargs: keyword arguments.
        :return: the transaction dictionary.
        """
        denom = (
            kwargs.pop("denom") if kwargs.get("denom", None) is not None else self.denom
        )
        chain_id = (
            kwargs.pop("chain_id")
            if kwargs.get("chain_id", None) is not None
            else self.chain_id
        )
        account_number, sequence = self._try_get_account_number_and_sequence(
            deployer_address
        )
        if account_number is None or sequence is None:
            return None  # pragma: nocover
        label = kwargs.pop("label", None)
        code_id = kwargs.pop("code_id", None)
        amount = kwargs.pop("amount", None)
        init_msg = kwargs.pop("init_msg", None)
        unexpected_keys = [
            key for key in kwargs.keys() if key not in ["tx_fee", "gas", "memo"]
        ]
        if len(unexpected_keys) != 0:  # pragma: nocover
            raise ValueError(f"Unexpected keyword arguments: {unexpected_keys}")
        if label is None and code_id is None and amount is None and init_msg is None:
            return self._get_storage_transaction(
                contract_interface,
                deployer_address,
                denom,
                chain_id,
                account_number,
                sequence,
                **kwargs,
            )
        if label is None:
            raise ValueError(  # pragma: nocover
                "Missing required keyword argument `label` of type `str` for `_get_init_transaction`."
            )
        if code_id is None:
            raise ValueError(  # pragma: nocover
                "Missing required keyword argument `code_id` of type `int` for `_get_init_transaction`."
            )
        if amount is None:
            raise ValueError(  # pragma: nocover
                "Missing required keyword argument `amount` of type `int` for `_get_init_transaction`."
            )
        if init_msg is None:
            raise ValueError(  # pragma: nocover
                "Missing required keyword argument `init_msg` of type `JSONLike` `for `_get_init_transaction`."
            )
        return self._get_init_transaction(
            deployer_address,
            denom,
            chain_id,
            account_number,
            sequence,
            amount,
            code_id,
            init_msg,
            label,
            **kwargs,
        )

    def _get_storage_transaction(
        self,
        contract_interface: Dict[str, str],
        deployer_address: Address,
        denom: str,
        chain_id: str,
        account_number: int,
        sequence: int,
        tx_fee: int = 0,
        gas: int = 0,
        memo: str = "",
        source: str = "",
        builder: str = "",
    ) -> Optional[JSONLike]:
        """
        Create a CosmWasm bytecode deployment transaction.

        :param contract_interface: the contract interface.
        :param deployer_address: the deployer address.
        :param denom: the denomination.
        :param chain_id: the Chain ID of the CosmWasm transaction. Default is 1 (i.e. mainnet).
        :param account_number: the account number.
        :param sequence: the sequence number.
        :param tx_fee: the transaction fee.
        :param gas: Maximum amount of gas to be used on executing command.
        :param memo: any string comment.
        :param source: the source.
        :param builder: the builder.
        :return: the unsigned CosmWasm contract deploy message
        """
        store_msg = {
            "type": MESSAGE_FORMAT_BY_VERSION["store"][self.cosmwasm_version],
            "value": {
                "sender": deployer_address,
                "wasm_byte_code": contract_interface[_BYTECODE],
                "source": source,
                "builder": builder,
            },
        }
        tx = self._get_transaction(
            account_number, chain_id, tx_fee, denom, gas, memo, sequence, msg=store_msg,
        )
        return tx

    def _get_init_transaction(
        self,
        deployer_address: Address,
        denom: str,
        chain_id: str,
        account_number: int,
        sequence: int,
        amount: int,
        code_id: int,
        init_msg: JSONLike,
        label: str,
        tx_fee: int = 0,
        gas: int = 0,
        memo: str = "",
    ) -> Optional[JSONLike]:
        """
        Create a CosmWasm InitMsg transaction.

        :param deployer_address: the deployer address of the message initiator.
        :param denom: the name of the denomination of the contract funds
        :param chain_id: the Chain ID of the CosmWasm transaction.
        :param account_number: the account number of the deployer.
        :param sequence: the sequence of the deployer.
        :param amount: Contract's initial funds amount
        :param code_id: the ID of contract bytecode.
        :param init_msg: the InitMsg containing parameters for contract constructor.
        :param label: the label name of the contract.
        :param tx_fee: the tx fee accepted.
        :param gas: Maximum amount of gas to be used on executing command.
        :param memo: any string comment.
        :return: the unsigned CosmWasm InitMsg
        """
        if self.cosmwasm_version != DEFAULT_COSMWASM_VERSIONS and amount == 0:
            init_funds = []
        else:
            init_funds = [{"amount": str(amount), "denom": denom}]
        instantiate_msg = {
            "type": MESSAGE_FORMAT_BY_VERSION["instantiate"][self.cosmwasm_version],
            "value": {
                "sender": deployer_address,
                "code_id": str(code_id),
                "label": label,
                "init_msg": init_msg,
                "init_funds": init_funds,
            },
        }
        tx = self._get_transaction(
            account_number,
            chain_id,
            tx_fee,
            denom,
            gas,
            memo,
            sequence,
            msg=instantiate_msg,
        )
        return tx

    def get_handle_transaction(
        self,
        sender_address: Address,
        contract_address: Address,
        handle_msg: Any,
        amount: int,
        tx_fee: int,
        denom: Optional[str] = None,
        gas: int = 0,
        memo: str = "",
        chain_id: Optional[str] = None,
    ) -> Optional[JSONLike]:
        """
        Create a CosmWasm HandleMsg transaction.

        :param sender_address: the sender address of the message initiator.
        :param contract_address: the address of the smart contract.
        :param handle_msg: HandleMsg in JSON format.
        :param amount: Funds amount sent with transaction.
        :param tx_fee: the tx fee accepted.
        :param denom: the name of the denomination of the contract funds
        :param gas: Maximum amount of gas to be used on executing command.
        :param memo: any string comment.
        :param chain_id: the Chain ID of the CosmWasm transaction. Default is 1 (i.e. mainnet).
        :return: the unsigned CosmWasm HandleMsg
        """
        denom = denom if denom is not None else self.denom
        chain_id = chain_id if chain_id is not None else self.chain_id
        account_number, sequence = self._try_get_account_number_and_sequence(
            sender_address
        )
        if account_number is None or sequence is None:
            return None  # pragma: nocover
        if self.cosmwasm_version != DEFAULT_COSMWASM_VERSIONS and amount == 0:
            sent_funds = []
        else:
            sent_funds = [{"amount": str(amount), "denom": denom}]
        execute_msg = {
            "type": MESSAGE_FORMAT_BY_VERSION["execute"][self.cosmwasm_version],
            "value": {
                "sender": sender_address,
                "contract": contract_address,
                "msg": handle_msg,
                "sent_funds": sent_funds,
            },
        }
        tx = self._get_transaction(
            account_number,
            chain_id,
            tx_fee,
            denom,
            gas,
            memo,
            sequence,
            msg=execute_msg,
        )
        return tx

    @try_decorator(
        "Encountered exception when trying to execute wasm transaction: {}",
        logger_method=_default_logger.warning,
    )
    def _try_execute_wasm_transaction(
        self, tx_signed: JSONLike, signed_tx_filename: str = "tx.signed"
    ) -> Optional[str]:
        """
        Execute a CosmWasm Transaction. QueryMsg doesn't require signing.

        :param tx_signed: the signed transaction.
        :param signed_tx_filename: signed transaction file name.
        :return: the transaction digest
        """
        with tempfile.TemporaryDirectory() as tmpdirname:
            with open_file(os.path.join(tmpdirname, signed_tx_filename), "w") as f:
                f.write(json.dumps(tx_signed))

            command = [
                self.cli_command,
                "tx",
                "broadcast",
                os.path.join(tmpdirname, signed_tx_filename),
            ]

            cli_stdout = self._execute_shell_command(command)

        try:
            tx_digest_json = json.loads(cli_stdout)
            hash_ = cast(str, tx_digest_json["txhash"])
        except JSONDecodeError:  # pragma: nocover
            raise ValueError(f"JSONDecodeError for cli_stdout={cli_stdout}")
        except KeyError:  # pragma: nocover
            raise ValueError(
                f"key `txhash` not found in tx_digest_json={tx_digest_json}"
            )
        return hash_

    def execute_contract_query(
        self, contract_address: Address, query_msg: JSONLike
    ) -> Optional[JSONLike]:
        """
        Execute a CosmWasm QueryMsg. QueryMsg doesn't require signing.

        :param contract_address: the address of the smart contract.
        :param query_msg: QueryMsg in JSON format.
        :return: the message receipt
        """
        result = self._try_execute_wasm_query(contract_address, query_msg)
        return result

    @try_decorator(
        "Encountered exception when trying to execute wasm query: {}",
        logger_method=_default_logger.warning,
    )
    def _try_execute_wasm_query(
        self, contract_address: Address, query_msg: JSONLike
    ) -> Optional[JSONLike]:
        """
        Execute a CosmWasm QueryMsg. QueryMsg doesn't require signing.

        :param contract_address: the address of the smart contract.
        :param query_msg: QueryMsg in JSON format.
        :return: the message receipt
        """
        wasm_client = WasmRestClient(self.rest_client)
        request = QuerySmartContractStateRequest(
            address=contract_address, query_data=json.dumps(query_msg).encode("UTF8")
        )
        res = wasm_client.SmartContractState(request)
        return json.loads(res.data)

    def get_transfer_transaction(  # pylint: disable=arguments-differ
        self,
        sender_address: Address,
        destination_address: Address,
        amount: int,
        tx_fee: int,
        tx_nonce: str,
        sender_public_key: Optional[str] = None,
        denom: Optional[str] = None,
        gas: int = 80000,
        memo: str = "",
        chain_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[JSONLike]:
        """
        Submit a transfer transaction to the ledger.

        :param sender_address: the sender address of the payer.
        :param destination_address: the destination address of the payee.
        :param amount: the amount of wealth to be transferred.
        :param tx_fee: the transaction fee.
        :param tx_nonce: verifies the authenticity of the tx
        :param sender_public_key: the public key of the payer
        :param denom: the denomination of tx fee and amount
        :param gas: the gas used.
        :param memo: memo to include in tx.
        :param chain_id: the chain ID of the transaction.
        :param kwargs: keyword arguments.
        :return: the transfer transaction
        """
        denom = denom if denom is not None else self.denom

        amount_coins = [Coin(denom=denom, amount=str(amount))]
        tx_fee_coins = [Coin(denom=denom, amount=str(tx_fee))]

        msg_send = MsgSend(
            from_address=str(sender_address),
            to_address=str(destination_address),
            amount=amount_coins,
        )
        send_msg_packed = ProtoAny()
        send_msg_packed.Pack(msg_send, type_url_prefix="/")

        chain_id = chain_id if chain_id is not None else self.chain_id
        account_data = self._try_get_account_data(sender_address)

        # This doesn't always work
        if sender_public_key is None:
            sender_public_key = account_data.pub_key.value

        tx = self._get_transaction(
            account_numbers=[account_data.account_number],
            from_addresses=[str(sender_address)],
            pub_keys=[bytes.fromhex(sender_public_key)],
            chain_id=chain_id,
            tx_fee=tx_fee_coins,
            gas=gas,
            memo=memo,
            sequences=[account_data.sequence],
            msgs=[send_msg_packed],
        )
        return tx

    @staticmethod
    def _get_transaction(
        account_numbers: List[int],
        from_addresses: List[str],
        pub_keys: List[bytes],
        chain_id: str,
        tx_fee: List[Coin],
        gas: int,
        memo: str,
        sequences: [int],
        msgs: List[ProtoAny],
    ) -> JSONLike:
        """
        Get a transaction.

        :param account_numbers: Account numbers for each signer.
        :param from_addresses: Addresses of each sender
        :param pub_keys: Public keys of each sender
        :param chain_id: the chain ID of the transaction.
        :param tx_fee: the transaction fee.
        :param gas: the gas used.
        :param memo: memo to include in tx.
        :param sequences: Sequence for each sender.
        :param msgs: Messages to be part of transaction.

        :return: the transaction
        """

        # Get account and signer info for each sender
        signer_infos: List[SignerInfo] = []
        sign_data: JSONLike = {}
        for from_address, pub_key, sequence, account_number in zip(
            from_addresses, pub_keys, sequences, account_numbers
        ):
            from_pub_key_packed = ProtoAny()
            from_pub_key_pb = ProtoPubKey(key=pub_key)
            from_pub_key_packed.Pack(from_pub_key_pb, type_url_prefix="/")

            # Prepare auth info
            single = ModeInfo.Single(mode=SignMode.SIGN_MODE_DIRECT)
            mode_info = ModeInfo(single=single)
            signer_info = SignerInfo(
                public_key=from_pub_key_packed, mode_info=mode_info, sequence=sequence,
            )
            signer_infos.append(signer_info)

            sign_data[from_address] = {
                "account_number": account_number,
                "chain_id": chain_id,
            }

        # Prepare auth info
        auth_info = AuthInfo(
            signer_infos=signer_infos, fee=Fee(amount=tx_fee, gas_limit=gas),
        )

        # Prepare Tx body
        tx_body = TxBody()
        tx_body.memo = memo
        tx_body.messages.extend(msgs)

        # Prepare Tx
        tx = Tx(body=tx_body, auth_info=auth_info)

        return {"tx": MessageToDict(tx), "sign_data": sign_data}

    @try_decorator(
        "Encountered exception when trying to get account number and sequence: {}",
        logger_method=_default_logger.warning,
    )
    def _try_get_account_data(self, address: Address) -> BaseAccount:
        """
        Try get account number and sequence for an address.

        :param address: the address
        :return: a tuple of account number and sequence
        """
        auth = AuthRestClient(self.rest_client)
        account_response = auth.Account(QueryAccountRequest(address=address))

        account = BaseAccount()
        if account_response.account.Is(BaseAccount.DESCRIPTOR):
            account_response.account.Unpack(account)
        else:
            raise TypeError("Unexpected account type")

        return account

    def send_signed_transaction(self, tx_signed: JSONLike) -> Optional[str]:
        """
        Send a signed transaction and wait for confirmation.

        :param tx_signed: the signed transaction
        :return: tx_digest, if present
        """
        if self.is_cosmwasm_transaction(tx_signed):
            tx_digest = self._try_execute_wasm_transaction(tx_signed)
        elif self.is_transfer_transaction(tx_signed):
            tx_digest = self._try_send_signed_transaction(tx_signed)
        else:  # pragma: nocover
            _default_logger.warning(
                "Cannot send transaction. Unknown transaction type: {}".format(
                    tx_signed
                )
            )
            tx_digest = None
        return tx_digest

    def is_cosmwasm_transaction(self, tx_signed: JSONLike) -> bool:
        """Check whether it is a cosmwasm tx."""
        try:
            _type = cast(dict, tx_signed.get("value", {})).get("msg", [])[0]["type"]
            result = _type in [
                value[self.cosmwasm_version]
                for value in MESSAGE_FORMAT_BY_VERSION.values()
            ]
        except (KeyError, IndexError):  # pragma: nocover
            result = False
        return result

    @staticmethod
    def is_transfer_transaction(tx_signed: JSONLike) -> bool:
        """Check whether it is a transfer tx."""
        try:
            _type = cast(dict, tx_signed.get("tx", {})).get("msg", [])[0]["type"]
            result = _type in ["cosmos-sdk/MsgSend"]
        except (KeyError, IndexError):  # pragma: nocover
            result = False
        return result

    @try_decorator(
        "Encountered exception when trying to send tx: {}",
        logger_method=_default_logger.warning,
    )
    def _try_send_signed_transaction(self, tx_signed: JSONLike) -> Optional[str]:
        """
        Try send the signed transaction.

        :param tx_signed: the signed transaction
        :return: tx_digest, if present
        """
        tx_digest = None  # type: Optional[str]
        url = self.network_address + "/txs"
        response = requests.post(url=url, json=tx_signed)
        if response.status_code == 200:
            try:
                tx_digest = response.json()["txhash"]
            except KeyError:  # pragma: nocover
                raise ValueError(
                    f"key `txhash` not found in response_json={response.json()}"
                )
        else:  # pragma: nocover
            raise ValueError("Cannot send transaction: {}".format(response.json()))
        return tx_digest

    def get_transaction_receipt(self, tx_digest: str) -> Optional[JSONLike]:
        """
        Get the transaction receipt for a transaction digest.

        :param tx_digest: the digest associated to the transaction.
        :return: the tx receipt, if present
        """
        tx_receipt = self._try_get_transaction_receipt(tx_digest)
        return tx_receipt

    @try_decorator(
        "Encountered exception when trying to get transaction receipt: {}",
        logger_method=_default_logger.warning,
    )
    def _try_get_transaction_receipt(self, tx_digest: str) -> Optional[JSONLike]:
        """
        Try get the transaction receipt for a transaction digest.

        :param tx_digest: the digest associated to the transaction.
        :return: the tx receipt, if present
        """
        result: Optional[JSONLike] = None
        url = self.network_address + f"/txs/{tx_digest}"
        response = requests.get(url=url)
        if response.status_code == 200:
            result = response.json()
        else:  # pragma: nocover
            raise ValueError(
                "Cannot get transaction receipt: {}".format(response.json())
            )
        return result

    def get_transaction(self, tx_digest: str) -> Optional[JSONLike]:
        """
        Get the transaction for a transaction digest.

        :param tx_digest: the digest associated to the transaction.
        :return: the tx, if present
        """
        # Cosmos does not distinguish between transaction receipt and transaction
        tx_receipt = self._try_get_transaction_receipt(tx_digest)
        return tx_receipt

    def get_contract_instance(
        self, contract_interface: Dict[str, str], contract_address: Optional[str] = None
    ) -> Any:
        """
        Get the instance of a contract.

        :param contract_interface: the contract interface.
        :param contract_address: the contract address.
        :return: the contract instance
        """
        # Instance object not available for cosmwasm
        return None

    @staticmethod
    def _execute_shell_command(command: List[str]) -> str:
        """
        Execute command using subprocess and get result as JSON dict.

        :param command: the shell command to be executed
        :return: the stdout result converted to JSON dict
        """
        stdout, _ = subprocess.Popen(  # nosec
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        ).communicate()

        return stdout.decode("ascii")

    def update_with_gas_estimate(self, transaction: JSONLike) -> JSONLike:
        """
        Attempts to update the transaction with a gas estimate

        :param transaction: the transaction
        :raises: NotImplementedError
        """
        raise NotImplementedError(  # pragma: nocover
            "No gas estimation has been implemented."
        )


class CosmosApi(_CosmosApi, CosmosHelper):
    """Class to interact with the Cosmos SDK via a HTTP APIs."""


""" Equivalent to:

@dataclass
class CosmosFaucetStatus:
    tx_digest: Optional[str]
    status: str
"""
CosmosFaucetStatus = namedtuple("CosmosFaucetStatus", ["tx_digest", "status"])


class CosmosFaucetApi(FaucetApi):
    """Cosmos testnet faucet API."""

    FAUCET_STATUS_PENDING = "pending"  # noqa: F841
    FAUCET_STATUS_PROCESSING = "processing"  # noqa: F841
    FAUCET_STATUS_COMPLETED = "complete"  # noqa: F841
    FAUCET_STATUS_FAILED = "failed"  # noqa: F841

    identifier = _COSMOS
    testnet_faucet_url = DEFAULT_FAUCET_URL
    testnet_name = TESTNET_NAME
    max_retry_attempts = 15

    def __init__(self, poll_interval: Optional[float] = None):
        """Initialize CosmosFaucetApi."""
        self._poll_interval = float(poll_interval or 1)

    def get_wealth(self, address: Address, url: Optional[str] = None) -> None:
        """
        Get wealth from the faucet for the provided address.

        :param address: the address.
        :param url: the url
        :raises: RuntimeError of explicit faucet failures
        """
        uid = self._try_create_faucet_claim(address, url)
        if uid is None:  # pragma: nocover
            raise RuntimeError("Unable to create faucet claim")

        retry_attempts = self.max_retry_attempts
        while retry_attempts > 0:
            retry_attempts -= 1

            # lookup status form the claim uid
            status = self._try_check_faucet_claim(uid, url)
            if status is None:  # pragma: nocover
                raise RuntimeError("Failed to check faucet claim status")

            # if the status is complete
            if status.status == self.FAUCET_STATUS_COMPLETED:
                break

            # if the status is failure
            if (
                status.status != self.FAUCET_STATUS_PENDING
                and status.status != self.FAUCET_STATUS_PROCESSING
            ):  # pragma: nocover
                raise RuntimeError(f"Failed to get wealth for {address}")

            # if the status is incomplete
            time.sleep(self._poll_interval)
        if retry_attempts == 0:
            raise ValueError("Faucet claim check timed out!")  # pragma: nocover

    @classmethod
    @try_decorator(
        "An error occured while attempting to request a faucet request:\n{}",
        logger_method=_default_logger.error,
    )
    def _try_create_faucet_claim(
        cls, address: Address, url: Optional[str] = None
    ) -> Optional[str]:
        """
        Create a token faucet claim request

        :param address: the address to request funds
        :param url: the url
        :return: None on failure, otherwise the request uid
        """
        uri = cls._faucet_request_uri(url)
        response = requests.post(url=uri, json={"address": address})

        uid = None
        if response.status_code == 200:
            try:
                uid = response.json()["uuid"]
            except KeyError:  # pragma: nocover
                ValueError(f"key `uid` not found in response_json={response.json()}")
            _default_logger.info("Wealth claim generated, uid: {}".format(uid))
        else:  # pragma: no cover
            _default_logger.warning(
                "Response: {}, Text: {}".format(response.status_code, response.text)
            )

        return uid

    @classmethod
    @try_decorator(
        "An error occured while attempting to request a faucet request:\n{}",
        logger_method=_default_logger.error,
    )
    def _try_check_faucet_claim(
        cls, uid: str, url: Optional[str] = None
    ) -> Optional[CosmosFaucetStatus]:
        """
        Check the status of a faucet request

        :param uid: The request uid to be checked
        :param url: the url
        :return: None on failure otherwise a CosmosFaucetStatus for the specified uid
        """
        response = requests.get(cls._faucet_status_uri(uid, url))
        if response.status_code != 200:  # pragma: nocover
            _default_logger.warning(
                "Response: {}, Text: {}".format(response.status_code, response.text)
            )
            return None

        # parse the response
        data = response.json()
        tx_digest = None
        if "txStatus" in data["claim"]:
            tx_digest = data["claim"]["txStatus"]["hash"]

        return CosmosFaucetStatus(tx_digest=tx_digest, status=data["claim"]["status"],)

    @classmethod
    def _faucet_request_uri(cls, url: Optional[str] = None) -> str:
        """
        Generates the request URI derived from `cls.faucet_base_url` or provided url.

        :param url: the url
        :return: the faucet request uri
        """
        if cls.testnet_faucet_url is None:  # pragma: nocover
            raise ValueError("Testnet faucet url not set.")
        url = cls.testnet_faucet_url if url is None else url
        return f"{url}/api/v3/claims"

    @classmethod
    def _faucet_status_uri(cls, uid: str, url: Optional[str] = None) -> str:
        """Generates the status URI derived from `cls.faucet_base_url`."""
        return f"{cls._faucet_request_uri(url)}/{uid}"
