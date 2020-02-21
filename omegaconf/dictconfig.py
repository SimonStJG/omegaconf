import copy
from enum import Enum
from typing import (
    AbstractSet,
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    MutableMapping,
    Optional,
    Tuple,
    Type,
    Union,
)

from ._utils import (
    ValueKind,
    _is_interpolation,
    get_dict_key_value_types,
    get_structured_config_data,
    get_type_of,
    get_value_kind,
    is_dict,
    is_primitive_dict,
    is_structured_config,
    is_structured_config_frozen,
)
from .base import Container, ContainerMetadata, Node
from .basecontainer import DEFAULT_VALUE_MARKER, BaseContainer
from .errors import (
    CompactKeyError,
    ConfigAttributeError,
    ConfigKeyError,
    KeyValidationError,
    MissingMandatoryValue,
    ReadonlyConfigError,
    UnsupportedInterpolationType,
    ValidationError,
)
from .nodes import EnumNode, ValueNode


class DictConfig(BaseContainer, MutableMapping[str, Any]):

    _metadata: ContainerMetadata

    def __init__(
        self,
        content: Union[Dict[str, Any], Any],
        key: Any = None,
        parent: Optional[Container] = None,
        ref_type: Optional[Type[Any]] = None,
        is_optional: bool = True,
    ) -> None:
        key_type, element_type = get_dict_key_value_types(ref_type)
        super().__init__(
            parent=parent,
            metadata=ContainerMetadata(
                key=key,
                optional=is_optional,
                ref_type=ref_type,
                object_type=None,
                key_type=key_type,
                element_type=element_type,
            ),
        )

        if is_structured_config(content) or is_structured_config(ref_type):
            self._set_value(content)
            if is_structured_config_frozen(content) or is_structured_config_frozen(
                ref_type
            ):
                self._set_flag("readonly", True)

        else:
            self._set_value(content)
            if isinstance(content, DictConfig):
                metadata = copy.deepcopy(content._metadata)
                metadata.key = key
                metadata.optional = is_optional
                metadata.element_type = element_type
                metadata.key_type = key_type
                self.__dict__["_metadata"] = metadata

    def __deepcopy__(self, memo: Dict[int, Any] = {}) -> "DictConfig":
        res = DictConfig({})
        for k, v in self.__dict__.items():
            res.__dict__[k] = copy.deepcopy(v, memo=memo)
        res._re_parent()
        return res

    def __copy__(self) -> "DictConfig":
        res = DictConfig(content=None)
        for k, v in self.__dict__.items():
            res.__dict__[k] = copy.copy(v)
        res._re_parent()
        return res

    def copy(self) -> "DictConfig":
        return copy.copy(self)

    def _validate_get(self, key: Any, value: Any = None) -> None:
        is_typed = self._metadata.object_type not in (Any, None,) and not is_dict(
            self._metadata.object_type
        )

        is_struct = self._get_flag("struct") is True
        if key not in self.__dict__["_content"]:
            if is_typed:
                # do not raise an exception if struct is explicitly set to False
                if self._get_node_flag("struct") is False:
                    return
            if is_typed or is_struct:
                if is_typed:
                    assert self._metadata.object_type is not None
                    msg = f"Key '{key}' not in '{self._metadata.object_type.__name__}'"
                else:
                    msg = f"Key '{key}' in not in struct"
                self._format_and_raise(
                    key=key, value=value, cause=ConfigAttributeError(msg)
                )

    def _validate_merge(self, key: Any, value: Any) -> None:
        self._validate_set_merge_impl(key, value, is_assign=False)

    def _validate_set(self, key: Any, value: Any) -> None:
        self._validate_set_merge_impl(key, value, is_assign=True)

    def _validate_set_merge_impl(self, key: Any, value: Any, is_assign: bool) -> None:
        from omegaconf import OmegaConf

        vk = get_value_kind(value)
        if vk == ValueKind.INTERPOLATION:
            return
        if isinstance(value, (str, ValueNode)) and vk == ValueKind.STR_INTERPOLATION:
            return

        if OmegaConf.is_none(value):
            if key is not None:
                node = self._get_node(key)
                if node is not None and not node._is_optional():
                    raise ValidationError("field '$FULL_KEY' is not Optional")
            else:
                if not self._is_optional():
                    raise ValidationError("field '$FULL_KEY' is not Optional")

        if value == "???":
            return

        # validate get
        if key is not None:
            try:
                self._validate_get(key, value)
            except AttributeError as e:
                raise ConfigAttributeError(f"Error setting $KEY=$VALUE : {e}")

        target: Optional[Node]
        if key is None:
            target = self
        else:
            target = self._get_node(key)

        if (target is not None and target._get_flag("readonly")) or self._get_flag(
            "readonly"
        ):
            if is_assign:
                msg = f"Cannot assign to read-only node : {value}"
            else:
                msg = f"Cannot merge into read-only node : {value}"
            raise ReadonlyConfigError(msg)

        if target is None:
            return

        def is_typed(c: Any) -> bool:
            return isinstance(c, DictConfig) and c._metadata.ref_type not in (Any, dict)

        if not is_typed(target):
            return

        # target must be optional by now. no need to check the type of value if None.
        if value is None:
            return

        target_type = OmegaConf.get_ref_type(target)
        value_type = OmegaConf.get_type(value)

        if is_dict(value_type) and is_dict(target_type):
            return

        if is_assign:
            # assign
            validation_error = (
                target_type is not None
                and value_type is not None
                and not issubclass(value_type, target_type)
            )
        else:
            # merge
            validation_error = not is_dict(value_type) and (
                target_type is not None
                and value_type is not None
                and not issubclass(value_type, target_type)
            )

        if validation_error:
            assert value_type is not None
            assert target_type is not None
            msg = (
                f"Invalid type assigned : {value_type.__name__} is not a "
                f"subclass of {target_type.__name__}. value: {value}"
            )
            raise ValidationError(msg)

    def _validate_and_normalize_key(self, key: Any) -> Union[str, Enum]:
        return self._s_validate_and_normalize_key(self._metadata.key_type, key)

    def _s_validate_and_normalize_key(
        self, key_type: Any, key: Any
    ) -> Union[str, Enum]:
        if key_type is None:
            for t in (str, Enum):
                try:
                    return self._s_validate_and_normalize_key(key_type=t, key=key)
                except KeyValidationError:
                    pass
            raise KeyValidationError("Incompatible key type '$KEY_TYPE'")
        else:
            if key_type == str:
                if not isinstance(key, str):
                    raise KeyValidationError(
                        f"Key $KEY ($KEY_TYPE) is incompatible with ({key_type.__name__})"
                    )
                return key
            elif issubclass(key_type, Enum):
                try:
                    ret = EnumNode.validate_and_convert_to_enum(key_type, key)
                    assert ret is not None
                    return ret
                except ValidationError as e:
                    raise KeyValidationError(
                        f"Key '$KEY' is incompatible with ({key_type.__name__}) : {e}"
                    )
            else:
                assert False

    def __setitem__(self, key: Union[str, Enum], value: Any) -> None:

        try:
            self.__set_impl(key=key, value=value)
        except AttributeError as e:
            self._format_and_raise(
                key=key, value=value, type_override=ConfigKeyError, cause=e
            )
        except Exception as e:
            self._format_and_raise(key=key, value=value, cause=e)

    def __set_impl(self, key: Union[str, Enum], value: Any) -> None:
        if isinstance(key, str):
            sub_keys = key.split(".")
            cur = self
            for idx, k in enumerate(sub_keys):
                if k == "":
                    raise CompactKeyError(f"Invalid key '{key}'")
                if idx == len(sub_keys) - 1:
                    # last key
                    if isinstance(cur, DictConfig):
                        cur.__set_single_key_val_impl(key=k, value=value)
                    else:
                        raise CompactKeyError(
                            f"Conflict detected inserting compact key '{key}'  "
                        )
                else:
                    if k.endswith("\\"):
                        sub_keys[idx + 1] = f"{k[0:-1]}.{sub_keys[idx+1]}"
                        continue
                    if k not in cur:
                        cur.__set_single_key_val_impl(key=k, value={})
                    cur = cur[k]
        else:
            self.__set_single_key_val_impl(key=key, value=value)

    def __set_single_key_val_impl(self, key: Union[str, Enum], value: Any) -> None:
        key = self._validate_and_normalize_key(key)
        self._set_item_impl(key, value)

    # hide content while inspecting in debugger
    def __dir__(self) -> Iterable[str]:
        if self._is_missing() or self._is_none():
            return []
        return self.__dict__["_content"].keys()  # type: ignore

    def __setattr__(self, key: str, value: Any) -> None:
        """
        Allow assigning attributes to DictConfig
        :param key:
        :param value:
        :return:
        """
        try:
            self.__set_impl(key, value)
        except Exception as e:
            self._format_and_raise(key=key, value=value, cause=e)
            assert False

    def __getattr__(self, key: str) -> Any:
        """
        Allow accessing dictionary values as attributes
        :param key:
        :return:
        """
        try:
            # PyCharm is sometimes inspecting __members__, be sure to tell it we don't have that.
            if key == "__members__":
                raise ConfigAttributeError()

            if key == "__name__":
                raise ConfigAttributeError()

            return self._get_impl(key=key, default_value=DEFAULT_VALUE_MARKER)
        except Exception as e:
            self._format_and_raise(key=key, value=None, cause=e)

    def __getitem__(self, key: Union[str, Enum]) -> Any:
        """
        Allow map style access
        :param key:
        :return:
        """

        try:
            return self._get_impl(key=key, default_value=DEFAULT_VALUE_MARKER)
        except AttributeError as e:
            self._format_and_raise(
                key=key, value=None, cause=e, type_override=ConfigKeyError
            )
        except Exception as e:
            self._format_and_raise(key=key, value=None, cause=e)

    def __delitem__(self, key: Union[str, int, Enum]) -> None:
        if self._get_flag("readonly"):
            self._format_and_raise(
                key=key,
                value=None,
                cause=ReadonlyConfigError(
                    "Cannot delete item from read-only DictConfig"
                ),
            )
        del self.__dict__["_content"][key]

    def get(
        self, key: Union[str, Enum], default_value: Any = DEFAULT_VALUE_MARKER,
    ) -> Any:
        try:
            return self._get_impl(key=key, default_value=default_value)
        except Exception as e:
            self._format_and_raise(key=key, value=None, cause=e)

    def _get_impl(self, key: Union[str, Enum], default_value: Any) -> Any:
        try:
            node = self._get_node(key=key)
        except ConfigAttributeError:
            if default_value != DEFAULT_VALUE_MARKER:
                node = default_value
            else:
                raise
        return self._resolve_with_default(
            key=key, value=node, default_value=default_value,
        )

    def _get_node(
        self, key: Union[str, Enum], validate_access: bool = True,
    ) -> Optional[Node]:
        try:
            key = self._validate_and_normalize_key(key)
        except KeyValidationError:
            if validate_access:
                raise
            else:
                return None
        value: Node = self.__dict__["_content"].get(key)
        if validate_access:
            self._validate_get(key)

        return value

    def pop(self, key: Union[str, Enum], default: Any = DEFAULT_VALUE_MARKER) -> Any:
        try:
            key = self._validate_and_normalize_key(key)
            if self._get_flag("readonly"):
                self._format_and_raise(
                    key=key,
                    value=None,
                    cause=ReadonlyConfigError("Cannot pop from read-only node"),
                )

            node = self._get_node(key=key)
            if node is not None:
                value = self._resolve_with_default(
                    key=key, value=node, default_value=default
                )
                del self[key]
                return value
            else:
                if default is not DEFAULT_VALUE_MARKER:
                    return default
                else:
                    full = self._get_full_key(key)
                    if full != key:
                        raise ConfigKeyError(f"Key not found: '{key}' (path: '{full}')")
                    else:
                        raise ConfigKeyError(f"Key not found: '{key}'")
        except Exception as e:
            self._format_and_raise(key=key, value=None, cause=e)

    def keys(self) -> Any:
        if self._is_missing() or self._is_interpolation() or self._is_none():
            return list()
        return self.__dict__["_content"].keys()

    def __contains__(self, key: object) -> bool:
        """
        A key is contained in a DictConfig if there is an associated value and
        it is not a mandatory missing value ('???').
        :param key:
        :return:
        """

        key = self._validate_and_normalize_key(key)
        try:
            node: Optional[Node] = self._get_node(key)
        except (KeyError, AttributeError):
            node = None

        if node is None:
            return False
        else:
            try:
                self._resolve_with_default(key=key, value=node)
                return True
            except UnsupportedInterpolationType:
                # Value that has unsupported interpolation counts as existing.
                return True
            except (MissingMandatoryValue, KeyError):
                return False

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())

    def items(self) -> AbstractSet[Tuple[str, Any]]:
        return self.items_ex(resolve=True, keys=None)

    def items_ex(
        self, resolve: bool = True, keys: Optional[List[str]] = None
    ) -> AbstractSet[Tuple[str, Any]]:
        # Using a dictionary because the keys are ordered
        items: Dict[Tuple[str, Any], None] = {}
        for key in self.keys():
            if resolve:
                value = self.get(key)
            else:
                value = self.__dict__["_content"][key]
                if isinstance(value, ValueNode):
                    value = value._value()
            if keys is None or key in keys:
                items[(key, value)] = None

        return items.keys()

    def __eq__(self, other: Any) -> bool:
        if other is None:
            return self.__dict__["_content"] is None
        if is_primitive_dict(other) or is_structured_config(other):
            other = DictConfig(other)
            return DictConfig._dict_conf_eq(self, other)
        if isinstance(other, DictConfig):
            return DictConfig._dict_conf_eq(self, other)
        return NotImplemented

    def __ne__(self, other: Any) -> bool:
        x = self.__eq__(other)
        if x is not NotImplemented:
            return not x
        return NotImplemented

    def __hash__(self) -> int:
        return hash(str(self))

    def _promote(self, type_or_prototype: Optional[Type[Any]]) -> None:
        """
        Retypes a node.
        This should only be used in rare circumstances, where you want to dynamically change
        the runtime structured-type of a DictConfig.
        It will change the type and add the additional fields based on the input class or object
        """
        if type_or_prototype is None:
            return
        if not is_structured_config(type_or_prototype):
            raise ValueError(f"Expected structured config class : {type_or_prototype}")

        from omegaconf import OmegaConf

        proto: DictConfig = OmegaConf.structured(type_or_prototype)
        object_type = proto._metadata.object_type
        # remove the type to prevent assignment validation from rejecting the promotion.
        proto._metadata.object_type = None
        self.merge_with(proto)
        # restore the type.
        self._metadata.object_type = object_type

    def _set_value(self, value: Any) -> None:
        from omegaconf import OmegaConf

        assert not isinstance(value, ValueNode)
        self._validate_set(key=None, value=value)

        if OmegaConf.is_none(value):
            self.__dict__["_content"] = None
            self._metadata.object_type = None
        elif _is_interpolation(value):
            self.__dict__["_content"] = value
            self._metadata.object_type = None
        elif value == "???":
            self.__dict__["_content"] = "???"
            self._metadata.object_type = None
        else:
            self.__dict__["_content"] = {}
            if is_structured_config(value):
                self._metadata.object_type = None
                data = get_structured_config_data(value)
                for k, v in data.items():
                    self.__setitem__(k, v)
                self._metadata.object_type = get_type_of(value)
            elif isinstance(value, DictConfig):
                self._metadata.object_type = dict
                for k, v in value.items_ex(resolve=False):
                    self.__setitem__(k, v)
                self.__dict__["_metadata"] = copy.deepcopy(value._metadata)

            elif isinstance(value, dict):
                self._metadata.object_type = self._metadata.ref_type
                for k, v in value.items():
                    self.__setitem__(k, v)
            else:
                assert False, f"Unsupported value type : {value}"  # pragma: no cover

    @staticmethod
    def _dict_conf_eq(d1: "DictConfig", d2: "DictConfig") -> bool:

        d1_none = d1.__dict__["_content"] is None
        d2_none = d2.__dict__["_content"] is None
        if d1_none and d2_none:
            return True
        if d1_none != d2_none:
            return False

        assert isinstance(d1, DictConfig)
        assert isinstance(d2, DictConfig)
        if len(d1) != len(d2):
            return False
        for k, v in d1.items_ex(resolve=False):
            if k not in d2.__dict__["_content"]:
                return False
            if not BaseContainer._item_eq(d1, k, d2, k):
                return False

        return True
