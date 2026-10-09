"""research.py - STUDENT IMPLEMENTS.  The main script.   Guide: GUIDE.md, part 3.

Usage:  python research.py "survey about world model"
Result: reports/<slug>.md   reports/<slug>.sources.json   reports/<slug>.meta.json
"""
import json  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import sys
import time  # noqa: F401
from collections import Counter  # noqa: F401
from pathlib import Path

from agents import FINALIZER_PATH, REPORT_PATH, SOURCES_PATH, VALIDATOR_PATH, WORKDIR, build_lead_agent  # noqa: F401
from model import make_model  # noqa: F401
from sandbox import download, open_sandbox, upload  # noqa: F401

ROOT = Path(__file__).parent
REPORTS = ROOT / "reports"
VALIDATOR_SOURCE = ROOT / "check_citations.py"
FINALIZER_SOURCE = ROOT / "finalize_citations.py"   # provided: uploaded next to your validator


def slugify(topic):
    """Turn a topic into a safe file name: lower case, runs of non-word characters become one "-", max 60 chars,
    never empty (fall back to "topic"). The topic is user input: "../../x" must not escape reports/."""
    cleaned = re.sub(r"[^\w]+", "-", topic.strip().lower()).strip("-")
    cleaned = cleaned[:60].strip("-")
    return cleaned or "topic"


def build_prompt(topic):
    """The user message sent to the lead agent."""
    return f"""Please conduct a comprehensive, deep scientific literature survey on the following topic:
"{topic}"

Follow your required workflow step-by-step:
1. Use `write_todos` to plan and break this topic into at least 3 distinct sub-questions (e.g. 3-4 sub-questions covering theoretical foundations, key architectures/methods, applications/benchmarks, and open challenges).
2. Delegate each sub-question in parallel to the `researcher` subagent via `task`. Provide each researcher with full instructions: the overarching topic, specific sub-question, the notes file path (e.g. {WORKDIR}/research/notes/01-slug.md), required note format, and mandate to gather from at least 2 source families including Hugging Face papers via `hf_search_papers`.
3. MANDATORY MULTI-SOURCE REQUIREMENT (RUBRIC 2.2):
   The final sources.json and report MUST contain citations from AT LEAST 3 distinct source families among:
   ['arxiv', 'hf-daily', 'hf-search', 'web'].
   You MUST explicitly instruct researchers to find Hugging Face papers via `hf_search_papers` and `hf_daily_papers`, arXiv via `arxiv_search`, and web via `web_search`.
   If any of the 3 families is missing from your notes, delegate another task to fetch from the missing source family before writing the report.
4. Aggregate all unique sources from the notes into {SOURCES_PATH} as a JSON list numbered 1..N with fields: n, id, url, title, date, source. Ensure valid URLs and correct source labels matching the URLs.
5. Write the comprehensive survey report body into {REPORT_PATH} following REPORT_TEMPLATE.md:
   - Title (# ...)
   - TL;DR (3-5 bullets with inline citations [n])
   - Background (citing foundational work [n])
   - 3 to 6 thematic sections comparing methods and citing [n]
   - Trends and open problems (citing [n])
   - Important: DO NOT write the '## References' section! The finalizer script will generate it.
   - You MUST explicitly cite sources from at least 3 distinct families in the text (including Hugging Face papers with source 'hf-search' or 'hf-daily', alongside arXiv and web). Remember that uncited sources are stripped by the finalizer!
6. Run the finalizer script via `execute`:
   `python3 {FINALIZER_PATH}`
7. Run the validator script via `execute`:
   `python3 {VALIDATOR_PATH}`
   Fix any reported issues until it prints 'OK: ...'.
8. Spot-check 2-3 key claims using the `citation-checker` subagent via `task`.
"""


def summarize(messages, elapsed, model_name):
    """Return {"model", "elapsed_s", "subagent_calls", "tool_calls": {name: count}, "tokens": {"input", "output"}}.

    PSEUDO-CODE: walk the lead's messages; for every message with tool_calls count call["name"] (subagent_calls = the
    count of "task"); add the input/output token counts from each message's usage_metadata when present.
    (Lead messages only: subagent tokens are not included, so this undercounts the real cost.)
    elapsed_s rounded to 0.1.
    """
    tool_calls = Counter()
    input_tokens = 0
    output_tokens = 0

    for msg in messages:
        calls = getattr(msg, "tool_calls", None)
        if calls is None and isinstance(msg, dict):
            calls = msg.get("tool_calls")
        if calls and isinstance(calls, list):
            for c in calls:
                name = c.get("name") if isinstance(c, dict) else getattr(c, "name", None)
                if name:
                    tool_calls[name] += 1

        meta = getattr(msg, "usage_metadata", None)
        if meta is None and isinstance(msg, dict):
            meta = msg.get("usage_metadata")
        if meta and isinstance(meta, dict):
            input_tokens += meta.get("input_tokens", 0)
            output_tokens += meta.get("output_tokens", 0)
        else:
            resp_meta = getattr(msg, "response_metadata", None)
            if resp_meta is None and isinstance(msg, dict):
                resp_meta = msg.get("response_metadata")
            if resp_meta and isinstance(resp_meta, dict):
                usage = resp_meta.get("token_usage") or resp_meta.get("usage", {})
                if isinstance(usage, dict):
                    input_tokens += usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
                    output_tokens += usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)

    return {
        "model": str(model_name),
        "elapsed_s": round(elapsed, 1),
        "subagent_calls": tool_calls.get("task", 0),
        "tool_calls": dict(tool_calls),
        "tokens": {
            "input": input_tokens,
            "output": output_tokens,
        },
    }


