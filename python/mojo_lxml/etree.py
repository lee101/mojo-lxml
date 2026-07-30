"""A focused lxml.etree-compatible API backed by Mojo parsing and XPath kernels."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, MutableMapping
from dataclasses import dataclass
from functools import lru_cache
import html as _html_stdlib
import io
import os
import re
import threading
from typing import Any

import numpy as np

from ._lib import addr, i64, lib, u64


class LxmlError(Exception):
    pass


class ParserError(LxmlError):
    pass


class XMLSyntaxError(SyntaxError, LxmlError):
    pass


_PREDEFINED = {"amp": "&", "lt": "<", "gt": ">", "apos": "'", "quot": '"'}
_VOID_HTML = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
_BOOLEAN_HTML = {
    "allowfullscreen", "async", "autofocus", "autoplay", "checked", "controls",
    "default", "defer", "disabled", "formnovalidate", "hidden", "inert", "ismap",
    "itemscope", "loop", "multiple", "muted", "nomodule", "novalidate", "open",
    "playsinline", "readonly", "required", "reversed", "selected",
}
_HTML_CLOSE_GROUPS = {
    "li": frozenset(("li",)),
    "dt": frozenset(("dt", "dd")),
    "dd": frozenset(("dt", "dd")),
    "tr": frozenset(("tr",)),
    "td": frozenset(("td", "th")),
    "th": frozenset(("td", "th")),
    "option": frozenset(("option",)),
}
_HTML_BLOCK_TAGS = frozenset(
    (
        "address", "article", "aside", "blockquote", "div", "dl", "fieldset",
        "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
        "menu", "nav", "ol", "p", "pre", "section", "table", "ul",
    )
)
_HTML_HEAD_TAGS = frozenset(("base", "link", "meta", "title", "style"))
_INVALID_XML_CHAR = re.compile(
    "[^\u0009\u000a\u000d\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
)


def _decode_entities(value: str, html: bool = False) -> str:
    if html:
        return _html_stdlib.unescape(value)
    if "&" not in value:
        if value.isprintable() or _INVALID_XML_CHAR.search(value) is None:
            return value
        raise XMLSyntaxError(f"PCDATA invalid Char value {ord(_INVALID_XML_CHAR.search(value).group())}")

    pieces = []
    position = 0
    while True:
        amp = value.find("&", position)
        if amp < 0:
            pieces.append(value[position:])
            break
        pieces.append(value[position:amp])
        semicolon = value.find(";", amp + 1)
        if semicolon < 0:
            raise XMLSyntaxError("EntityRef: expecting ';'")
        entity = value[amp + 1:semicolon]
        if entity in _PREDEFINED:
            replacement = _PREDEFINED[entity]
        elif re.fullmatch(r"#[xX][0-9A-Fa-f]+", entity):
            number = int(entity[2:], 16)
            try:
                replacement = chr(number)
            except ValueError as exc:
                raise XMLSyntaxError(f"invalid character reference: {entity}") from exc
        elif re.fullmatch(r"#[0-9]+", entity):
            number = int(entity[1:])
            try:
                replacement = chr(number)
            except ValueError as exc:
                raise XMLSyntaxError(f"invalid character reference: {entity}") from exc
        elif re.fullmatch(r"[A-Za-z_:][\w.:-]*", entity):
            raise XMLSyntaxError(f"Entity '{entity}' not defined")
        elif entity.startswith("#"):
            raise XMLSyntaxError(f"invalid character reference: {entity}")
        else:
            raise XMLSyntaxError("EntityRef: no name")
        pieces.append(replacement)
        position = semicolon + 1

    result = "".join(pieces)
    for character in result:
        codepoint = ord(character)
        if not (
            codepoint in (9, 10, 13)
            or 0x20 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        ):
            raise XMLSyntaxError(f"PCDATA invalid Char value {codepoint}")
    return result


@lru_cache(maxsize=1024)
def _validate_xml_name(name: str) -> None:
    if name.count(":") > 1:
        raise XMLSyntaxError(f"Failed to parse QName '{name}'")
    parts = name.split(":")
    for part in parts:
        if not part or not (part[0].isalpha() or part[0] == "_"):
            raise XMLSyntaxError(f"Invalid XML name '{name}'")
        if any(not (char.isalnum() or char in "._-") for char in part[1:]):
            raise XMLSyntaxError(f"Invalid XML name '{name}'")


def _qname(raw: str, namespaces: dict[str | None, str], attribute: bool = False) -> str:
    if ":" in raw:
        prefix, local = raw.split(":", 1)
        if prefix not in namespaces:
            raise XMLSyntaxError(f"Namespace prefix {prefix} is not defined")
        return f"{{{namespaces[prefix]}}}{local}"
    if not attribute and namespaces.get(None):
        return f"{{{namespaces[None]}}}{raw}"
    return raw


class _Attrib(MutableMapping[str, str]):
    __slots__ = ("_owner", "_values")

    def __init__(self, owner: Element, values: dict[str, str] | None = None):
        self._owner = owner
        self._values = {} if values is None else dict(values)

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._values[str(key)] = str(value)
        self._owner._touch()

    def __delitem__(self, key: str) -> None:
        del self._values[key]
        self._owner._touch()

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return repr(self._values)


class Element:
    __slots__ = (
        "_tag",
        "attrib",
        "_text",
        "tail",
        "_children",
        "_parent",
        "nsmap",
        "_flat_cache",
        "_synthetic_text_wrapper",
    )

    def __init__(
        self,
        _tag: str,
        attrib: dict[str, Any] | None = None,
        nsmap: dict[str | None, str] | None = None,
        **extra: Any,
    ):
        if not isinstance(_tag, str) or not _tag:
            raise ValueError("Invalid tag name")
        self._tag = _tag
        values = None
        if attrib or extra:
            values = {str(k): str(v) for k, v in (attrib or {}).items()}
            values.update({str(k): str(v) for k, v in extra.items()})
        self.attrib = _Attrib(self, values)
        self._text: str | None = None
        self.tail: str | None = None
        self._children: list[Element] = []
        self._parent: Element | None = None
        self.nsmap = dict(nsmap or {})
        self._flat_cache = None

    @property
    def tag(self) -> str:
        return self._tag

    @tag.setter
    def tag(self, value: str) -> None:
        self._tag = value
        self._touch()

    @property
    def text(self) -> str | None:
        return self._text

    @text.setter
    def text(self, value: str | None) -> None:
        self._text = value
        self._touch()

    def _touch(self) -> None:
        root = self
        while root._parent is not None:
            root = root._parent
        root._flat_cache = None

    def __len__(self) -> int:
        return len(self._children)

    def __iter__(self) -> Iterator[Element]:
        return iter(self._children)

    def __getitem__(self, index):
        return self._children[index]

    def __setitem__(self, index, value: Element) -> None:
        self._touch()
        if isinstance(index, slice):
            old = self._children[index]
            replacements = list(value)
            if not all(isinstance(child, Element) for child in replacements):
                raise TypeError("expected Elements")
            for child in old:
                child._parent = None
            for child in replacements:
                child._parent = self
            self._children[index] = replacements
        else:
            if not isinstance(value, Element):
                raise TypeError("expected an Element")
            self._children[index]._parent = None
            value._parent = self
            self._children[index] = value

    def __delitem__(self, index) -> None:
        self._touch()
        removed = self._children[index]
        if isinstance(removed, list):
            for child in removed:
                child._parent = None
        else:
            removed._parent = None
        del self._children[index]

    def __repr__(self) -> str:
        return f"<Element {self.tag!r} at {hex(id(self))}>"

    def append(self, element: Element) -> None:
        if not isinstance(element, Element):
            raise TypeError("expected an Element")
        if element._parent is not None:
            element._parent.remove(element)
        self._touch()
        element._parent = self
        self._children.append(element)

    def extend(self, elements: Iterable[Element]) -> None:
        for element in elements:
            self.append(element)

    def insert(self, index: int, element: Element) -> None:
        if element._parent is not None:
            element._parent.remove(element)
        self._touch()
        element._parent = self
        self._children.insert(index, element)

    def remove(self, element: Element) -> None:
        self._touch()
        self._children.remove(element)
        element._parent = None

    def clear(self, keep_tail: bool = False) -> None:
        self._touch()
        for child in self._children:
            child._parent = None
        self._children.clear()
        self.attrib.clear()
        self.text = None
        if not keep_tail:
            self.tail = None

    def get(self, key: str, default: Any = None) -> Any:
        return self.attrib.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.attrib[key] = value

    def keys(self):
        return self.attrib.keys()

    def values(self):
        return self.attrib.values()

    def items(self):
        return self.attrib.items()

    def getparent(self) -> Element | None:
        return self._parent

    def getchildren(self) -> list[Element]:
        return list(self._children)

    def getnext(self) -> Element | None:
        if self._parent is None:
            return None
        siblings = self._parent._children
        index = siblings.index(self)
        return siblings[index + 1] if index + 1 < len(siblings) else None

    def getprevious(self) -> Element | None:
        if self._parent is None:
            return None
        siblings = self._parent._children
        index = siblings.index(self)
        return siblings[index - 1] if index else None

    def getroottree(self) -> ElementTree:
        root = self
        while root._parent is not None:
            root = root._parent
        return ElementTree(root)

    def iter(self, tag: str | None = None, *tags: str) -> Iterator[Element]:
        wanted = None if tag is None and not tags else {tag, *tags}
        stack = [self]
        while stack:
            node = stack.pop()
            if wanted is None or "*" in wanted or node.tag in wanted:
                yield node
            stack.extend(reversed(node._children))

    def iterchildren(self, tag: str | None = None, reversed: bool = False):
        children = self._children[::-1] if reversed else self._children
        for child in children:
            if tag is None or tag == "*" or child.tag == tag:
                yield child

    def itertext(self, with_tail: bool = True) -> Iterator[str]:
        if self.text:
            yield self.text
        for child in self._children:
            yield from child.itertext(with_tail=with_tail)
            if with_tail and child.tail:
                yield child.tail

    def find(self, path: str, namespaces: dict[str, str] | None = None):
        values = self.xpath(path, namespaces=namespaces)
        return values[0] if values else None

    def findall(self, path: str, namespaces: dict[str, str] | None = None):
        return self.xpath(path, namespaces=namespaces)

    def findtext(
        self, path: str, default: Any = None, namespaces: dict[str, str] | None = None
    ):
        found = self.find(path, namespaces=namespaces)
        return default if found is None else found.text

    def xpath(
        self,
        _path: str,
        namespaces: dict[str, str] | None = None,
        extensions=None,
        smart_strings: bool = True,
        **variables,
    ):
        if extensions or variables:
            raise NotImplementedError("XPath extensions and variables are not covered")
        return _xpath(self, _path, namespaces or {})


class ElementTree:
    def __init__(self, element: Element | None = None, file=None, parser=None):
        if file is not None:
            parsed = parse(file, parser)
            self._root = parsed._root
        else:
            self._root = element

    def getroot(self) -> Element | None:
        return self._root

    def _setroot(self, root: Element) -> None:
        self._root = root
        root._parent = None

    def find(self, path: str, namespaces=None):
        return self._root.find(path, namespaces) if self._root is not None else None

    def findall(self, path: str, namespaces=None):
        return self._root.findall(path, namespaces) if self._root is not None else []

    def findtext(self, path: str, default=None, namespaces=None):
        return (
            self._root.findtext(path, default, namespaces)
            if self._root is not None
            else default
        )

    def iter(self, tag: str | None = None):
        return self._root.iter(tag) if self._root is not None else iter(())

    def xpath(self, _path: str, namespaces=None, extensions=None, smart_strings=True, **variables):
        if self._root is None:
            return []
        return self._root.xpath(
            _path, namespaces, extensions, smart_strings, **variables
        )

    def write(self, file, encoding="ASCII", xml_declaration=None, pretty_print=False, method="xml"):
        value = tostring(
            self._root,
            encoding=encoding,
            xml_declaration=xml_declaration,
            pretty_print=pretty_print,
            method=method,
        )
        if hasattr(file, "write"):
            file.write(value)
        else:
            mode = "w" if isinstance(value, str) else "wb"
            with open(file, mode) as stream:
                stream.write(value)


@dataclass
class XMLParser:
    encoding: str | None = None
    attribute_defaults: bool = False
    dtd_validation: bool = False
    load_dtd: bool = False
    no_network: bool = True
    ns_clean: bool = False
    recover: bool = False
    remove_blank_text: bool = False
    remove_comments: bool = False
    remove_pis: bool = False
    strip_cdata: bool = True
    resolve_entities: bool = True
    huge_tree: bool = False


@dataclass
class HTMLParser:
    encoding: str | None = None
    recover: bool = True
    remove_blank_text: bool = False
    remove_comments: bool = False
    remove_pis: bool = False
    no_network: bool = True
    huge_tree: bool = False


def _source_bytes(text: str | bytes | bytearray | memoryview, encoding=None) -> bytes:
    if isinstance(text, str):
        return text.encode(encoding or "utf-8")
    if isinstance(text, (bytes, bytearray, memoryview)):
        return bytes(text)
    raise TypeError("can only parse strings or bytes")


def _syntax_error(data: bytes, position: int, code: int) -> XMLSyntaxError:
    position = max(0, min(position, len(data)))
    line = data.count(b"\n", 0, position) + 1
    last = data.rfind(b"\n", 0, position)
    column = position + 1 if last < 0 else position - last
    messages = {
        1: "Extra content at the end of the document",
        3: "Comment not terminated",
        4: "CData section not finished",
        5: "Processing instruction not terminated",
        6: "DOCTYPE not terminated",
        7: "Malformed closing tag",
        8: "Opening and ending tag mismatch",
        11: "Extra content at the end of the document",
        12: "Start tag not terminated",
        13: "Attribute value missing",
        14: "Unescaped quote in attribute value",
        15: "AttValue: \" or ' expected",
        16: "Specification mandates value for attribute",
        17: "Premature end of data",
    }
    message = messages.get(code, "Malformed markup")
    return XMLSyntaxError(f"{message}, line {line}, column {column}")


def _tokenize(data: bytes, html_mode: bool):
    raw = np.frombuffer(data, dtype=np.uint8)
    tag_count = data.count(b"<")
    capacity = max(64, 2 * tag_count + data.count(b"=") + 1)
    stack_capacity = max(1, tag_count + 1)
    stack = np.empty((2, stack_capacity), dtype=np.int64)
    error = np.empty(2, dtype=np.int64)
    while True:
        fields = np.empty((5, capacity), dtype=np.int64)
        count = lib().mlx_tokenize(
            addr(raw), len(data), *(addr(field) for field in fields),
            addr(stack[0]), addr(stack[1]), capacity, int(html_mode), addr(error)
        )
        if count >= 0:
            return tuple(field[:count] for field in fields)
        if int(error[1]) != 2 or capacity >= len(data) + 1:
            raise _syntax_error(data, int(error[0]), int(error[1]))
        capacity = min(len(data) + 1, capacity * 2)


def _append_text(element: Element, value: str) -> None:
    if not value:
        return
    element._touch()
    if element._children:
        child = element._children[-1]
        child.tail = (child.tail or "") + value
    else:
        element._text = (element._text or "") + value


def _append_parsed_text(element: Element, value: str) -> None:
    if not value:
        return
    if element._children:
        child = element._children[-1]
        child.tail = (child.tail or "") + value
    else:
        element._text = (element._text or "") + value


def _parse_markup(data: bytes, parser: XMLParser | HTMLParser, html_mode: bool) -> Element:
    if not data.isascii():
        try:
            data.decode(parser.encoding or "utf-8")
        except UnicodeDecodeError as exc:
            raise XMLSyntaxError(str(exc)) from exc
    kinds, aa, bb, cc, dd = _tokenize(data, html_mode)
    kinds = memoryview(kinds)
    aa = memoryview(aa)
    bb = memoryview(bb)
    cc = memoryview(cc)
    dd = memoryview(dd)
    stack: list[Element] = []
    roots: list[Element] = []
    orphan_text = ""

    def piece(start: int, end: int) -> str:
        return data[start:end].decode(parser.encoding or "utf-8")

    index = 0
    token_count = len(kinds)
    while index < token_count:
        kind = kinds[index]
        if kind == 1:
            raw_tag = piece(int(aa[index]), int(bb[index]))
            if html_mode:
                raw_tag = raw_tag.lower()
                closable = _HTML_CLOSE_GROUPS.get(raw_tag)
                if closable:
                    for pos in range(len(stack) - 1, -1, -1):
                        if stack[pos].tag in closable:
                            del stack[pos:]
                            break
                if raw_tag in _HTML_BLOCK_TAGS:
                    for pos in range(len(stack) - 1, -1, -1):
                        if stack[pos].tag == "p":
                            del stack[pos:]
                            break
            elif not html_mode:
                _validate_xml_name(raw_tag)

            attr_count = int(dd[index])
            attr_stop = index + attr_count + 1
            ns = stack[-1].nsmap if stack else {}
            ns_copied = False
            for attr_index in range(index + 1, attr_stop):
                raw_name = piece(int(aa[attr_index]), int(bb[attr_index]))
                if raw_name == "xmlns" or raw_name.startswith("xmlns:"):
                    if not ns_copied:
                        ns = dict(ns)
                        ns_copied = True
                    raw_value = piece(int(cc[attr_index]), int(dd[attr_index]))
                    ns[None if raw_name == "xmlns" else raw_name[6:]] = raw_value

            resolved_tag = (
                raw_tag if html_mode or not ns else _qname(raw_tag, ns)
            )
            element = Element.__new__(Element)
            element._tag = resolved_tag
            element.attrib = _Attrib(element)
            element._text = None
            element.tail = None
            element._children = []
            element._parent = None
            element.nsmap = ns
            element._flat_cache = None
            seen = set() if attr_count else None
            for attr_index in range(index + 1, attr_stop):
                raw_name = piece(int(aa[attr_index]), int(bb[attr_index]))
                if raw_name == "xmlns" or raw_name.startswith("xmlns:"):
                    continue
                raw_value = piece(int(cc[attr_index]), int(dd[attr_index]))
                if html_mode:
                    raw_name = raw_name.lower()
                    if b"=" not in data[int(bb[attr_index]):int(cc[attr_index])]:
                        raw_value = raw_name
                    name = raw_name
                else:
                    _validate_xml_name(raw_name)
                    name = (
                        _qname(raw_name, ns, attribute=True) if ns else raw_name
                    )
                if name in seen and not html_mode:
                    raise XMLSyntaxError(f"Attribute {name} redefined")
                seen.add(name)
                element.attrib._values[name] = _decode_entities(raw_value, html_mode)

            is_self_closing = bool(cc[index])
            if html_mode and raw_tag in _VOID_HTML:
                is_self_closing = True
            if stack:
                element._parent = stack[-1]
                stack[-1]._children.append(element)
            else:
                roots.append(element)
            if not is_self_closing:
                stack.append(element)
            index = attr_stop
            continue
        elif kind == 3:
            if stack:
                value = piece(int(aa[index]), int(bb[index]))
                if not int(cc[index]):
                    value = _decode_entities(value, html_mode)
                if not (parser.remove_blank_text and not value.strip()):
                    _append_parsed_text(stack[-1], value)
            elif html_mode:
                orphan_text += _decode_entities(
                    piece(int(aa[index]), int(bb[index])), True
                )
        elif kind == 4:
            raw_end = piece(int(aa[index]), int(bb[index]))
            raw_end = raw_end.lower() if html_mode else raw_end
            if html_mode:
                for pos in range(len(stack) - 1, -1, -1):
                    if stack[pos].tag == raw_end:
                        del stack[pos:]
                        break
            elif stack:
                stack.pop()
        index += 1
    if not roots and html_mode and orphan_text:
        span = Element("span")
        span.text = orphan_text
        span._synthetic_text_wrapper = True
        return span
    if not roots:
        raise XMLSyntaxError("Document is empty")
    if html_mode and len(roots) > 1:
        wrapper = Element("html")
        head = None
        body = None
        for root in roots:
            if root.tag in _HTML_HEAD_TAGS and body is None:
                if head is None:
                    head = SubElement(wrapper, "head")
                head.append(root)
            else:
                if body is None:
                    body = SubElement(wrapper, "body")
                body.append(root)
        return wrapper
    return roots[0]


def _ensure_html_document(root: Element) -> Element:
    if root.tag == "html":
        return root
    wrapper = Element("html")
    body = None
    if root.tag in _HTML_HEAD_TAGS:
        SubElement(wrapper, "head").append(root)
    else:
        body = SubElement(wrapper, "body")
        if getattr(root, "_synthetic_text_wrapper", False):
            body.text = root.text
        else:
            body.append(root)
    return wrapper


def fromstring(text, parser: XMLParser | HTMLParser | None = None, base_url=None):
    parser = parser or XMLParser()
    html_mode = isinstance(parser, HTMLParser)
    root = _parse_markup(_source_bytes(text, parser.encoding), parser, html_mode)
    return _ensure_html_document(root) if html_mode else root


XML = fromstring


def HTML(text, parser: HTMLParser | None = None, base_url=None):
    parser = parser or HTMLParser()
    root = _parse_markup(_source_bytes(text, parser.encoding), parser, True)
    return _ensure_html_document(root)


def parse(source, parser: XMLParser | HTMLParser | None = None, base_url=None):
    if hasattr(source, "read"):
        data = source.read()
    else:
        with open(os.fspath(source), "rb") as stream:
            data = stream.read()
    return ElementTree(fromstring(data, parser, base_url))


def SubElement(
    _parent: Element,
    _tag: str,
    attrib: dict[str, Any] | None = None,
    nsmap: dict[str | None, str] | None = None,
    **extra: Any,
) -> Element:
    child = Element(_tag, attrib, nsmap, **extra)
    _parent.append(child)
    return child


def iselement(element) -> bool:
    return isinstance(element, Element)


def _split_clark(name: str) -> tuple[str | None, str]:
    if name.startswith("{"):
        uri, local = name[1:].split("}", 1)
        return uri, local
    return None, name


def QName(text_or_uri, tag=None):
    if tag is None:
        return str(text_or_uri)
    return f"{{{text_or_uri}}}{tag}"


def _display_name(name: str, nsmap: dict[str | None, str], attribute=False) -> str:
    uri, local = _split_clark(name)
    if uri is None:
        return local
    for prefix, value in nsmap.items():
        if value == uri and (prefix is not None or not attribute):
            return f"{prefix}:{local}" if prefix else local
    return name


def _escape_text(value: str) -> str:
    if "&" not in value and "<" not in value and ">" not in value:
        return value
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attr(value: str) -> str:
    if (
        "&" not in value
        and "<" not in value
        and ">" not in value
        and '"' not in value
    ):
        return value
    return _escape_text(value).replace('"', "&quot;")


def _serialize_into(
    element: Element, method: str, pretty: bool, pieces: list[str], level: int = 0
) -> None:
    if method == "text":
        pieces.extend(element.itertext())
        return
    html_mode = method == "html"
    indent = "  " * level if pretty else ""
    if element.nsmap:
        tag = _display_name(element._tag, element.nsmap)
        attrs: list[tuple[str, str]] = []
        parent_ns = element._parent.nsmap if element._parent else {}
        for prefix, uri in element.nsmap.items():
            if parent_ns.get(prefix) != uri:
                attrs.append(("xmlns" if prefix is None else f"xmlns:{prefix}", uri))
        attrs.extend(
            (_display_name(name, element.nsmap, attribute=True), value)
            for name, value in element.attrib._values.items()
        )
    else:
        tag = element._tag
        attrs = element.attrib._values.items()
    children = element._children
    element_text = element._text
    pieces.append(indent)
    pieces.append("<")
    pieces.append(tag)
    for name, value in attrs:
        pieces.append(" ")
        pieces.append(name)
        if not (
            html_mode
            and name.lower() in _BOOLEAN_HTML
            and value.lower() == name.lower()
        ):
            pieces.append('="')
            pieces.append(_escape_attr(value))
            pieces.append('"')
    if not children and element_text is None:
        if html_mode and tag.lower() in _VOID_HTML:
            pieces.append(">")
        else:
            pieces.append("/>")
        return
    pieces.append(">")
    if element_text:
        pieces.append(_escape_text(element_text))
    for child in children:
        if pretty and not element_text:
            pieces.append("\n")
        _serialize_into(child, method, pretty, pieces, level + 1)
        if child.tail:
            pieces.append(_escape_text(child.tail))
    if pretty and children and not children[-1].tail and not element_text:
        pieces.append("\n" + indent)
    pieces.append("</")
    pieces.append(tag)
    pieces.append(">")


def _serialize(element: Element, method: str, pretty: bool, level: int = 0) -> str:
    pieces: list[str] = []
    _serialize_into(element, method, pretty, pieces, level)
    return "".join(pieces)


def tostring(
    element_or_tree,
    encoding=None,
    method="xml",
    xml_declaration=None,
    pretty_print=False,
    with_tail=True,
    standalone=None,
    doctype=None,
    exclusive=False,
    inclusive_ns_prefixes=None,
    with_comments=True,
    strip_text=False,
):
    element = (
        element_or_tree.getroot()
        if isinstance(element_or_tree, ElementTree)
        else element_or_tree
    )
    if not isinstance(element, Element):
        raise TypeError("Type 'NoneType' cannot be serialized")
    if exclusive or inclusive_ns_prefixes or strip_text:
        raise NotImplementedError("canonical XML options are not covered")
    value = _serialize(element, method, pretty_print)
    if with_tail and element.tail:
        value += _escape_text(element.tail)
    if doctype:
        value = str(doctype) + ("\n" if pretty_print else "") + value
    if encoding == "unicode":
        if xml_declaration:
            raise ValueError("Serialisation to unicode must not request an XML declaration")
        return value
    codec = encoding or "ASCII"
    declaration = ""
    if xml_declaration or (xml_declaration is None and codec.upper() not in {"ASCII", "UTF-8", "UTF8"}):
        declaration = f"<?xml version='1.0' encoding='{codec}'"
        if standalone is not None:
            declaration += f" standalone='{'yes' if standalone else 'no'}'"
        declaration += "?>\n"
    return (declaration + value).encode(codec, errors="xmlcharrefreplace")


def tostringlist(element_or_tree, *args, **kwargs):
    return [tostring(element_or_tree, *args, **kwargs)]


def _resolve_xpath_name(name: str, namespaces: dict[str, str]) -> str:
    if name == "*" or name.startswith("{"):
        return name
    if ":" in name:
        prefix, local = name.split(":", 1)
        if prefix not in namespaces:
            raise XPathEvalError(f"Undefined namespace prefix: {prefix}")
        return f"{{{namespaces[prefix]}}}{local}"
    return name


class XPathError(LxmlError):
    pass


class XPathSyntaxError(XPathError):
    pass


class XPathEvalError(XPathError):
    pass


def _path_parts(path: str):
    parts = []
    current = []
    bracket = 0
    quote = None
    index = 0
    separator = None
    while index < len(path):
        char = path[index]
        if quote:
            current.append(char)
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
            current.append(char)
        elif char == "[":
            bracket += 1
            current.append(char)
        elif char == "]":
            bracket -= 1
            current.append(char)
        elif char == "/" and bracket == 0:
            if current:
                parts.append((separator, "".join(current)))
                current = []
            if index + 1 < len(path) and path[index + 1] == "/":
                separator = "//"
                index += 1
            else:
                separator = "/"
        else:
            current.append(char)
        index += 1
    if current:
        parts.append((separator, "".join(current)))
    return parts


_STEP = re.compile(r"^([^\[]+)((?:\[[^\]]+\])*)$")
_PRED = re.compile(r"\[([^\]]+)\]")
_ATTR_EQ = re.compile(r"^@([^=\s]+)\s*=\s*(['\"])(.*?)\2$")


def _compile_path(path: str, namespaces: dict[str, str]):
    terminal = None
    if path.endswith("/text()"):
        path, terminal = path[:-7], "text"
    else:
        match = re.search(r"/@([^/]+)$", path)
        if match:
            terminal = ("attr", _resolve_xpath_name(match.group(1), namespaces))
            path = path[:match.start()]
    absolute = path.startswith("/")
    parts = _path_parts(path)
    steps = []
    for part_index, (separator, raw) in enumerate(parts):
        if raw == ".":
            continue
        match = _STEP.match(raw)
        if not match:
            raise XPathSyntaxError(f"Invalid expression: {path}")
        raw_name, predicates = match.groups()
        name = _resolve_xpath_name(raw_name.strip(), namespaces)
        if absolute and part_index == 0 and separator == "/":
            axis = 2
        elif separator == "//":
            axis = 1
        else:
            axis = 0
        pred_mode = 0
        pred_name = ""
        pred_value = ""
        position = 0
        for predicate in _PRED.findall(predicates):
            predicate = predicate.strip()
            attr_eq = _ATTR_EQ.match(predicate)
            if predicate.startswith("@") and "=" not in predicate:
                pred_mode = 1
                pred_name = _resolve_xpath_name(predicate[1:].strip(), namespaces)
            elif attr_eq:
                pred_mode = 2
                pred_name = _resolve_xpath_name(attr_eq.group(1), namespaces)
                pred_value = attr_eq.group(3)
            elif predicate.isdigit():
                position = int(predicate)
            elif predicate.startswith("text()"):
                raise NotImplementedError(
                    f"XPath predicate [{predicate}] is not covered"
                )
            else:
                raise NotImplementedError(f"XPath predicate [{predicate}] is not covered")
        steps.append((axis, name, pred_mode, pred_name, pred_value, position))
    return absolute, steps, terminal


def _flatten(context: Element):
    root = context
    while root._parent is not None:
        root = root._parent
    if root._flat_cache is not None:
        return root._flat_cache
    nodes: list[Element] = []
    parents: list[int] = []
    lookup: dict[int, int] = {}
    pending = [(root, -1)]
    while pending:
        node, parent = pending.pop()
        index = len(nodes)
        nodes.append(node)
        parents.append(parent)
        lookup[id(node)] = index
        pending.extend((child, index) for child in reversed(node._children))
    offsets = [0]
    name_ids: dict[str, int] = {}
    value_ids: dict[str, int] = {}

    def symbol(table: dict[str, int], value: str) -> int:
        identifier = table.get(value)
        if identifier is None:
            identifier = len(table) + 1
            table[value] = identifier
        return identifier

    names = []
    values = []
    for node in nodes:
        for name, value in node.attrib.items():
            names.append(symbol(name_ids, name))
            values.append(symbol(value_ids, value))
        offsets.append(len(names))
    tag_ids: dict[str, int] = {}
    count = len(nodes)
    result = root, nodes, lookup, i64(0) if not parents else np.asarray(parents, dtype=np.int64), (
        np.asarray(offsets, dtype=np.int64),
        u64(names),
        u64(values),
    ), u64([symbol(tag_ids, node.tag) for node in nodes]), (
        np.empty(count, dtype=np.int64),
        np.empty(count, dtype=np.int64),
        np.zeros(count, dtype=np.int64),
        np.empty(count, dtype=np.int64),
        np.empty((count + 4095) // 4096, dtype=np.int64),
    ), (tag_ids, name_ids, value_ids), threading.Lock()
    root._flat_cache = result
    return result


def _select(context: Element, steps, absolute: bool):
    (
        root,
        nodes,
        lookup,
        parents,
        attrs,
        node_tags,
        workspace,
        symbols,
        selector_lock,
    ) = _flatten(context)
    attr_offsets, attr_names, attr_values = attrs
    tag_ids, name_ids, value_ids = symbols
    count = len(nodes)
    starts = np.asarray(
        [0 if absolute else lookup[id(context)]], dtype=np.int64
    )
    axes = np.asarray([step[0] for step in steps], dtype=np.int64) if steps else i64(0)
    missing = np.iinfo(np.uint64).max
    tags = u64(
        [0 if step[1] == "*" else tag_ids.get(step[1], missing) for step in steps]
    )
    pred_modes = np.asarray([step[2] for step in steps], dtype=np.int64) if steps else i64(0)
    pred_names = u64(
        [name_ids.get(step[3], missing) if step[3] else 0 for step in steps]
    )
    pred_values = u64(
        [value_ids.get(step[4], missing) if step[4] else 0 for step in steps]
    )
    positions = np.asarray([step[5] for step in steps], dtype=np.int64) if steps else i64(0)
    work_a, work_b, marks, result, task_counts = workspace
    with selector_lock:
        selected = lib().mlx_select(
            addr(parents), addr(node_tags), count,
            addr(attr_offsets), addr(attr_names), addr(attr_values),
            int(attr_offsets[-1]),
            addr(starts), len(starts), addr(axes), addr(tags), addr(pred_modes),
            addr(pred_names), addr(pred_values), addr(positions), len(steps),
            addr(work_a), addr(work_b), addr(marks), addr(result), addr(task_counts),
        )
        if selected < 0 or selected > count:
            raise RuntimeError(f"Mojo selector returned invalid result count {selected}")
        output = [nodes[int(index)] for index in result[:selected]]
    return output


def _xpath(context: Element, expression: str, namespaces: dict[str, str]):
    expression = expression.strip()
    for function in ("count", "boolean", "string"):
        prefix = function + "("
        if expression.startswith(prefix) and expression.endswith(")"):
            inner = _xpath(context, expression[len(prefix):-1], namespaces)
            if function == "count":
                return float(len(inner))
            if function == "boolean":
                return bool(inner)
            if not inner:
                return ""
            first = inner[0] if isinstance(inner, list) else inner
            return "".join(first.itertext()) if isinstance(first, Element) else str(first)
    if expression == ".":
        return [context]
    if expression == "text()":
        return [context.text] if context.text is not None else []
    if expression.startswith("@") and "/" not in expression:
        name = _resolve_xpath_name(expression[1:], namespaces)
        return [context.attrib[name]] if name in context.attrib else []
    absolute, steps, terminal = _compile_path(expression, namespaces)
    selected = _select(context, steps, absolute)
    if terminal == "text":
        return [node.text for node in selected if node.text is not None]
    if isinstance(terminal, tuple):
        name = terminal[1]
        return [node.attrib[name] for node in selected if name in node.attrib]
    return selected


class XPath:
    def __init__(
        self,
        path: str,
        namespaces: dict[str, str] | None = None,
        extensions=None,
        regexp: bool = True,
        smart_strings: bool = True,
    ):
        if extensions:
            raise NotImplementedError("XPath extensions are not covered")
        self.path = path
        self.namespaces = namespaces or {}
        _compile_path(path, self.namespaces)

    def __call__(self, context, **variables):
        if variables:
            raise NotImplementedError("XPath variables are not covered")
        if isinstance(context, ElementTree):
            context = context.getroot()
        return _xpath(context, self.path, self.namespaces)


def cleanup_namespaces(tree_or_element, top_nsmap=None, keep_ns_prefixes=None):
    return None


def strip_tags(tree_or_element, *tag_names):
    root = tree_or_element.getroot() if isinstance(tree_or_element, ElementTree) else tree_or_element
    wanted = set(tag_names)
    for node in list(root.iter()):
        for child in list(node):
            if child.tag in wanted:
                index = node._children.index(child)
                node._children.pop(index)
                for grandchild in reversed(child._children):
                    grandchild._parent = node
                    node._children.insert(index, grandchild)


def strip_elements(tree_or_element, *tag_names, with_tail=True):
    root = tree_or_element.getroot() if isinstance(tree_or_element, ElementTree) else tree_or_element
    wanted = set(tag_names)
    for node in list(root.iter()):
        for child in list(node):
            if child.tag in wanted:
                if not with_tail and child.tail:
                    _append_text(node, child.tail)
                node.remove(child)
