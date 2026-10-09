"""tools.py - STUDENT IMPLEMENTS.  Source tools for the research agents.   Guide: GUIDE.md, part 1.

Rules for every tool:
  * runs on the HOST (not in the sandbox): API keys must never enter the sandbox;
  * returns a STRING (JSON text of compact records) and NEVER raises:
        "NO RESULTS"  when the source answers with nothing,
        "ERROR: ..."  when the source keeps failing after the retries (the agent then tries another source);
  * the docstring is the tool description the LLM reads: keep it precise (what it does, what it returns, when to use it).
Try your tools without any agent:   python tools.py
"""
import json  # noqa: F401
import os  # noqa: F401
import random
import re
import time  # noqa: F401
import xml.etree.ElementTree  # noqa: F401  (arXiv answers with Atom XML)

import httpx  # noqa: F401
from dotenv import load_dotenv
from langchain_core.tools import tool

load_dotenv()

# ---- constants (given) ----
ARXIV_URL = "https://export.arxiv.org/api/query"  # https only: http answers 301
HF_DAILY_URL = "https://huggingface.co/api/daily_papers"
HF_SEARCH_URL = "https://huggingface.co/api/papers/search"
EXA_URL = "https://mcp.exa.ai/mcp"

_LAST_ARXIV_CALL = 0.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class RetryableError(Exception):
    """Given. Raise it inside a call to ask with_retry to wait and try again (retry_after in seconds, optional)."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def redact_secrets(text: str) -> str:
    """Redact sensitive keys from error messages."""
    key = (os.getenv("EXA_API_KEY") or "").strip()
    if key and key in text:
        text = text.replace(key, "[REDACTED]")
    return text


# ---- TODO 1: retry helper ----
def with_retry(fn, *, attempts=5, base=1.0, cap=30.0):
    """Call fn(); when it raises RetryableError, wait and call it again."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            is_retryable = False
            retry_after = None

            if isinstance(exc, RetryableError):
                is_retryable = True
                if exc.retry_after is not None:
                    try:
                        retry_after = float(exc.retry_after)
                    except (ValueError, TypeError):
                        pass
            elif isinstance(exc, httpx.HTTPStatusError):
                if exc.response.status_code in RETRYABLE_STATUS_CODES:
                    is_retryable = True
                    header_val = exc.response.headers.get("Retry-After")
                    if header_val:
                        try:
                            retry_after = float(header_val)
                        except (ValueError, TypeError):
                            pass
            elif isinstance(exc, httpx.TransportError):
                is_retryable = True

            if not is_retryable or attempt == attempts - 1:
                raise

            if retry_after is not None:
                delay = min(cap, max(0.0, retry_after))
            else:
                backoff = base * (2 ** attempt)
                jitter = random.uniform(0.0, 1.0)
                delay = min(cap, backoff + jitter)

            time.sleep(delay)


def _rate_limit_arxiv():
    global _LAST_ARXIV_CALL
    elapsed = time.monotonic() - _LAST_ARXIV_CALL
    if elapsed < 3.0:
        time.sleep(3.0 - elapsed)
    _LAST_ARXIV_CALL = time.monotonic()


# ---- TODO 2: arXiv ----
@tool
def arxiv_search(query: str, max_results: int = 10) -> str:
    """Search arXiv papers by keywords, newest first. Returns a JSON list of {id, url, published, title, summary}."""
    try:
        terms = re.findall(r"[\w-]+", query)
        if not terms:
            return "NO RESULTS"
        search_query = " AND ".join(f"all:{t}" for t in terms)
        clamped_max = max(1, min(max_results, 30))
        params = {
            "search_query": search_query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": clamped_max,
        }

        def _fetch():
            _rate_limit_arxiv()
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(ARXIV_URL, params=params)
                resp.raise_for_status()
                return resp.text

        xml_text = with_retry(_fetch, attempts=5, base=2.0, cap=60.0)
        root = xml.etree.ElementTree.fromstring(xml_text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)
        if not entries:
            return "NO RESULTS"

        records = []
        for entry in entries:
            raw_id = entry.findtext("atom:id", default="", namespaces=ns)
            pid = raw_id.split("/abs/")[-1].strip()
            pid = re.sub(r"v\d+$", "", pid)
            if not pid:
                continue
            url = f"https://arxiv.org/abs/{pid}"
            pub = entry.findtext("atom:published", default="", namespaces=ns)[:10]
            raw_title = entry.findtext("atom:title", default="", namespaces=ns)
            title = " ".join(raw_title.split())
            raw_summary = entry.findtext("atom:summary", default="", namespaces=ns)
            summary = " ".join(raw_summary.split())[:600]
            records.append({
                "id": pid,
                "url": url,
                "published": pub,
                "title": title,
                "summary": summary,
            })

        if not records:
            return "NO RESULTS"
        return json.dumps(records, ensure_ascii=False)
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {redact_secrets(str(exc))}"


