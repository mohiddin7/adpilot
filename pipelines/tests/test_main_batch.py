import hashlib
import io
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common  # noqa: E402

main = common.load_script("main")
gen = common.load_script("00_generate_synthetic")
TODAY = date(2026, 9, 24)
CSVS = ["facebook_ads.csv", "google_ads.csv", "tiktok_ads.csv"]


def test_utc_today_uses_the_utc_date():
    new_york = timezone(timedelta(hours=-4))
    assert main.utc_today(datetime(2026, 9, 23, 22, 30, tzinfo=new_york)) == date(2026, 9, 24)


@pytest.mark.parametrize("gold_max, window", [
    (date(2026, 9, 22), (date(2026, 9, 21), date(2026, 9, 23))),   # normal day: restating days + the one turning final
    (date(2026, 9, 15), (date(2026, 9, 16), date(2026, 9, 23))),   # missed days: catch up from gold
    (date(2026, 9, 23), (date(2026, 9, 21), date(2026, 9, 23))),   # re-run the same day
    (date(2026, 8, 23), (date(2026, 8, 24), date(2026, 9, 23))),   # exactly MAX_CATCHUP_DAYS
])
def test_batch_window(gold_max, window):
    assert main.batch_window(gold_max, TODAY) == window


def test_window_refuses_empty_gold_and_long_gaps():
    with pytest.raises(RuntimeError, match="backfill"):
        main.batch_window(None, TODAY)
    with pytest.raises(RuntimeError, match="backfill"):
        main.batch_window(date(2026, 8, 22), TODAY)


def _batch(start=date(2026, 9, 21)):
    return main.build_batch(start, date(2026, 9, 23), date(2026, 9, 24), today=TODAY)


def test_batch_round_trip_verifies():
    files = _batch()
    manifest = json.loads(files.pop(main.MANIFEST))
    main.verify_batch(manifest, files)
    assert sorted(f["name"] for f in manifest["files"]) == CSVS
    assert (manifest["start"], manifest["end"], manifest["as_of"]) == ("2026-09-21", "2026-09-23", "2026-09-24")
    assert manifest["batch"] == "2026-09-24" and manifest["generator_version"] == gen.GENERATOR_VERSION


@pytest.mark.parametrize("tamper", ["drop", "truncate", "flip", "extra"])
def test_verify_refuses_a_tampered_batch(tamper):
    files = _batch()
    manifest = json.loads(files.pop(main.MANIFEST))
    if tamper == "drop":
        files.pop("google_ads.csv")
    elif tamper == "truncate":
        files["google_ads.csv"] = files["google_ads.csv"].rsplit(b"\n", 2)[0] + b"\n"
    elif tamper == "flip":                                  # same row count, one digit changed
        body = files["google_ads.csv"]
        files["google_ads.csv"] = body[:-2] + (b"1" if body[-2:-1] != b"1" else b"2") + b"\n"
    else:
        files["evil.csv"] = b"x\n"
    with pytest.raises(RuntimeError):
        main.verify_batch(manifest, files)


def test_empty_platform_file_still_verifies():
    body = b"date,campaign_id\n"
    main.verify_batch({"files": [{"name": "google_ads.csv", "rows": 0, "sha256": hashlib.sha256(body).hexdigest()}]},
                      {"google_ads.csv": body})


def test_overlapping_batches_agree():
    wide, narrow = _batch(date(2026, 9, 19)), _batch(date(2026, 9, 21))
    for name in CSVS:
        a, b = pd.read_csv(io.BytesIO(wide[name])), pd.read_csv(io.BytesIO(narrow[name]))
        pd.testing.assert_frame_equal(a[a.date >= "2026-09-21"].reset_index(drop=True), b)


class _Blob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def upload_from_string(self, data, content_type=None):
        self.bucket.order.append(self.name)
        self.bucket.objects[self.name] = data


class _Bucket:
    def __init__(self):
        self.order, self.objects = [], {}

    def blob(self, name):
        return _Blob(self, name)


def test_upload_writes_the_manifest_last():
    bucket = _Bucket()
    path = main.upload_batch(bucket, _batch())
    assert path == "landing/2026-09-24/_manifest.json" and bucket.order[-1] == path
    assert sorted(bucket.order[:-1]) == [f"landing/2026-09-24/{n}" for n in CSVS]
