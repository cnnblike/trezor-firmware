# Automatically generated by pb2py
# fmt: off
import protobuf as p

if __debug__:
    try:
        from typing import Dict, List  # noqa: F401
        from typing_extensions import Literal  # noqa: F401
    except ImportError:
        pass


class WebAuthnRemoveResidentCredential(p.MessageType):
    MESSAGE_WIRE_TYPE = 803

    def __init__(
        self,
        index: int = None,
    ) -> None:
        self.index = index

    @classmethod
    def get_fields(cls) -> Dict:
        return {
            1: ('index', p.UVarintType, 0),
        }
