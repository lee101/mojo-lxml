"""End-to-end benchmarks against upstream lxml on identical documents."""

from __future__ import annotations

import os
import platform
import sys
import time

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python")
)

from lxml import etree as upstream  # noqa: E402
from mojo_lxml import etree  # noqa: E402


def best_time(function, repetitions=5):
    best = float("inf")
    result = None
    for _ in range(repetitions):
        start = time.perf_counter()
        result = function()
        best = min(best, time.perf_counter() - start)
    return best, result


def cpu_name():
    try:
        with open("/proc/cpuinfo") as stream:
            for line in stream:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown CPU"


def row(name, ours, reference):
    relative = reference / ours
    return (
        f"| {name} | {ours * 1000:.2f} ms | {reference * 1000:.2f} ms "
        f"| {relative:.2f}x |"
    )


def main():
    count = int(os.environ.get("MOJO_LXML_BENCH_COUNT", "25000"))
    records = "".join(
        f'<item id="{index}" kind="{index % 10}"><name>record {index}</name>'
        f"<value>{index * 3}</value></item>"
        for index in range(count)
    )
    xml_data = f"<dataset>{records}</dataset>".encode()
    html_data = (
        "<html><body><table>"
        + "".join(
            f'<tr data-kind="{index % 10}"><td>{index}</td><td>row {index}</td></tr>'
            for index in range(count)
        )
        + "</table></body></html>"
    ).encode()

    ours_xml = etree.fromstring(xml_data)
    ref_xml = upstream.fromstring(xml_data)
    assert len(ours_xml.xpath(".//item[@kind='7']")) == len(
        ref_xml.xpath(".//item[@kind='7']")
    )

    results = []
    ours, ours_result = best_time(lambda: etree.fromstring(xml_data), repetitions=3)
    reference, ref_result = best_time(lambda: upstream.fromstring(xml_data), repetitions=3)
    assert len(ours_result) == len(ref_result)
    label_count = f"{count // 1000}k" if count % 1000 == 0 else str(count)
    results.append((f"XML parse, {label_count} records", ours, reference))

    ours, ours_result = best_time(lambda: etree.HTML(html_data), repetitions=3)
    reference, ref_result = best_time(lambda: upstream.HTML(html_data), repetitions=3)
    assert len(ours_result.xpath(".//tr")) == len(ref_result.xpath(".//tr"))
    results.append((f"HTML parse, {label_count} rows", ours, reference))

    ours, ours_result = best_time(
        lambda: ours_xml.xpath(".//item[@kind='7']"), repetitions=5
    )
    reference, ref_result = best_time(
        lambda: ref_xml.xpath(".//item[@kind='7']"), repetitions=5
    )
    assert [node.get("id") for node in ours_result] == [
        node.get("id") for node in ref_result
    ]
    node_count = count * 3
    node_label = (
        f"{node_count // 1000}k" if node_count % 1000 == 0 else str(node_count)
    )
    results.append((f"XPath attr filter, {node_label} nodes", ours, reference))

    ours, ours_result = best_time(
        lambda: etree.tostring(ours_xml, encoding="UTF-8"), repetitions=3
    )
    reference, ref_result = best_time(
        lambda: upstream.tostring(ref_xml, encoding="UTF-8"), repetitions=3
    )
    assert ours_result == ref_result
    results.append((f"XML serialize, {label_count} records", ours, reference))

    print(f"Machine: {cpu_name()} ({platform.system()} {platform.machine()})")
    print()
    print("| Workload | mojo-lxml | lxml | lxml time / mojo-lxml time |")
    print("|---|---:|---:|---:|")
    for name, ours, reference in results:
        print(row(name, ours, reference))


if __name__ == "__main__":
    main()
