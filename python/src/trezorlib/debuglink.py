# This file is part of the Trezor project.
#
# Copyright (C) 2012-2022 SatoshiLabs and contributors
#
# This library is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License version 3
# as published by the Free Software Foundation.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the License along with this library.
# If not, see <https://www.gnu.org/licenses/lgpl-3.0.html>.

import logging
import textwrap
from collections import namedtuple
from copy import deepcopy
from enum import IntEnum
from itertools import zip_longest
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Generator,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
)

from mnemonic import Mnemonic

from . import mapping, messages, protobuf
from .client import TrezorClient
from .exceptions import TrezorFailure
from .log import DUMP_BYTES
from .tools import expect

if TYPE_CHECKING:
    from .transport import Transport
    from .messages import PinMatrixRequestType

    ExpectedMessage = Union[
        protobuf.MessageType, Type[protobuf.MessageType], "MessageFilter"
    ]

EXPECTED_RESPONSES_CONTEXT_LINES = 3

LayoutLines = namedtuple("LayoutLines", "lines text")

LOG = logging.getLogger(__name__)


def layout_lines(lines: Sequence[str]) -> LayoutLines:
    return LayoutLines(lines, " ".join(lines))


class DebugLink:
    def __init__(self, transport: "Transport", auto_interact: bool = True) -> None:
        self.transport = transport
        self.allow_interactions = auto_interact
        self.mapping = mapping.DEFAULT_MAPPING

    def open(self) -> None:
        self.transport.begin_session()

    def close(self) -> None:
        self.transport.end_session()

    def _call(self, msg: protobuf.MessageType, nowait: bool = False) -> Any:
        LOG.debug(
            f"sending message: {msg.__class__.__name__}",
            extra={"protobuf": msg},
        )
        msg_type, msg_bytes = self.mapping.encode(msg)
        LOG.log(
            DUMP_BYTES,
            f"encoded as type {msg_type} ({len(msg_bytes)} bytes): {msg_bytes.hex()}",
        )
        self.transport.write(msg_type, msg_bytes)
        if nowait:
            return None

        ret_type, ret_bytes = self.transport.read()
        LOG.log(
            DUMP_BYTES,
            f"received type {msg_type} ({len(msg_bytes)} bytes): {msg_bytes.hex()}",
        )
        msg = self.mapping.decode(ret_type, ret_bytes)
        LOG.debug(
            f"received message: {msg.__class__.__name__}",
            extra={"protobuf": msg},
        )
        return msg

    def state(self) -> messages.DebugLinkState:
        return self._call(messages.DebugLinkGetState())

    def read_layout(self) -> LayoutLines:
        return layout_lines(self.state().layout_lines)

    def wait_layout(self) -> LayoutLines:
        obj = self._call(messages.DebugLinkGetState(wait_layout=True))
        if isinstance(obj, messages.Failure):
            raise TrezorFailure(obj)
        return layout_lines(obj.layout_lines)

    def watch_layout(self, watch: bool) -> None:
        """Enable or disable watching layouts.
        If disabled, wait_layout will not work.

        The message is missing on T1. Use `TrezorClientDebugLink.watch_layout` for
        cross-version compatibility.
        """
        self._call(messages.DebugLinkWatchLayout(watch=watch))

    def encode_pin(self, pin: str, matrix: Optional[str] = None) -> str:
        """Transform correct PIN according to the displayed matrix."""
        if matrix is None:
            matrix = self.state().matrix
            if matrix is None:
                # we are on trezor-core
                return pin

        return "".join([str(matrix.index(p) + 1) for p in pin])

    def read_recovery_word(self) -> Tuple[Optional[str], Optional[int]]:
        state = self.state()
        return (state.recovery_fake_word, state.recovery_word_pos)

    def read_reset_word(self) -> str:
        state = self._call(messages.DebugLinkGetState(wait_word_list=True))
        return state.reset_word

    def read_reset_word_pos(self) -> int:
        state = self._call(messages.DebugLinkGetState(wait_word_pos=True))
        return state.reset_word_pos

    def input(
        self,
        word: Optional[str] = None,
        button: Optional[bool] = None,
        swipe: Optional[messages.DebugSwipeDirection] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        wait: Optional[bool] = None,
        hold_ms: Optional[int] = None,
    ) -> Optional[LayoutLines]:
        if not self.allow_interactions:
            return None

        args = sum(a is not None for a in (word, button, swipe, x))
        if args != 1:
            raise ValueError("Invalid input - must use one of word, button, swipe")

        decision = messages.DebugLinkDecision(
            yes_no=button, swipe=swipe, input=word, x=x, y=y, wait=wait, hold_ms=hold_ms
        )
        ret = self._call(decision, nowait=not wait)
        if ret is not None:
            return layout_lines(ret.lines)

        return None

    def click(
        self, click: Tuple[int, int], wait: bool = False
    ) -> Optional[LayoutLines]:
        x, y = click
        return self.input(x=x, y=y, wait=wait)

    def press_yes(self) -> None:
        self.input(button=True)

    def press_no(self) -> None:
        self.input(button=False)

    def swipe_up(self, wait: bool = False) -> None:
        self.input(swipe=messages.DebugSwipeDirection.UP, wait=wait)

    def swipe_down(self) -> None:
        self.input(swipe=messages.DebugSwipeDirection.DOWN)

    def swipe_right(self) -> None:
        self.input(swipe=messages.DebugSwipeDirection.RIGHT)

    def swipe_left(self) -> None:
        self.input(swipe=messages.DebugSwipeDirection.LEFT)

    def stop(self) -> None:
        self._call(messages.DebugLinkStop(), nowait=True)

    def reseed(self, value: int) -> protobuf.MessageType:
        return self._call(messages.DebugLinkReseedRandom(value=value))

    def start_recording(self, directory: str) -> None:
        self._call(messages.DebugLinkRecordScreen(target_directory=directory))

    def stop_recording(self) -> None:
        self._call(messages.DebugLinkRecordScreen(target_directory=None))

    @expect(messages.DebugLinkMemory, field="memory", ret_type=bytes)
    def memory_read(self, address: int, length: int) -> protobuf.MessageType:
        return self._call(messages.DebugLinkMemoryRead(address=address, length=length))

    def memory_write(self, address: int, memory: bytes, flash: bool = False) -> None:
        self._call(
            messages.DebugLinkMemoryWrite(address=address, memory=memory, flash=flash),
            nowait=True,
        )

    def flash_erase(self, sector: int) -> None:
        self._call(messages.DebugLinkFlashErase(sector=sector), nowait=True)

    @expect(messages.Success)
    def erase_sd_card(self, format: bool = True) -> messages.Success:
        return self._call(messages.DebugLinkEraseSdCard(format=format))


