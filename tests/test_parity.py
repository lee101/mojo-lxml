from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor

import pytest
import numpy as np
from lxml import etree as upstream
from lxml import html as upstream_html

from mojo_lxml import etree, html
from mojo_lxml._lib import addr


XML_SAMPLE = b"""<?xml version="1.0"?>
<catalog xmlns:p="urn:price" source="demo">
  <book id="b1" lang="en"><title>A &amp; B</title><p:price currency="USD">10</p:price></book>
  <book id="b2"><title><![CDATA[C < D]]></title><p:price currency="EUR">20</p:price></book>
  <note enabled="">remember <b>this</b> later</note>
  <empty/>
</catalog>"""


def signature(node):
    return (
        node.tag,
        dict(node.attrib),
        node.text,
        node.tail,
        [signature(child) for child in node],
    )


def xpath_value(value):
    if isinstance(value, list):
        return [
            ("element", item.tag, dict(item.attrib), item.text)
            if hasattr(item, "tag")
            else ("scalar", str(item))
            for item in value
        ]
    return value


def test_xml_tree_parity():
    assert signature(etree.fromstring(XML_SAMPLE)) == signature(
        upstream.fromstring(XML_SAMPLE)
    )


def test_xml_alias():
    assert signature(etree.XML(XML_SAMPLE)) == signature(upstream.XML(XML_SAMPLE))


def test_cdata_and_entity_parity():
    source = b"<r>one &lt; two<![CDATA[ & three]]>&#33;&#x3f;</r>"
    assert etree.fromstring(source).text == upstream.fromstring(source).text


def test_all_predefined_xml_entities():
    source = b"<r>&amp;&lt;&gt;&apos;&quot;</r>"
    assert etree.fromstring(source).text == upstream.fromstring(source).text


def test_utf8_parity():
    source = "<r city='Zürich'>雪と café</r>"
    assert signature(etree.fromstring(source)) == signature(upstream.fromstring(source))


def test_namespace_resolution_parity():
    source = b'<r xmlns="urn:d" xmlns:q="urn:q" q:a="x"><q:c a="y"/></r>'
    assert signature(etree.fromstring(source)) == signature(upstream.fromstring(source))


def test_doctype_is_skipped_safely():
    source = b'<!DOCTYPE r [<!ELEMENT r ANY>]><r><x/></r>'
    assert signature(etree.fromstring(source)) == signature(upstream.fromstring(source))


def test_processing_instruction_is_skipped():
    source = b"<?xml version='1.0'?><r><?work now?><x/></r>"
    ours = etree.fromstring(source)
    reference = upstream.fromstring(
        source, upstream.XMLParser(remove_pis=True)
    )
    assert signature(ours) == signature(reference)


@pytest.mark.parametrize(
    "source",
    [
        b"<r><x></r>",
        b"<r>",
        b"<r a=one/>",
        b"<r a='one' a='two'/>",
        b"<r>&missing;</r>",
        b"<r>A & B</r>",
        b"<1bad/>",
        b"<r bad:name:again='x'/>",
        b"<r>\x01</r>",
        b"<r/><second/>",
        b"text<r/>",
    ],
)
def test_malformed_xml_rejected(source):
    with pytest.raises((upstream.XMLSyntaxError, SyntaxError)):
        upstream.fromstring(source)
    with pytest.raises(etree.XMLSyntaxError):
        etree.fromstring(source)


@pytest.mark.parametrize(
    "expression",
    [
        "book",
        "./book",
        ".//title",
        "book/title",
        "book[@id]",
        "book[@id='b2']",
        'book[@id="b1"]',
        "book[1]",
        "book/title/text()",
        "book/@id",
        ".//*",
        ".//p:price",
        ".//p:price[@currency='EUR']",
        "/catalog/book",
        "/catalog/book/title",
    ],
)
def test_xpath_nodes_and_scalars_parity(expression):
    ours = etree.fromstring(XML_SAMPLE)
    reference = upstream.fromstring(XML_SAMPLE)
    namespaces = {"p": "urn:price"}
    assert xpath_value(ours.xpath(expression, namespaces=namespaces)) == xpath_value(
        reference.xpath(expression, namespaces=namespaces)
    )


@pytest.mark.parametrize(
    "expression",
    [
        "count(.//book)",
        "count(.//p:price)",
        "boolean(.//book[@id='b2'])",
        "boolean(.//missing)",
        "string(book/title)",
    ],
)
def test_xpath_functions_parity(expression):
    ours = etree.fromstring(XML_SAMPLE)
    reference = upstream.fromstring(XML_SAMPLE)
    namespaces = {"p": "urn:price"}
    assert ours.xpath(expression, namespaces=namespaces) == reference.xpath(
        expression, namespaces=namespaces
    )


