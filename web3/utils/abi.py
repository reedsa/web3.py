import functools
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
    cast,
)

from eth_abi import (
    codec,
)
from eth_abi.codec import (
    ABICodec,
)
from eth_abi.registry import (
    registry as default_registry,
)
from eth_typing import (
    ABI,
    ABICallable,
    ABIConstructor,
    ABIElement,
    ABIElementInfo,
    ABIError,
    ABIEvent,
    ABIFallback,
    ABIFunction,
    ABIReceive,
    HexStr,
    Primitives,
)
from eth_utils.address import (
    is_binary_address,
    is_checksum_address,
)
from eth_utils.conversions import (
    hexstr_if_str,
    to_bytes,
)
from eth_utils.hexadecimal import (
    encode_hex,
)
from eth_utils.toolz import (
    pipe,
)
from eth_utils.types import (
    is_list_like,
    is_text,
)
from hexbytes import (
    HexBytes,
)

from web3._utils.abi import (
    filter_by_argument_name,
)
from web3._utils.function_identifiers import (
    FallbackFn,
    ReceiveFn,
)
from web3.exceptions import (
    ABIConstructorNotFound,
    ABIFallbackNotFound,
    ABIReceiveNotFound,
    MismatchedABI,
    Web3TypeError,
    Web3ValidationError,
    Web3ValueError,
)
from web3.types import (
    FunctionIdentifier,
)

from eth_utils.abi import (  # noqa
    abi_to_signature,
    collapse_if_tuple,
    event_abi_to_log_topic,
    event_signature_to_log_topic,
    filter_abi_by_name,
    filter_abi_by_type,
    function_abi_to_4byte_selector,
    function_signature_to_4byte_selector,
    get_abi_input_names,
    get_abi_input_types,
    get_abi_output_names,
    get_abi_output_types,
    get_aligned_abi_inputs,
    get_all_event_abis,
    get_all_function_abis,
    get_normalized_abi_inputs,
)


def _filter_by_argument_count(
    num_arguments: int, contract_abi: ABI
) -> List[ABIElement]:
    return [
        abi
        for abi in contract_abi
        if abi["type"] != "fallback"
        and abi["type"] != "receive"
        and len(abi.get("inputs", [])) == num_arguments
    ]


def _filter_by_encodability(
    abi_codec: codec.ABIEncoder,
    contract_abi: ABI,
    *args: Optional[Sequence[Any]],
    **kwargs: Optional[Dict[str, Any]],
) -> List[ABICallable]:
    return [
        cast(ABICallable, function_abi)
        for function_abi in contract_abi
        if check_if_arguments_can_be_encoded(
            function_abi, *args, abi_codec=abi_codec, **kwargs
        )
    ]


def _mismatched_abi_error_diagnosis(
    function_identifier: FunctionIdentifier,
    matching_function_signatures: Sequence[str],
    arg_count_matches: int,
    encoding_matches: int,
    *args: Optional[Sequence[Any]],
    **kwargs: Optional[Dict[str, Any]],
) -> str:
    """
    Raise a ``MismatchedABI`` when a function ABI lookup results in an error.

    An error may result from multiple functions matching the provided signature and
    arguments or no functions are identified.
    """
    diagnosis = "\n"
    if arg_count_matches == 0:
        diagnosis += "Function invocation failed due to improper number of arguments."
    elif encoding_matches == 0:
        diagnosis += "Function invocation failed due to no matching argument types."
    elif encoding_matches > 1:
        diagnosis += (
            "Ambiguous argument encoding. "
            "Provided arguments can be encoded to multiple functions "
            "matching this call."
        )

    collapsed_args = _extract_argument_types(*args)
    collapsed_kwargs = dict(
        {(k, _extract_argument_types([v])) for k, v in kwargs.items()}
    )

    return (
        f"\nCould not identify the intended function with name "
        f"`{function_identifier}`, positional arguments with type(s) "
        f"`({collapsed_args})` and keyword arguments with type(s) "
        f"`{collapsed_kwargs}`."
        f"\nFound {len(matching_function_signatures)} function(s) with the name "
        f"`{function_identifier}`: {matching_function_signatures}{diagnosis}"
    )