class NullDebugLink(DebugLink):
    def __init__(self) -> None:
        # Ignoring type error as self.transport will not be touched while using NullDebugLink
        super().__init__(None)  # type: ignore ["None" cannot be assigned to parameter of type "Transport"]

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def _call(
        self, msg: protobuf.MessageType, nowait: bool = False
    ) -> Optional[messages.DebugLinkState]:
        if not nowait:
            if isinstance(msg, messages.DebugLinkGetState):
                return messages.DebugLinkState()
            else:
                raise RuntimeError("unexpected call to a fake debuglink")

        return None


class DebugUI:
    INPUT_FLOW_DONE = object()

    def __init__(self, debuglink: DebugLink) -> None:
        self.debuglink = debuglink
        self.clear()

    def clear(self) -> None:
        self.pins: Optional[Iterator[str]] = None
        self.passphrase = ""
        self.input_flow: Union[
            Generator[None, messages.ButtonRequest, None], object, None
        ] = None

    def button_request(self, br: messages.ButtonRequest) -> None:
        if self.input_flow is None:
            if br.code == messages.ButtonRequestType.PinEntry:
                self.debuglink.input(self.get_pin())
            else:
                if br.pages is not None:
                    for _ in range(br.pages - 1):
                        self.debuglink.swipe_up(wait=True)
                self.debuglink.press_yes()
        elif self.input_flow is self.INPUT_FLOW_DONE:
            raise AssertionError("input flow ended prematurely")
        else:
            try:
                assert isinstance(self.input_flow, Generator)
                self.input_flow.send(br)
            except StopIteration:
                self.input_flow = self.INPUT_FLOW_DONE

    def get_pin(self, code: Optional["PinMatrixRequestType"] = None) -> str:
        if self.pins is None:
            raise RuntimeError("PIN requested but no sequence was configured")

        try:
            return self.debuglink.encode_pin(next(self.pins))
        except StopIteration:
            raise AssertionError("PIN sequence ended prematurely")

    def get_passphrase(self, available_on_device: bool) -> str:
        return self.passphrase