def test_compiled_xpath_parity():
    ours = etree.fromstring(XML_SAMPLE)
    reference = upstream.fromstring(XML_SAMPLE)
    ours_path = etree.XPath(".//p:price[@currency='USD']", namespaces={"p": "urn:price"})
    ref_path = upstream.XPath(".//p:price[@currency='USD']", namespaces={"p": "urn:price"})
    assert xpath_value(ours_path(ours)) == xpath_value(ref_path(reference))


def test_relative_xpath_context():
    ours = etree.fromstring(XML_SAMPLE)[1]
    reference = upstream.fromstring(XML_SAMPLE)[1]
    assert xpath_value(ours.xpath("./title")) == xpath_value(reference.xpath("./title"))


def test_find_family_parity():
    ours = etree.fromstring(XML_SAMPLE)
    reference = upstream.fromstring(XML_SAMPLE)
    assert ours.find("book/title").text == reference.find("book/title").text
    assert [x.get("id") for x in ours.findall("book")] == [
        x.get("id") for x in reference.findall("book")
    ]
    assert ours.findtext("book/title", "missing") == reference.findtext(
        "book/title", "missing"
    )
    assert ours.findtext("missing", "default") == "default"


def test_iteration_parity():
    ours = etree.fromstring(XML_SAMPLE)
    reference = upstream.fromstring(XML_SAMPLE)
    assert [node.tag for node in ours.iter()] == [node.tag for node in reference.iter()]
    assert list(ours.itertext()) == list(reference.itertext())


def test_navigation_parity():
    root = etree.fromstring(b"<r><a/><b/><c/></r>")
    assert root[1].getparent() is root
    assert root[1].getprevious() is root[0]
    assert root[1].getnext() is root[2]
    assert root[0].getprevious() is None
    assert root[2].getnext() is None
    assert root[1].getroottree().getroot() is root


def test_element_mutation_and_serialization_parity():
    ours = etree.Element("root", mode="demo")
    reference = upstream.Element("root", mode="demo")
    for root, module in ((ours, etree), (reference, upstream)):
        root.text = "before"
        first = module.SubElement(root, "item", id="1")
        first.text = "A & B"
        first.tail = "after"
        root.insert(0, module.Element("prefix"))
        root[-1].set("active", "yes")
    assert etree.tostring(ours) == upstream.tostring(reference)
    ours.remove(ours[0])
    reference.remove(reference[0])
    assert etree.tostring(ours) == upstream.tostring(reference)


@pytest.mark.parametrize("encoding", [None, "UTF-8", "ASCII", "unicode"])
def test_tostring_encoding_parity(encoding):
    source = "<r a='1'>café &amp; tea<x/></r>"
    ours = etree.fromstring(source)
    reference = upstream.fromstring(source)
    assert etree.tostring(ours, encoding=encoding) == upstream.tostring(
        reference, encoding=encoding
    )


def test_tostringlist_parity():
    ours = etree.fromstring(b"<r><x/></r>")
    reference = upstream.fromstring(b"<r><x/></r>")
    assert etree.tostringlist(ours) == upstream.tostringlist(reference)


def test_pretty_print_parity():
    source = b"<r><a><b/></a><c/></r>"
    assert etree.tostring(
        etree.fromstring(source), pretty_print=True
    ) == upstream.tostring(upstream.fromstring(source), pretty_print=True).rstrip(b"\n")


def test_parse_filelike_parity():
    ours = etree.parse(io.BytesIO(XML_SAMPLE))
    reference = upstream.parse(io.BytesIO(XML_SAMPLE))
    assert signature(ours.getroot()) == signature(reference.getroot())


def test_parse_path_and_write(tmp_path):
    source = tmp_path / "input.xml"
    source.write_bytes(XML_SAMPLE)
    tree = etree.parse(source)
    target = tmp_path / "output.xml"
    tree.write(target, encoding="UTF-8")
    assert signature(etree.parse(target).getroot()) == signature(tree.getroot())


@pytest.mark.parametrize(
    "source",
    [
        "<p>Hello<br>world</p>",
        "<html><body><h1>Title</h1></body></html>",
        "<title>T</title><p>x</p>",
        "<ul><li>one<li>two</ul>",
        "<p>a<div>b</div>",
        "<form><input disabled><option selected>x</option></form>",
    ],
)
def test_etree_html_parity(source):
    ours = etree.HTML(source)
    reference = upstream.HTML(source)
    assert etree.tostring(ours, method="html") == upstream.tostring(
        reference, method="html"
    )


