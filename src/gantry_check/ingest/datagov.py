"""The data.gov.sg poll-download dance, shared by every dataset we pull from there.

A dataset is not served directly: you GET a poll endpoint until it hands back a short-lived
signed S3 URL, then GET that. Some datasets report `status: DOWNLOAD_SUCCESS` alongside the
URL (the public holiday CSVs do); others return the URL with no status at all (the LTA gantry
GeoJSON does), so the URL -- not the status -- is what decides success.
"""

from __future__ import annotations

import time

import httpx

POLL_DOWNLOAD_URL = "https://api-open.data.gov.sg/v1/public/api/datasets/{dataset_id}/poll-download"

#: Statuses that mean "the export is ready"; `None` covers responses that omit the field.
_READY_STATUSES = frozenset({"DOWNLOAD_SUCCESS"})

_POLL_ATTEMPTS = 5
_POLL_DELAY_S = 1.0


def download_dataset_text(client: httpx.Client, dataset_id: str) -> str:
    """Resolve a dataset's signed download URL and fetch it as text.

    The first poll can come back still preparing the export, hence the retry.
    """
    url = POLL_DOWNLOAD_URL.format(dataset_id=dataset_id)
    for attempt in range(_POLL_ATTEMPTS):
        response = client.get(url)
        response.raise_for_status()
        payload = response.json().get("data") or {}
        status = payload.get("status")
        if payload.get("url") and (status is None or status in _READY_STATUSES):
            download = client.get(payload["url"], follow_redirects=True)
            download.raise_for_status()
            return download.text
        if attempt < _POLL_ATTEMPTS - 1:
            time.sleep(_POLL_DELAY_S)
    raise RuntimeError(f"dataset {dataset_id} never returned a download URL")