# ---- TODO 3: Hugging Face ----
@tool
def hf_daily_papers(limit: int = 30, date: str = "", keyword: str = "") -> str:
    """Hugging Face Daily Papers = what is trending in AI research. Returns a JSON list of
    {id, url, published, title, summary, upvotes, github, stars} sorted by upvotes. `date` is YYYY-MM-DD (empty = latest).
    `keyword` filters title/summary; there is no topic search on this endpoint (use hf_search_papers for a topic)."""
    try:
        clamped_limit = max(1, min(limit, 100))
        params = {"limit": clamped_limit}
        if date.strip():
            params["date"] = date.strip()

        def _fetch():
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(HF_DAILY_URL, params=params)
                resp.raise_for_status()
                return resp.json()

        data = with_retry(_fetch, attempts=5, base=1.0, cap=30.0)
        if not isinstance(data, list) or not data:
            return "NO RESULTS"

        kw = keyword.strip().lower()
        records = []
        for item in data:
            if not isinstance(item, dict):
                continue
            paper = item.get("paper") if isinstance(item.get("paper"), dict) else item
            pid = paper.get("id")
            if not pid:
                continue
            title = " ".join(str(paper.get("title") or item.get("title") or "").split())
            summary = " ".join(str(paper.get("summary") or item.get("summary") or "").split())[:600]
            published = str(paper.get("publishedAt") or item.get("publishedAt") or "")[:10]
            upvotes = int(paper.get("upvotes") or item.get("upvotes") or 0)
            github = str(paper.get("githubRepo") or item.get("githubRepo") or "")
            stars = int(paper.get("githubStars") or item.get("githubStars") or 0)

            if kw and (kw not in title.lower() and kw not in summary.lower()):
                continue

            records.append({
                "id": str(pid),
                "url": f"https://huggingface.co/papers/{pid}",
                "published": published,
                "title": title,
                "summary": summary,
                "upvotes": upvotes,
                "github": github,
                "stars": stars,
            })

        records.sort(key=lambda r: r["upvotes"], reverse=True)
        if not records:
            return "NO RESULTS"
        return json.dumps(records, ensure_ascii=False)
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {redact_secrets(str(exc))}"


@tool
def hf_search_papers(query: str, limit: int = 10) -> str:
    """Search Hugging Face papers by topic. Returns a JSON list of
    {id, url, published, title, summary, upvotes, github, stars}."""
    try:
        q = query.strip()
        if not q:
            return "NO RESULTS"
        clamped_limit = max(1, min(limit, 50))
        params = {"q": q, "limit": clamped_limit}

        def _fetch():
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(HF_SEARCH_URL, params=params)
                resp.raise_for_status()
                return resp.json()

        data = with_retry(_fetch, attempts=5, base=1.0, cap=30.0)
        if not isinstance(data, list) or not data:
            return "NO RESULTS"

        records = []
        for item in data:
            if not isinstance(item, dict):
                continue
            paper = item.get("paper") if isinstance(item.get("paper"), dict) else item
            pid = paper.get("id")
            if not pid:
                continue
            title = " ".join(str(paper.get("title") or item.get("title") or "").split())
            raw_summary = (
                paper.get("ai_summary")
                or paper.get("summary")
                or item.get("ai_summary")
                or item.get("summary")
                or ""
            )
            summary = " ".join(str(raw_summary).split())[:600]
            published = str(paper.get("publishedAt") or item.get("publishedAt") or "")[:10]
            upvotes = int(paper.get("upvotes") or item.get("upvotes") or 0)
            github = str(paper.get("githubRepo") or item.get("githubRepo") or "")
            stars = int(paper.get("githubStars") or item.get("githubStars") or 0)

            records.append({
                "id": str(pid),
                "url": f"https://huggingface.co/papers/{pid}",
                "published": published,
                "title": title,
                "summary": summary,
                "upvotes": upvotes,
                "github": github,
                "stars": stars,
            })

        if not records:
            return "NO RESULTS"
        return json.dumps(records, ensure_ascii=False)
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {redact_secrets(str(exc))}"