def test_explicit_html_parser_entry_point():
    source = "<p>Hello<br>world</p>"
    ours = etree.fromstring(source, etree.HTMLParser())
    reference = upstream.fromstring(source, upstream.HTMLParser())
    assert etree.tostring(ours, method="html") == upstream.tostring(
        reference, method="html"
    )


@pytest.mark.parametrize(
    "source",
    [
        "<dl><dt>a<dd>b<dt>c</dl>",
        "<table><tr><td>a<td>b<tr><th>c</table>",
        "<select><option>a<option>b</select>",
    ],
)
def test_documented_html_implicit_closing_rules(source):
    ours = etree.HTML(source)
    reference = upstream.HTML(source)
    assert etree.tostring(ours, method="html") == upstream.tostring(
        reference, method="html"
    )


@pytest.mark.parametrize(
    "source",
    [
        "<p>Hello</p>",
        "<div><span>A</span><br>B</div>",
        "<ul><li>one<li>two</ul>",
        "<html><body><p>x</p></body></html>",
        "plain text",
    ],
)
def test_html_fromstring_parity(source):
    ours = html.fromstring(source)
    reference = upstream_html.fromstring(source)
    assert etree.tostring(ours, method="html") == upstream.tostring(
        reference, method="html"
    )


def test_html_document_fromstring_parity():
    source = "<title>T</title><p>Hello</p>"
    ours = html.document_fromstring(source)
    reference = upstream_html.document_fromstring(source)
    assert etree.tostring(ours, method="html") == upstream.tostring(
        reference, method="html"
    )


def test_remove_blank_text_parity():
    source = b"<r>  <a> x </a>\n<b/> </r>"
    ours = etree.fromstring(source, etree.XMLParser(remove_blank_text=True))
    reference = upstream.fromstring(
        source, upstream.XMLParser(remove_blank_text=True)
    )
    assert signature(ours) == signature(reference)


def test_attribute_mapping():
    element = etree.Element("x", {"a": "1"})
    element.attrib["b"] = 2
    assert dict(element.attrib) == {"a": "1", "b": "2"}
    del element.attrib["a"]
    assert element.keys() == element.attrib.keys()
    element.clear()
    assert not element.attrib and len(element) == 0


def test_unsupported_xpath_is_explicit():
    root = etree.fromstring(b"<r><x/></r>")
    with pytest.raises(NotImplementedError):
        root.xpath(".//x[last()]")
    with pytest.raises(NotImplementedError):
        root.xpath(".//x[text()='value']/child")


def test_native_address_validation():
    with pytest.raises(TypeError):
        addr(np.ones(4, dtype=np.int32))
    with pytest.raises(ValueError):
        addr(np.ones(8, dtype=np.int64)[::2])


def test_xpath_simd_tail_and_reused_workspace():
    root = etree.Element("root")
    selected = [etree.SubElement(root, "hit", index=str(index)) for index in range(17)]
    branch = etree.SubElement(root, "branch")
    nested = etree.SubElement(branch, "hit", index="nested")
    assert root.xpath(".//hit") == [*selected, nested]
    assert branch.xpath(".//hit") == [nested]
    assert root.xpath("./hit") == selected


def test_xpath_reused_workspace_is_thread_safe():
    root = etree.fromstring(
        "<r>" + "".join(f"<x id='{index}'/>" for index in range(100)) + "</r>"
    )

    def query(index):
        return root.xpath(f".//x[@id='{index % 100}']")[0].get("id")

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert list(executor.map(query, range(400))) == [
            str(index % 100) for index in range(400)
        ]


def test_xpath_parallel_threshold():
    root = etree.Element("root")
    root._children = [etree.Element("miss") for _ in range(262_142)]
    for child in root._children:
        child._parent = root
    target = etree.Element("target")
    target._parent = root
    root._children.append(target)
    assert root.xpath(".//target") == [target]


def test_html_token_capacity_retry():
    attributes = " ".join(f"a{index}" for index in range(80))
    source = f"<div {attributes}>value</div>"
    ours = etree.HTML(source).xpath(".//div")[0]
    reference = upstream.HTML(source).xpath(".//div")[0]
    assert list(ours.attrib) == list(reference.attrib)
    assert ours.text == reference.text