class MessageFilter:
    def __init__(self, message_type: Type[protobuf.MessageType], **fields: Any) -> None:
        self.message_type = message_type
        self.fields: Dict[str, Any] = {}
        self.update_fields(**fields)

    def update_fields(self, **fields: Any) -> "MessageFilter":
        for name, value in fields.items():
            try:
                self.fields[name] = self.from_message_or_type(value)
            except TypeError:
                self.fields[name] = value

        return self

    @classmethod
    def from_message_or_type(
        cls, message_or_type: "ExpectedMessage"
    ) -> "MessageFilter":
        if isinstance(message_or_type, cls):
            return message_or_type
        if isinstance(message_or_type, protobuf.MessageType):
            return cls.from_message(message_or_type)
        if isinstance(message_or_type, type) and issubclass(
            message_or_type, protobuf.MessageType
        ):
            return cls(message_or_type)
        raise TypeError("Invalid kind of expected response")

    @classmethod
    def from_message(cls, message: protobuf.MessageType) -> "MessageFilter":
        fields = {}
        for field in message.FIELDS.values():
            value = getattr(message, field.name)
            if value in (None, [], protobuf.REQUIRED_FIELD_PLACEHOLDER):
                continue
            fields[field.name] = value
        return cls(type(message), **fields)

    def match(self, message: protobuf.MessageType) -> bool:
        if type(message) != self.message_type:
            return False

        for field, expected_value in self.fields.items():
            actual_value = getattr(message, field, None)
            if isinstance(expected_value, MessageFilter):
                if actual_value is None or not expected_value.match(actual_value):
                    return False
            elif expected_value != actual_value:
                return False

        return True

    def to_string(self, maxwidth: int = 80) -> str:
        fields: List[Tuple[str, str]] = []
        for field in self.message_type.FIELDS.values():
            if field.name not in self.fields:
                continue
            value = self.fields[field.name]
            if isinstance(value, IntEnum):
                field_str = value.name
            elif isinstance(value, MessageFilter):
                field_str = value.to_string(maxwidth - 4)
            elif isinstance(value, protobuf.MessageType):
                field_str = protobuf.format_message(value)
            else:
                field_str = repr(value)
            field_str = textwrap.indent(field_str, "    ").lstrip()
            fields.append((field.name, field_str))

        pairs = [f"{k}={v}" for k, v in fields]
        oneline_str = ", ".join(pairs)
        if len(oneline_str) < maxwidth:
            return f"{self.message_type.__name__}({oneline_str})"
        else:
            item: List[str] = []
            item.append(f"{self.message_type.__name__}(")
            for pair in pairs:
                item.append(f"    {pair}")
            item.append(")")
            return "\n".join(item)


class MessageFilterGenerator:
    def __getattr__(self, key: str) -> Callable[..., "MessageFilter"]:
        message_type = getattr(messages, key)
        return MessageFilter(message_type).update_fields


message_filters = MessageFilterGenerator()


