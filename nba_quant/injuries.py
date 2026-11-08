"""Archive the official report without inventing structured eligibility from PDF text."""
import hashlib
import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from .schemas import utc

PAGE_URL = "https://official.nba.com/nba-injury-report-2026-27-season/"
PDF_PATTERN = re.compile(r"https://ak-static\.cms\.nba\.com/referee/injury/Injury-Report_(\d{4}-\d{2}-\d{2})_(\d{2})_(\d{2})(AM|PM)\.pdf")


def report_candidates(html, now):
    matches = []
    for match in PDF_PATTERN.finditer(html):
        date, hour, minute, meridiem = match.groups()
        published = datetime.strptime(f"{date} {hour}:{minute}{meridiem}", "%Y-%m-%d %I:%M%p").replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
        if published <= now:
            matches.append((published, match.group(0)))
    return sorted(set(matches), reverse=True)


def refresh_report(output_dir, now=None, fetch=None, source_url=None):
    actual_now = datetime.now(timezone.utc)
    now = utc(now) if now is not None else actual_now
    if now > actual_now:
        raise ValueError("Report retrieval time cannot be in the future")
    def default_fetch(url):
        with urllib.request.urlopen(url, timeout=30) as response:
            content = response.read(20_000_001)
            if len(content) > 20_000_000:
                raise ValueError("Official report response exceeds size limit")
            return content
    fetch = fetch or default_fetch
    # A supplied URL must still be the exact official host and filename format.
    # It permits archiving an explicitly verified report when index access fails.
    if source_url is not None:
        if not PDF_PATTERN.fullmatch(source_url):
            raise ValueError("Only an exact official NBA injury PDF URL is accepted")
        candidates = report_candidates(source_url, now)
    else:
        html = fetch(PAGE_URL)
        candidates = report_candidates(html.decode("utf-8"), now)
    if not candidates:
        raise ValueError("No official report published at or before retrieval time")
    published, url = candidates[0]
    raw = fetch(url)
    if not raw.startswith(b"%PDF"):
        raise ValueError("Official report is not a PDF")
    sha = hashlib.sha256(raw).hexdigest()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{sha}.pdf"
    path.write_bytes(raw)
    receipt = {"provider": "NBA Official", "source_url": url, "index_url": PAGE_URL,
        "report_published_at": published.isoformat(), "retrieved_at": now.isoformat(),
        "known_at": now.isoformat(), "sha256": sha, "raw_path": str(path.resolve()),
        "status": "RAW_OFFICIAL_REPORT", "identity_mapping": "UNREVIEWED",
        "selection": "explicit_official_url" if source_url else "latest_published_index_link",
        "eligible_for_automatic_roster_gate": False,
        "note": "Official raw report preserved. PDF text is not automatically converted into player active probabilities or minutes restrictions."}
    (out / f"{sha}.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    (out / "latest.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


def latest_report(output_dir):
    path = Path(output_dir) / "latest.json"
    if not path.exists():
        return None
    metadata = json.loads(path.read_text(encoding="utf-8"))
    # Verify actual bytes, rather than trusting the latest-pointer status.
    raw = Path(metadata["raw_path"])
    if not raw.is_file() or hashlib.sha256(raw.read_bytes()).hexdigest() != metadata["sha256"]:
        raise ValueError("Official injury report archive hash mismatch")
    return metadata