def _extract_argument_types(*args: Sequence[Any]) -> str:
    """
    Takes a list of arguments and returns a string representation of the argument types,
    appropriately collapsing `tuple` types into the respective nested types.
    """
    collapsed_args = []

    for arg in args:
        if is_list_like(arg):
            collapsed_nested = []
            for nested in arg:
                if is_list_like(nested):
                    collapsed_nested.append(f"({_extract_argument_types(nested)})")
                else:
                    collapsed_nested.append(_get_argument_readable_type(nested))
            collapsed_args.append(",".join(collapsed_nested))
        else:
            collapsed_args.append(_get_argument_readable_type(arg))

    return ",".join(collapsed_args)


def _get_argument_readable_type(arg: Any) -> str:
    """
    Returns the class name of the argument, or `address` if the argument is an address.
    """
    if is_checksum_address(arg) or is_binary_address(arg):
        return "address"

    return arg.__class__.__name__


def _element_abi_to_type(
    element_abi: ABIElement,
) -> ABIElement:
    """
    Convert an ABI element to its respective type.
    """
    element_type = element_abi.get("type")
    if element_type == "function":
        return cast(ABIFunction, element_abi)
    elif element_type == "constructor":
        return cast(ABIConstructor, element_abi)
    elif element_type == "fallback":
        return cast(ABIFallback, element_abi)
    elif element_type == "receive":
        return cast(ABIReceive, element_abi)
    elif element_type == "event":
        return cast(ABIEvent, element_abi)
    elif element_type == "error":
        return cast(ABIError, element_abi)
    else:
        raise Web3ValueError(f"Invalid abi type `{element_type}`.")


def get_abi_element_info(
    abi: ABI,
    function_identifier: str,
    *args: Optional[List[Any]],
    abi_codec: Optional[Any] = None,
    **kwargs: Optional[Dict[str, Any]],
) -> ABIElementInfo:
    """
    Information about the function ABI, selector and input arguments.

    Returns the ABI which matches the provided identifier, named arguments (``args``)
    and keyword args (``kwargs``).

    :param abi: Contract ABI.
    :type abi: `ABI`
    :param function_identifier: Find a function ABI with matching identifier.
    :type function_identifier: `str`
    :param args: Find a function ABI with matching args.
    :type args: `Optional[Sequence[Any]]`
    :param kwargs: Find a function ABI with matching kwargs.
    :type kwargs: `Optional[Dict[str, Any]]`
    :param abi_codec: Codec used for encoding and decoding. Default with \
    `strict_bytes_type_checking` enabled.
    :type abi_codec: `Optional[Any]`
    :return: Function information including the ABI, selector and args.
    :rtype: `ABIElementInfo`

    .. doctest::

        >>> from web3.utils.abi import get_abi_element_info
        >>> abi = [
        ...     {
        ...         "constant": False,
        ...         "inputs": [
        ...             {"name": "a", "type": "uint256"},
        ...             {"name": "b", "type": "uint256"},
        ...         ],
        ...         "name": "multiply",
        ...         "outputs": [{"name": "result", "type": "uint256"}],
        ...         "payable": False,
        ...         "stateMutability": "nonpayable",
        ...         "type": "function",
        ...     }
        ... ]
        >>> fn_info = get_abi_element_info(abi, "multiply", *[7, 3])
        >>> fn_info["abi"]
        {'constant': False, 'inputs': [{'name': 'a', 'type': 'uint256'}, {\
'name': 'b', 'type': 'uint256'}], 'name': 'multiply', 'outputs': [{\
'name': 'result', 'type': 'uint256'}], 'payable': False, \
'stateMutability': 'nonpayable', 'type': 'function'}
        >>> fn_info["selector"]
        '0x165c4a16'
        >>> fn_info["arguments"]
        (7, 3)
    """
    fn_abi = get_abi_element(
        abi, function_identifier, *args, abi_codec=abi_codec, **kwargs
    )
    fn_selector = encode_hex(function_abi_to_4byte_selector(fn_abi))
    fn_inputs: Tuple[Any, ...] = tuple()

    if fn_abi["type"] == "fallback" or fn_abi["type"] == "receive":
        return ABIElementInfo(abi=fn_abi, selector=fn_selector, arguments=tuple())
    else:
        fn_inputs = get_normalized_abi_inputs(fn_abi, *args, **kwargs)
        _, aligned_fn_inputs = get_aligned_abi_inputs(fn_abi, fn_inputs)

        return ABIElementInfo(
            abi=fn_abi, selector=fn_selector, arguments=aligned_fn_inputs
        )


