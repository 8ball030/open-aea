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

"""This module contains the handler for the 'echo' skill."""
from typing import cast

from aea.protocols.base import Message
from aea.protocols.default.message import DefaultMessage
from aea.skills.base import Handler

from packages.fetchai.skills.echo.dialogues import DefaultDialogue
from packages.fetchai.skills.echo.dialogues import DefaultDialogues


class EchoHandler(Handler):
    """Echo handler."""

    SUPPORTED_PROTOCOL = DefaultMessage.protocol_id

    def setup(self) -> None:
        """Set up the handler."""
        self.context.logger.info("Echo Handler: setup method called.")

    def handle(self, message: Message) -> None:
        """
        Handle the message.

        :param message: the message.
        :return: None
        """
        message = cast(DefaultMessage, message)  # recover dialogue
        dialogues = cast(DefaultDialogues, self.context.default_dialogues)
        dialogue = cast(DefaultDialogue, dialogues.update(message))
        if dialogue is None:
            self._handle_unidentified_dialogue(message)
            return  # handle message
        if message.performative == DefaultMessage.Performative.BYTES:
            self._handle_bytes(message, dialogue)
        elif message.performative == DefaultMessage.Performative.ERROR:
            self._handle_error(message, dialogue)
        else:
            self._handle_invalid(message, dialogue)

    def teardown(self) -> None:
        """
        Teardown the handler.

        :return: None
        """
        self.context.logger.info("Echo Handler: teardown method called.")

    def _handle_unidentified_dialogue(self, message: DefaultMessage) -> None:
        """
        Handle unidentified dialogue.

        :param message: the message.
        :return: None
        """
        self.context.logger.info(
            "received invalid default message={}, unidentified dialogue.".format(
                message
            )
        )
        dialogues = cast(DefaultDialogues, self.context.default_dialogues)
        reply = DefaultMessage(
            performative=DefaultMessage.Performative.ERROR,
            dialogue_reference=dialogues.new_self_initiated_dialogue_reference(),
            error_code=DefaultMessage.ErrorCode.INVALID_DIALOGUE,
            error_msg="Invalid dialogue.",
            error_data={"default_message": message.encode()},
        )
        reply.counterparty = message.sender
        dialogues.update(message)
        self.context.outbox.put_message(message=reply)

    def _handle_error(self, message: DefaultMessage, dialogue: DefaultDialogue) -> None:
        """
        Handle a message of error performative.

        :param message: the default message.
        :param dialogue: the dialogue.
        """
        self.context.logger.info(
            "received default error message={} in dialogue={}.".format(
                message, dialogue
            )
        )

    def _handle_bytes(self, message: DefaultMessage, dialogue: DefaultDialogue):
        """
        Handle a message of bytes performative.

        :param message: the default message.
        :param dialogue: the default dialogue.
        :return: None
        """
        self.context.logger.info(
            "Echo Handler: message={}, sender={}".format(message, message.counterparty)
        )
        reply = DefaultMessage(
            performative=DefaultMessage.Performative.BYTES,
            dialogue_reference=message.dialogue_reference,
            message_id=message.message_id + 1,
            target=message.message_id,
            content=message.content,
        )
        reply.counterparty = message.sender
        dialogue.update(reply)
        self.context.outbox.put_message(message=reply)

    def _handle_invalid(self, message: DefaultMessage, dialogue: DefaultDialogue):
        """
        Handle an invalid message.

        :param message: the message.
        :param dialogue: the dialogue.
        :return: None
        """
        self.context.logger.info(
            "received invalid message={} in dialogue={}.".format(message, dialogue)
        )
