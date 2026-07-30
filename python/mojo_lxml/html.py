"""The covered lxml.html-style convenience entry points."""

from __future__ import annotations

from . import etree


HtmlElement = etree.Element


def fromstring(html, base_url=None, parser=None, **kw):
    parser = parser or etree.HTMLParser()
    return etree._parse_markup(etree._source_bytes(html, parser.encoding), parser, True)


def document_fromstring(html, parser=None, ensure_head_body=False, **kw):
    root = fromstring(html, parser=parser)
    if root.tag != "html":
        wrapper = etree.Element("html")
        body = etree.SubElement(wrapper, "body")
        if getattr(root, "_synthetic_text_wrapper", False):
            body.text = root.text
        else:
            body.append(root)
        root = wrapper
    if ensure_head_body:
        if root.find("head") is None:
            root.insert(0, etree.Element("head"))
        if root.find("body") is None:
            root.append(etree.Element("body"))
    return root


def tostring(doc, pretty_print=False, include_meta_content_type=False, encoding=None, method="html", with_tail=True, doctype=None):
    return etree.tostring(
        doc,
        pretty_print=pretty_print,
        encoding=encoding,
        method=method,
        with_tail=with_tail,
        doctype=doctype,
    )


def parse(filename_or_url, parser=None, base_url=None, **kw):
    return etree.parse(filename_or_url, parser or etree.HTMLParser(), base_url)
