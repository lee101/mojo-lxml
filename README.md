# mojo-lxml

`mojo-lxml` is a standalone Mojo implementation of a focused, useful subset of
[`lxml`](https://lxml.de/): XML/HTML parsing, mutable element trees,
serialization, and common location-path XPath queries. It does not link to
libxml2. A Mojo byte scanner tokenizes and validates markup, and a second Mojo
kernel evaluates tree axes and attribute predicates; Python provides the
familiar `etree` object API.

This is an API-compatible alternative for the covered subset, not a replacement
for all of lxml. Change:

```python
from lxml import etree
```

to:

```python
from mojo_lxml import etree
```

## Covered subset

- `etree.fromstring`, `XML`, `HTML`, `parse`, `tostring`, and `tostringlist`
- `XMLParser` and `HTMLParser` entry points, including UTF-8 input and
  `remove_blank_text`
- elements, sub-elements, attributes, text/tail, parents and siblings, mutable
  children, iteration, `find`, `findall`, and `findtext`
- XML namespaces in Clark notation and prefixed XPath names
- CDATA, the five predefined XML entities, numeric character references,
  declarations, processing instructions, and safely skipped DOCTYPE blocks
- tolerant HTML void elements and common implicit-closing rules for `li`, `p`,
  table cells/rows, description lists, and options
- XPath child, descendant, and absolute paths; wildcards; numeric position;
  attribute existence/equality; `text()` and attribute results; and `count`,
  `boolean`, and `string`
- compiled `etree.XPath` expressions for the same grammar

The parity suite compares all of these behaviors directly with the real `lxml`
package.

Not covered are the complete XPath 1.0 grammar (unions, arithmetic, `last()`,
general functions, variables, and extensions), comments or processing
instructions as tree nodes, DTD-defined entities/defaults/validation, XInclude,
XSLT, XML Schema, Relax NG, canonical XML, streaming parsers, libxml2 recovery
semantics, non-UTF-8 declarations without an explicit parser encoding, and the
full HTML5 parsing algorithm. Parser options outside the covered behaviors are
accepted for source compatibility but do not enable those features.

## Install and run

The repository pins the tested Mojo nightly:

```bash
pixi install
pixi run build
pixi run test
```

The build creates `dist/libmojo-lxml.so`. `PYTHONPATH=python` is set by the pixi
environment.

## Usage

```python
from mojo_lxml import etree

root = etree.fromstring(
    b"""<catalog>
      <book id="b1"><title>A &amp; B</title></book>
      <book id="b2"><title>Mojo</title></book>
    </catalog>"""
)

titles = root.xpath(".//book[@id='b2']/title/text()")
assert titles == ["Mojo"]

root.find("book").set("available", "yes")
print(etree.tostring(root, encoding="unicode"))
```

HTML uses the matching convenience module:

```python
from mojo_lxml import html

document = html.fromstring("<ul><li>one<li>two</ul>")
assert [item.text for item in document.xpath(".//li")] == ["one", "two"]
```

## Benchmarks

Measured with `pixi run bench` on an Intel Xeon E5-2697 v4 at 2.30 GHz,
Linux x86-64. Times are the best of 3 or 5 runs. The relative column is
`lxml time / mojo-lxml time`, so values below 1 mean mojo-lxml is slower.

| Workload | mojo-lxml | lxml | Relative |
|---|---:|---:|---:|
| XML parse, 25k records | 448.12 ms | 46.39 ms | 0.10x |
| HTML parse, 25k rows | 466.17 ms | 54.04 ms | 0.12x |
| XPath attr filter, 75k nodes | 0.85 ms | 21.55 ms | 25.33x |
| XML serialize, 25k records | 71.89 ms | 13.52 ms | 0.19x |

Upstream lxml is a thin, mature Cython layer over highly optimized libxml2 data
structures. mojo-lxml still pays for Python element construction after its Mojo
scan, which dominates parsing. XPath's contiguous tree representation and work
buffers are cached until the tree is mutated, avoiding repeated flattening,
symbol construction, and allocation.

No GPU path is provided. Tokenization, tree construction, selection, and
serialization are branch-heavy and below roughly two operations per byte, so
device transfer and launch overhead would dominate. The CPU remains the only
execution path. Parsing is sequential because nesting state is carried between
tokens; the independent large-tree selector was not changed because it is
already more than 5x faster than upstream lxml.

## How it works

Python passes UTF-8 buffers to the shared library as integer addresses through
`ctypes`. NumPy views remain zero-copy across that boundary. The Mojo tokenizer
writes five caller-owned contiguous `int64` arrays containing token kinds, byte
spans, and per-start-tag attribute counts. It maintains a caller-owned stack
while validating XML nesting, so the native side performs no allocation and
retains no Python memory.

Python resolves namespaces, decodes entities, and builds mutable element
objects from those spans. For XPath, a live tree is flattened in preorder into
contiguous parent indices, tree-local integer symbols, and per-node contiguous
attribute ranges. The cached Mojo selector evaluates child, descendant, self,
tag, attribute, and position tests into reusable caller-owned work buffers.
The symbols identify tags and attribute strings without hash-collision risk.
Tokenizer text scans, tag blocks, and result copies use host-width SIMD with
scalar tails. Results are indices mapped back to the original element objects;
source strings and returned values remain ordinary Python strings.

Run the locked benchmark with:

```bash
pixi run bench
```