def get_abi_element(
    abi: ABI,
    function_identifier: FunctionIdentifier,
    *args: Optional[Sequence[Any]],
    abi_codec: Optional[Any] = None,
    **kwargs: Optional[Dict[str, Any]],
) -> ABIElement:
    """
    Return the interface for an ``ABIElement`` which matches the provided identifier
    and arguments.

    The ABI which matches the provided identifier, named arguments (``args``) and
    keyword args (``kwargs``) will be returned.

    The `abi_codec` may be overridden if custom encoding and decoding is required. The
    default is used if no codec is provided. More details about customizations are in
    the `eth-abi Codecs Doc <https://eth-abi.readthedocs.io/en/latest/codecs.html>`__.

    :param abi: Contract ABI.
    :type abi: `ABI`
    :param function_identifier: Find a function ABI with matching name.
    :type function_identifier: `FunctionIdentifier`
    :param args: Find a function ABI with matching args.
    :type args: `Optional[list[Any]]`
    :param kwargs: Find a function ABI with matching kwargs.
    :type kwargs: `Optional[Dict[str, Any]]`
    :param abi_codec: Codec used for encoding and decoding. Default with \
    `strict_bytes_type_checking` enabled.
    :type abi_codec: `Optional[Any]`
    :return: ABI for the specific ABI element.
    :rtype: `ABIElement`

    .. doctest::

        >>> from web3.utils.abi import get_abi_element
        >>> abi = [
        ...     {
        ...         "constant": False,
        ...         "inputs": [
        ...             {"name": "a", "type": "uint256"},
        ...             {"name": "b", "type": "uint256"},
        ...         ],
        ...         "name": "multiply",
        ...         "outputs": [{"name": "result", "type": "uint256"}],
        ...         "payable": False,
        ...         "stateMutability": "nonpayable",
        ...         "type": "function",
        ...     }
        ... ]
        >>> get_abi_element(abi, "multiply", *[7, 3])
        {'constant': False, 'inputs': [{'name': 'a', 'type': 'uint256'}, {\
'name': 'b', 'type': 'uint256'}], 'name': 'multiply', 'outputs': [{'name': 'result', \
'type': 'uint256'}], 'payable': False, 'stateMutability': 'nonpayable', \
'type': 'function'}
    """
    if abi_codec is None:
        abi_codec = ABICodec(default_registry)

    if function_identifier is FallbackFn or function_identifier == "fallback":
        return get_fallback_function_abi(abi)

    if function_identifier is ReceiveFn or function_identifier == "receive":
        return get_receive_function_abi(abi)

    if function_identifier is None or not is_text(function_identifier):
        raise Web3TypeError("Unsupported function identifier")

    if function_identifier == "constructor":
        return get_constructor_function_abi(abi)

    filtered_abis_by_name = filter_abi_by_name(cast(str, function_identifier), abi)
    arg_count = len(args) + len(kwargs)
    abi_element_matches = _filter_by_argument_count(arg_count, filtered_abis_by_name)

    if not args and not kwargs and len(abi_element_matches) == 1:
        return _element_abi_to_type(abi_element_matches[0])

    elements_with_encodable_args = _filter_by_encodability(
        abi_codec, abi_element_matches, *args, **kwargs
    )

    if len(elements_with_encodable_args) != 1:
        matching_function_signatures = [
            abi_to_signature(func) for func in filtered_abis_by_name
        ]

        error_diagnosis = _mismatched_abi_error_diagnosis(
            function_identifier,
            matching_function_signatures,
            len(abi_element_matches),
            len(elements_with_encodable_args),
            *args,
            **kwargs,
        )

        raise MismatchedABI(error_diagnosis)

    return _element_abi_to_type(elements_with_encodable_args[0])


def get_constructor_function_abi(contract_abi: ABI) -> ABIConstructor:
    """
    Return the constructor function ABI from the contract ABI.

    :param contract_abi: Contract ABI.
    :type contract_abi: `ABI`
    :return: Constructor function ABI.
    :rtype: `ABIConstructor`

    .. doctest::

        >>> from web3.utils.abi import get_constructor_function_abi
        >>> abi = [
        ...     {
        ...         "constant": False,
        ...         "inputs": [],
        ...         "name": "constructor",
        ...         "outputs": [],
        ...         "payable": False,
        ...         "stateMutability": "nonpayable",
        ...         "type": "constructor",
        ...     }
        ... ]
        >>> get_constructor_function_abi(abi)
        {'constant': False, 'inputs': [], 'name': 'constructor', 'outputs': [], \
'payable': False, 'stateMutability': 'nonpayable', 'type': 'constructor'}
    """
    constructor_abis = filter_abi_by_type("constructor", contract_abi)
    if constructor_abis and constructor_abis[0]["type"] == "constructor":
        return cast(ABIConstructor, constructor_abis[0])
    else:
        raise ABIConstructorNotFound(
            "No constructor function was found in the contract ABI."
        )