class TrezorClientDebugLink(TrezorClient):
    # This class implements automatic responses
    # and other functionality for unit tests
    # for various callbacks, created in order
    # to automatically pass unit tests.
    #
    # This mixing should be used only for purposes
    # of unit testing, because it will fail to work
    # without special DebugLink interface provided
    # by the device.

    def __init__(self, transport: "Transport", auto_interact: bool = True) -> None:
        try:
            debug_transport = transport.find_debug()
            self.debug = DebugLink(debug_transport, auto_interact)
            # try to open debuglink, see if it works
            self.debug.open()
            self.debug.close()
        except Exception:
            if not auto_interact:
                self.debug = NullDebugLink()
            else:
                raise

        self.reset_debug_features()

        super().__init__(transport, ui=self.ui)

    def reset_debug_features(self) -> None:
        """Prepare the debugging client for a new testcase.

        Clears all debugging state that might have been modified by a testcase.
        """
        self.ui: DebugUI = DebugUI(self.debug)
        self.in_with_statement = False
        self.expected_responses: Optional[List[MessageFilter]] = None
        self.actual_responses: Optional[List[protobuf.MessageType]] = None
        self.filters: Dict[
            Type[protobuf.MessageType],
            Callable[[protobuf.MessageType], protobuf.MessageType],
        ] = {}

    def open(self) -> None:
        super().open()
        if self.session_counter == 1:
            self.debug.open()

    def close(self) -> None:
        if self.session_counter == 1:
            self.debug.close()
        super().close()

    def set_filter(
        self,
        message_type: Type[protobuf.MessageType],
        callback: Callable[[protobuf.MessageType], protobuf.MessageType],
    ) -> None:
        """Configure a filter function for a specified message type.

        The `callback` must be a function that accepts a protobuf message, and returns
        a (possibly modified) protobuf message of the same type. Whenever a message
        is sent or received that matches `message_type`, `callback` is invoked on the
        message and its result is substituted for the original.

        Useful for test scenarios with an active malicious actor on the wire.
        """
        if not self.in_with_statement:
            raise RuntimeError("Must be called inside 'with' statement")

        self.filters[message_type] = callback

    def _filter_message(self, msg: protobuf.MessageType) -> protobuf.MessageType:
        message_type = msg.__class__
        callback = self.filters.get(message_type)
        if callable(callback):
            return callback(deepcopy(msg))
        else:
            return msg

    def set_input_flow(
        self, input_flow: Generator[None, Optional[messages.ButtonRequest], None]
    ) -> None:
        """Configure a sequence of input events for the current with-block.

        The `input_flow` must be a generator function. A `yield` statement in the
        input flow function waits for a ButtonRequest from the device, and returns
        its code.

        Example usage:

        >>> def input_flow():
        >>>     # wait for first button prompt
        >>>     code = yield
        >>>     assert code == ButtonRequestType.Other
        >>>     # press No
        >>>     client.debug.press_no()
        >>>
        >>>     # wait for second button prompt
        >>>     yield
        >>>     # press Yes
        >>>     client.debug.press_yes()
        >>>
        >>> with client:
        >>>     client.set_input_flow(input_flow)
        >>>     some_call(client)
        """
        if not self.in_with_statement:
            raise RuntimeError("Must be called inside 'with' statement")

        if callable(input_flow):
            input_flow = input_flow()
        if not hasattr(input_flow, "send"):
            raise RuntimeError("input_flow should be a generator function")
        self.ui.input_flow = input_flow
        input_flow.send(None)  # start the generator

    def watch_layout(self, watch: bool = True) -> None:
        """Enable or disable watching layout changes.

        Since trezor-core v2.3.2, it is necessary to call `watch_layout()` before
        using `debug.wait_layout()`, otherwise layout changes are not reported.
        """
        if self.version >= (2, 3, 2):
            # version check is necessary because otherwise we cannot reliably detect
            # whether and where to wait for reply:
            # - T1 reports unknown debuglink messages on the wirelink
            # - TT < 2.3.0 does not reply to unknown debuglink messages due to a bug
            self.debug.watch_layout(watch)

    def __enter__(self) -> "TrezorClientDebugLink":
        # For usage in with/expected_responses
        if self.in_with_statement:
            raise RuntimeError("Do not nest!")
        self.in_with_statement = True
        return self

    def __exit__(self, exc_type: Any, value: Any, traceback: Any) -> None:
        __tracebackhide__ = True  # for pytest # pylint: disable=W0612

        self.watch_layout(False)
        # copy expected/actual responses before clearing them
        expected_responses = self.expected_responses
        actual_responses = self.actual_responses
        self.reset_debug_features()

        if exc_type is None:
            # If no other exception was raised, evaluate missed responses
            # (raises AssertionError on mismatch)
            self._verify_responses(expected_responses, actual_responses)

    def set_expected_responses(
        self, expected: List[Union["ExpectedMessage", Tuple[bool, "ExpectedMessage"]]]
    ) -> None:
        """Set a sequence of expected responses to client calls.

        Within a given with-block, the list of received responses from device must
        match the list of expected responses, otherwise an AssertionError is raised.

        If an expected response is given a field value other than None, that field value
        must exactly match the received field value. If a given field is None
        (or unspecified) in the expected response, the received field value is not
        checked.

        Each expected response can also be a tuple (bool, message). In that case, the
        expected response is only evaluated if the first field is True.
        This is useful for differentiating sequences between Trezor models:

        >>> trezor_one = client.features.model == "1"
        >>> client.set_expected_responses([
        >>>     messages.ButtonRequest(code=ConfirmOutput),
        >>>     (trezor_one, messages.ButtonRequest(code=ConfirmOutput)),
        >>>     messages.Success(),
        >>> ])
        """
        if not self.in_with_statement:
            raise RuntimeError("Must be called inside 'with' statement")

        # make sure all items are (bool, message) tuples
        expected_with_validity = (
            e if isinstance(e, tuple) else (True, e) for e in expected
        )

        # only apply those items that are (True, message)
        self.expected_responses = [
            MessageFilter.from_message_or_type(expected)
            for valid, expected in expected_with_validity
            if valid
        ]
        self.actual_responses = []

    def use_pin_sequence(self, pins: Iterable[str]) -> None:
        """Respond to PIN prompts from device with the provided PINs.
        The sequence must be at least as long as the expected number of PIN prompts.
        """
        self.ui.pins = iter(pins)

    def use_passphrase(self, passphrase: str) -> None:
        """Respond to passphrase prompts from device with the provided passphrase."""
        self.ui.passphrase = Mnemonic.normalize_string(passphrase)

    def use_mnemonic(self, mnemonic: str) -> None:
        """Use the provided mnemonic to respond to device.
        Only applies to T1, where device prompts the host for mnemonic words."""
        self.mnemonic = Mnemonic.normalize_string(mnemonic).split(" ")

    def _raw_read(self) -> protobuf.MessageType:
        __tracebackhide__ = True  # for pytest # pylint: disable=W0612

        resp = super()._raw_read()
        resp = self._filter_message(resp)
        if self.actual_responses is not None:
            self.actual_responses.append(resp)
        return resp

    def _raw_write(self, msg: protobuf.MessageType) -> None:
        return super()._raw_write(self._filter_message(msg))

    @staticmethod
    def _expectation_lines(expected: List[MessageFilter], current: int) -> List[str]:
        start_at = max(current - EXPECTED_RESPONSES_CONTEXT_LINES, 0)
        stop_at = min(current + EXPECTED_RESPONSES_CONTEXT_LINES + 1, len(expected))
        output: List[str] = []
        output.append("Expected responses:")
        if start_at > 0:
            output.append(f"    (...{start_at} previous responses omitted)")
        for i in range(start_at, stop_at):
            exp = expected[i]
            prefix = "    " if i != current else ">>> "
            output.append(textwrap.indent(exp.to_string(), prefix))
        if stop_at < len(expected):
            omitted = len(expected) - stop_at
            output.append(f"    (...{omitted} following responses omitted)")

        output.append("")
        return output

    @classmethod
    def _verify_responses(
        cls,
        expected: Optional[List[MessageFilter]],
        actual: Optional[List[protobuf.MessageType]],
    ) -> None:
        __tracebackhide__ = True  # for pytest # pylint: disable=W0612

        if expected is None and actual is None:
            return

        assert expected is not None
        assert actual is not None

        for i, (exp, act) in enumerate(zip_longest(expected, actual)):
            if exp is None:
                output = cls._expectation_lines(expected, i)
                output.append("No more messages were expected, but we got:")
                for resp in actual[i:]:
                    output.append(
                        textwrap.indent(protobuf.format_message(resp), "    ")
                    )
                raise AssertionError("\n".join(output))

            if act is None:
                output = cls._expectation_lines(expected, i)
                output.append("This and the following message was not received.")
                raise AssertionError("\n".join(output))

            if not exp.match(act):
                output = cls._expectation_lines(expected, i)
                output.append("Actually received:")
                output.append(textwrap.indent(protobuf.format_message(act), "    "))
                raise AssertionError("\n".join(output))

    def mnemonic_callback(self, _) -> str:
        word, pos = self.debug.read_recovery_word()
        if word:
            return word
        if pos:
            return self.mnemonic[pos - 1]

        raise RuntimeError("Unexpected call")