def save_outputs(backend, topic, messages, elapsed, model_name, reports_dir=REPORTS):
    """Download the report from the sandbox and write the three files into reports_dir. Return the report path.

    PSEUDO-CODE:
      files = download(backend, [REPORT_PATH, SOURCES_PATH])
      if the report is missing/empty or sources.json is missing/invalid JSON: raise RuntimeError and WRITE NOTHING
          (a failed run must never leave an empty or half-written report behind)
      write <slug>.sources.json, <slug>.meta.json (topic + summarize(...) + n_sources + source_families: the sorted
      distinct "source" values of sources.json) and <slug>.md
    """
    files = download(backend, [REPORT_PATH, SOURCES_PATH])
    report_bytes = files.get(REPORT_PATH)
    sources_bytes = files.get(SOURCES_PATH)

    if not report_bytes or not report_bytes.strip():
        raise RuntimeError(f"Report at {REPORT_PATH} is missing or empty")
    if not sources_bytes or not sources_bytes.strip():
        raise RuntimeError(f"Sources at {SOURCES_PATH} is missing or empty")

    try:
        sources_data = json.loads(sources_bytes.decode("utf-8"))
        if not isinstance(sources_data, list):
            raise ValueError("sources.json must be a JSON list")
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON in {SOURCES_PATH}: {exc}") from exc

    # Sanity checks and normalization: reject placeholder or fabricated sources
    valid_families = {"arxiv", "hf-daily", "hf-search", "web"}
    for s in sources_data:
        url = str(s.get("url") or "").lower()
        title = str(s.get("title") or "").lower()
        if any(bad in url for bad in ["link1", "link2", "webarticle", "example.com"]) or any(
            bad in title for bad in ["hugging face paper 1", "arxiv paper 1", "web article 1"]
        ):
            raise RuntimeError(f"Detected placeholder/hallucinated source in {SOURCES_PATH}: {s}")

        # Normalize minor labeling discrepancies
        src = str(s.get("source") or "").lower()
        if "arxiv.org" in url:
            s["source"] = "arxiv"
        elif "huggingface.co" in url and src not in {"hf-search", "hf-daily"}:
            s["source"] = "hf-search"
        elif src not in valid_families:
            s["source"] = "web"

    summary = summarize(messages, elapsed, model_name)
    source_families = sorted(
        list({s.get("source") for s in sources_data if isinstance(s, dict) and s.get("source")})
    )

    if len(set(source_families) & valid_families) < 3:
        raise RuntimeError(f"Report has only {len(set(source_families) & valid_families)} source families: {source_families}")

    meta_data = {
        "topic": topic,
        **summary,
        "n_sources": len(sources_data),
        "source_families": source_families,
    }

    reports_path = Path(reports_dir)
    reports_path.mkdir(parents=True, exist_ok=True)
    slug = slugify(topic)

    report_file = reports_path / f"{slug}.md"
    sources_file = reports_path / f"{slug}.sources.json"
    meta_file = reports_path / f"{slug}.meta.json"

    sources_file.write_text(json.dumps(sources_data, ensure_ascii=False, indent=2), encoding="utf-8")
    meta_file.write_text(json.dumps(meta_data, ensure_ascii=False, indent=2), encoding="utf-8")
    report_file.write_text(report_bytes.decode("utf-8"), encoding="utf-8")

    return report_file


def main(topic):
    """Return the process exit code (0 ok, 1 failed run, 2 no topic)."""
    if not topic or not topic.strip():
        print("Usage: python research.py \"<topic>\"", file=sys.stderr)
        return 2

    topic = topic.strip()
    model = make_model()
    model_name = getattr(model, "model_name", None) or getattr(model, "model", None) or os.getenv("LAB_MODEL", "model")
    start = time.monotonic()

    with open_sandbox() as backend:
        backend.execute(f"mkdir -p {WORKDIR}/research/notes {WORKDIR}/report")
        upload(
            backend,
            {
                VALIDATOR_PATH: VALIDATOR_SOURCE.read_bytes(),
                FINALIZER_PATH: FINALIZER_SOURCE.read_bytes(),
            },
        )
        agent = build_lead_agent(backend, model)
        user_prompt = build_prompt(topic)
        result = agent.invoke(
            {"messages": [{"role": "user", "content": user_prompt}]},
            config={"recursion_limit": 1000},
        )
        elapsed = time.monotonic() - start
        messages = result.get("messages", []) if isinstance(result, dict) else []

        try:
            report_path = save_outputs(backend, topic, messages, elapsed, model_name)
            print(f"SUCCESS: Report saved to {report_path}")
            return 0
        except RuntimeError as err:
            print(f"FAILED: {err}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main(" ".join(sys.argv[1:])))