def get_receive_function_abi(contract_abi: ABI) -> ABIReceive:
    """
    Return the receive function ABI from the contract ABI.

    :param contract_abi: Contract ABI.
    :type contract_abi: `ABI`
    :return: Receive function ABI.
    :rtype: `ABIReceive`

    .. doctest::

        >>> from web3.utils.abi import get_receive_function_abi
        >>> abi = [
        ...     {
        ...         "constant": False,
        ...         "inputs": [],
        ...         "name": "receive",
        ...         "outputs": [],
        ...         "payable": False,
        ...         "stateMutability": "nonpayable",
        ...         "type": "receive",
        ...     }
        ... ]
        >>> get_receive_function_abi(abi)
        {'constant': False, 'inputs': [], 'name': 'receive', 'outputs': [], \
'payable': False, 'stateMutability': 'nonpayable', 'type': 'receive'}
    """
    receive_abis = filter_abi_by_type("receive", contract_abi)
    if receive_abis and receive_abis[0]["type"] == "receive":
        return cast(ABIReceive, receive_abis[0])
    else:
        raise ABIReceiveNotFound("No receive function was found in the contract ABI.")


def get_fallback_function_abi(contract_abi: ABI) -> ABIFallback:
    """
    Return the fallback function ABI from the contract ABI.

    :param contract_abi: Contract ABI.
    :type contract_abi: `ABI`
    :return: Fallback function ABI.
    :rtype: `ABIFallback`

    .. doctest::

        >>> from web3.utils.abi import get_fallback_function_abi
        >>> abi = [
        ...     {
        ...         "constant": False,
        ...         "inputs": [],
        ...         "name": "fallback",
        ...         "outputs": [],
        ...         "payable": False,
        ...         "stateMutability": "nonpayable",
        ...         "type": "fallback",
        ...     }
        ... ]
        >>> get_fallback_function_abi(abi)
        {'constant': False, 'inputs': [], 'name': 'fallback', 'outputs': [], \
'payable': False, 'stateMutability': 'nonpayable', 'type': 'fallback'}
    """
    fallback_abis = filter_abi_by_type("fallback", contract_abi)
    if fallback_abis and fallback_abis[0]["type"] == "fallback":
        return cast(ABIFallback, fallback_abis[0])
    else:
        raise ABIFallbackNotFound("No fallback function was found in the contract ABI.")


def check_if_arguments_can_be_encoded(
    abi_element: ABIElement,
    *args: Optional[Sequence[Any]],
    abi_codec: Optional[Any] = None,
    **kwargs: Optional[Dict[str, Any]],
) -> bool:
    """
    Check if the provided arguments can be encoded with the element ABI.

    :param abi_element: The ABI element.
    :type abi_element: `ABIElement`
    :param args: Positional arguments.
    :type args: `Optional[Sequence[Any]]`
    :param kwargs: Keyword arguments.
    :type kwargs: `Optional[Dict[str, Any]]`
    :param abi_codec: Codec used for encoding and decoding. Default with \
    `strict_bytes_type_checking` enabled.
    :type abi_codec: `Optional[Any]`
    :return: True if the arguments can be encoded, False otherwise.
    :rtype: `bool`

    .. doctest::

            >>> from web3.utils.abi import check_if_arguments_can_be_encoded
            >>> abi = {
            ...     "constant": False,
            ...     "inputs": [
            ...         {"name": "a", "type": "uint256"},
            ...         {"name": "b", "type": "uint256"},
            ...     ],
            ...     "name": "multiply",
            ...     "outputs": [{"name": "result", "type": "uint256"}],
            ...     "payable": False,
            ...     "stateMutability": "nonpayable",
            ...     "type": "function",
            ... }
            >>> check_if_arguments_can_be_encoded(abi, *[7, 3], **{})
            True
    """
    if abi_element["type"] == "fallback" or abi_element["type"] == "receive":
        return True

    try:
        arguments = get_normalized_abi_inputs(abi_element, *args, **kwargs)
    except TypeError:
        return False

    if len(abi_element.get("inputs", ())) != len(arguments):
        return False

    try:
        types, aligned_args = get_aligned_abi_inputs(abi_element, arguments)
    except TypeError:
        return False

    if abi_codec is None:
        abi_codec = ABICodec(default_registry)

    return all(
        abi_codec.is_encodable(_type, arg) for _type, arg in zip(types, aligned_args)
    )