# ---- TODO 4: web search / fetch through the Exa MCP endpoint ----
def _call_exa_mcp(tool_name: str, arguments: dict) -> str:
    key = (os.getenv("EXA_API_KEY") or "").strip()
    url = f"{EXA_URL}?exaApiKey={key}" if key else EXA_URL
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }

    def _request():
        with httpx.Client(timeout=45.0) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            text = resp.text

            data_json = None
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    raw_data = line[len("data:"):].strip()
                    try:
                        data_json = json.loads(raw_data)
                        break
                    except Exception:
                        pass

            if not data_json:
                try:
                    data_json = resp.json()
                except Exception:
                    raise RuntimeError("Could not parse JSON response from Exa MCP")

            if "error" in data_json:
                err = data_json["error"]
                err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                if any(x in err_msg.lower() for x in ["rate limit", "too many requests", "429"]):
                    raise RetryableError(f"Exa rate limited: {err_msg}")
                raise RuntimeError(f"Exa MCP error: {err_msg}")

            result = data_json.get("result", {})
            meta = result.get("_meta", {})
            meta_str = json.dumps(meta).lower()
            if any(x in meta_str for x in ["rate", "limit", "quota", "exceeded"]):
                raise RetryableError(f"Exa rate limited in _meta: {meta}")

            content_list = result.get("content", [])
            texts = [c.get("text", "") for c in content_list if isinstance(c, dict) and c.get("type") == "text"]
            full_text = "\n".join(texts)

            if any(x in full_text.lower() for x in ["rate limit exceeded", "too many requests", "daily limit reached"]):
                raise RetryableError("Exa rate limit warning in response content")

            return full_text

    return with_retry(_request, attempts=5, base=2.0, cap=60.0)


@tool
def web_search(query: str, objective: str = "", num_results: int = 5) -> str:
    """Search the web (Exa). Describe the ideal page in natural language. Returns clean text of the top results with URLs."""
    try:
        q = query.strip()
        if not q:
            return "NO RESULTS"
        obj = objective.strip() or f"Find comprehensive research and surveys about {q}"
        num = max(1, min(num_results, 10))
        text = _call_exa_mcp("web_search_exa", {"query": q, "objective": obj, "numResults": num})
        if not text or not text.strip():
            return "NO RESULTS"
        return text
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {redact_secrets(str(exc))}"


@tool
def web_fetch(url: str) -> str:
    """Read the full content of one web page (e.g. an arXiv abstract page) as markdown. Long pages are truncated."""
    try:
        u = url.strip()
        if not u:
            return "NO RESULTS"
        text = _call_exa_mcp("web_fetch_exa", {"urls": [u]})
        if not text or not text.strip():
            return "NO RESULTS"
        return text[:12000]
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {redact_secrets(str(exc))}"


# ---- TODO 5: registry (the researcher subagent gets exactly these) ----
SOURCE_TOOLS = [arxiv_search, hf_daily_papers, hf_search_papers, web_search, web_fetch]


if __name__ == "__main__":
    for name, fn, args in [
        ("arxiv_search", arxiv_search, {"query": "world model", "max_results": 3}),
        ("hf_daily_papers", hf_daily_papers, {"limit": 20}),
        ("hf_search_papers", hf_search_papers, {"query": "world model", "limit": 3}),
        ("web_search", web_search, {"query": "survey paper on world models", "num_results": 2}),
        ("web_fetch", web_fetch, {"url": "https://arxiv.org/abs/1803.10122"}),
    ]:
        try:
            print(f"== {name}\n{fn.invoke(args)[:400]}\n")
        except NotImplementedError as exc:
            print(f"== {name}: not implemented yet ({exc})\n")
