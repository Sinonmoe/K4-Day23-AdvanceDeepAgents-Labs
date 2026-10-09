"""check_citations.py - STUDENT IMPLEMENTS `check`.   Runs INSIDE the sandbox (standard library only).

research.py uploads this file to the sandbox and the lead agent runs it with the `execute` tool:
    python3 /tmp/work/research/check_citations.py [report.md] [sources.json]
It must exit 0 and print "OK: ..." when the report is consistent, else print each problem and exit 1.
"""
import json
import re
import sys

REPORT = "/tmp/work/report/report.md"
SOURCES = "/tmp/work/research/sources.json"


_GROUP = re.compile(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\](?!\()")   # [3]  [1, 2]  [1-3]  [2-3]; not [3](link)
_CODE = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
_REF_HEADING = re.compile(r"(?m)^##[ \t]+References[ \t]*$")


def _group_numbers(group):
    numbers = []
    for part in re.split(r"\s*,\s*", group):
        span = re.fullmatch(r"(\d+)\s*[–-]\s*(\d+)", part)
        if span:
            a, b = int(span.group(1)), int(span.group(2))
            numbers.extend(range(a, b + 1) if 0 <= b - a <= 200 else [a, b])
        else:
            numbers.append(int(part))
    return numbers


def check(report_text, sources):
    """Return a list of problem strings (empty list = OK)."""
    problems = []
    if not isinstance(sources, list) or len(sources) == 0:
        return ["no sources in sources.json"]

    seen_urls = set()
    source_by_n = {}
    for entry in sources:
        if not isinstance(entry, dict):
            problems.append(f"source entry {entry!r} is not an object")
            continue
        n = entry.get("n")
        if not isinstance(n, int):
            problems.append(f"source entry {entry!r}: n must be an int")
            continue
        if n in source_by_n:
            problems.append(f"duplicate source number [{n}] in sources.json")
        source_by_n[n] = entry

        url = entry.get("url")
        if not isinstance(url, str) or not (url.startswith("http://") or url.startswith("https://")):
            problems.append(f"source [{n}] url must start with http:// or https://: {url!r}")
            continue

        if url in seen_urls:
            problems.append(f"duplicate url in sources.json: {url}")
        seen_urls.add(url)

    matches = list(_REF_HEADING.finditer(report_text))
    if not matches:
        problems.append("missing '## References' heading")
        body = report_text
        ref_section = ""
    else:
        body = report_text[:matches[-1].start()]
        ref_section = report_text[matches[-1].end():]

    # Find citations in body only (ignoring code spans/blocks)
    segments = _CODE.split(body)
    cited = set()
    for i, segment in enumerate(segments):
        if i % 2 == 1:
            continue
        for m in _GROUP.finditer(segment):
            for num in _group_numbers(m.group(1)):
                cited.add(num)

    for c in sorted(cited):
        if c not in source_by_n:
            problems.append(f"[{c}] cited in body but missing from sources.json")

    for n in sorted(source_by_n):
        if n not in cited:
            problems.append(f"source [{n}] never cited in body")

    ref_lines_by_n = {}
    for line in ref_section.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        bundled = re.match(r"^\[(\d+(?:\s*[,–-]\s*\d+)+)\]", line)
        if bundled:
            problems.append(f"bundled sources under one reference line: {line}")
            continue
        m = re.match(r"^\[(\d+)\](?:\s+|$)", line)
        if not m:
            problems.append(f"reference line does not start with '[n]': {line}")
            continue
        n = int(m.group(1))
        if n in ref_lines_by_n:
            problems.append(f"duplicate reference line for [{n}]")
        else:
            ref_lines_by_n[n] = line

    for n in sorted(source_by_n):
        if n not in ref_lines_by_n:
            problems.append(f"missing reference line for source [{n}]")

    for n in sorted(ref_lines_by_n):
        if n not in source_by_n:
            problems.append(f"reference line [{n}] is not in sources.json")

    for n, line in sorted(ref_lines_by_n.items()):
        if n in source_by_n:
            raw_urls = re.findall(r"https?://\S+", line)
            cleaned_urls = [u.rstrip(".,;)>") for u in raw_urls]
            if len(cleaned_urls) != 1:
                problems.append(f"reference line [{n}] must contain exactly one URL, found {len(cleaned_urls)}: {line}")
            else:
                expected_url = source_by_n[n].get("url")
                if cleaned_urls[0] != expected_url and raw_urls[0] != expected_url:
                    problems.append(
                        f"reference line [{n}] URL '{cleaned_urls[0]}' does not match sources.json URL '{expected_url}'"
                    )

    return problems


def main(argv):
    report_path = argv[1] if len(argv) > 1 else REPORT
    sources_path = argv[2] if len(argv) > 2 else SOURCES
    try:
        with open(report_path, encoding="utf-8") as f:
            report = f.read()
        with open(sources_path, encoding="utf-8") as f:
            sources = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"cannot read inputs: {exc}")
        return 1
    problems = check(report, sources)
    if problems:
        print("\n".join(problems))
        return 1
    print(f"OK: {len(sources)} sources, all citations resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