def get_event_abi(
    abi: ABI,
    event_name: str,
    argument_names: Optional[Sequence[str]] = None,
) -> ABIEvent:
    """
    Find the event interface with the given name and/or arguments.

    :param abi: Contract ABI.
    :type abi: `ABI`
    :param event_name: Find an event abi with matching event name.
    :type event_name: `str`
    :param argument_names: Find an event abi with matching arguments.
    :type argument_names: `list[str]`
    :return: ABI for the event interface.
    :rtype: `ABIEvent`

    .. doctest::

        >>> from web3.utils import get_event_abi
        >>> abi = [
        ...   {"type": "function", "name": "myFunction", "inputs": [], "outputs": []},
        ...   {"type": "function", "name": "myFunction2", "inputs": [], "outputs": []},
        ...   {"type": "event", "name": "MyEvent", "inputs": []}
        ... ]
        >>> get_event_abi(abi, 'MyEvent')
        {'type': 'event', 'name': 'MyEvent', 'inputs': []}
    """
    filters = [
        functools.partial(filter_abi_by_type, "event"),
    ]

    if event_name is None or event_name == "":
        raise Web3ValidationError(
            "event_name is required in order to match an event ABI."
        )

    filters.append(functools.partial(filter_abi_by_name, event_name))

    if argument_names is not None:
        filters.append(functools.partial(filter_by_argument_name, argument_names))

    event_abi_candidates = cast(Sequence[ABIEvent], pipe(abi, *filters))

    if len(event_abi_candidates) == 1:
        return event_abi_candidates[0]
    elif len(event_abi_candidates) == 0:
        raise Web3ValueError("No matching events found")
    else:
        raise Web3ValueError("Multiple events found")


def get_event_log_topics(
    event_abi: ABIEvent,
    topics: Optional[Sequence[HexBytes]] = None,
) -> Sequence[HexBytes]:
    r"""
    Return topics for an event ABI.

    :param event_abi: Event ABI.
    :type event_abi: `ABIEvent`
    :param topics: Transaction topics from a `LogReceipt`.
    :type topics: `list[HexBytes]`
    :return: Event topics for the event ABI.
    :rtype: `list[HexBytes]`

    .. doctest::

        >>> from web3.utils import get_event_log_topics
        >>> abi = {
        ...   'type': 'event',
        ...   'anonymous': False,
        ...   'name': 'MyEvent',
        ...   'inputs': [
        ...     {
        ...       'name': 's',
        ...       'type': 'uint256'
        ...     }
        ...   ]
        ... }
        >>> keccak_signature = b'l+Ff\xba\x8d\xa5\xa9W\x17b\x1d\x87\x9aw\xder_=\x81g\t\xb9\xcb\xe9\xf0Y\xb8\xf8u\xe2\x84'  # noqa: E501
        >>> get_event_log_topics(abi, [keccak_signature, '0x1', '0x2'])
        ['0x1', '0x2']
    """
    if topics is None:
        topics = []

    if event_abi["anonymous"]:
        return topics
    elif len(topics) == 0:
        raise MismatchedABI("Expected non-anonymous event to have 1 or more topics")
    elif event_abi_to_log_topic(event_abi) != log_topic_to_bytes(topics[0]):
        raise MismatchedABI("The event signature did not match the provided ABI")
    else:
        return topics[1:]


def log_topic_to_bytes(
    log_topic: Union[Primitives, HexStr, str],
) -> bytes:
    r"""
    Return topic signature as bytes.

    :param log_topic: Event topic from a `LogReceipt`.
    :type log_topic: `Primitive`, `HexStr` or `str`
    :return: Topic signature as bytes.
    :rtype: `bytes`

    .. doctest::

        >>> from web3.utils import log_topic_to_bytes
        >>> log_topic_to_bytes('0xa12fd1')
        b'\xa1/\xd1'
    """
    return hexstr_if_str(to_bytes, log_topic)