@expect(messages.Success, field="message", ret_type=str)
def load_device(
    client: "TrezorClient",
    mnemonic: Union[str, Iterable[str]],
    pin: Optional[str],
    passphrase_protection: bool,
    label: Optional[str],
    language: str = "en-US",
    skip_checksum: bool = False,
    needs_backup: bool = False,
    no_backup: bool = False,
) -> protobuf.MessageType:
    if isinstance(mnemonic, str):
        mnemonic = [mnemonic]

    mnemonics = [Mnemonic.normalize_string(m) for m in mnemonic]

    if client.features.initialized:
        raise RuntimeError(
            "Device is initialized already. Call device.wipe() and try again."
        )

    resp = client.call(
        messages.LoadDevice(
            mnemonics=mnemonics,
            pin=pin,
            passphrase_protection=passphrase_protection,
            language=language,
            label=label,
            skip_checksum=skip_checksum,
            needs_backup=needs_backup,
            no_backup=no_backup,
        )
    )
    client.init_device()
    return resp


# keep the old name for compatibility
load_device_by_mnemonic = load_device


@expect(messages.Success, field="message", ret_type=str)
def self_test(client: "TrezorClient") -> protobuf.MessageType:
    if client.features.bootloader_mode is not True:
        raise RuntimeError("Device must be in bootloader mode")

    return client.call(
        messages.SelfTest(
            payload=b"\x00\xFF\x55\xAA\x66\x99\x33\xCCABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!\x00\xFF\x55\xAA\x66\x99\x33\xCC"
        )
    )
