"""Tests for the prometheus_query tool. Requires a live Prometheus
(kube-prometheus-stack, hostPort-mapped to localhost:9090 by kind-config.yaml).
"""
from tools.prometheus_query import MAX_SERIES, query_instant, query_range_stats


def test_query_instant_returns_structured_series():
    result = query_instant('up{namespace="incident-agent"}')
    assert result["query"] == 'up{namespace="incident-agent"}'
    assert result["result_type"] == "vector"
    assert len(result["series"]) >= 1
    series = result["series"][0]
    assert {"labels", "value"} <= series.keys()
    assert series["value"] in (0.0, 1.0)


def test_query_instant_handles_bad_promql_gracefully():
    result = query_instant("this is not valid promql {{{")
    assert "error" in result
    assert result["series"] == []


def test_query_instant_caps_series_count():
    # "up" with no filter returns every scrape target cluster-wide - plenty
    # to exceed MAX_SERIES and exercise the cap.
    result = query_instant("up")
    assert len(result["series"]) <= MAX_SERIES
    if result["series_count"] > MAX_SERIES:
        assert result["truncated"] is True


def test_query_range_stats_returns_summary_not_raw_series():
    result = query_range_stats('http_requests_total{namespace="incident-agent"}', minutes=5)
    assert result["window_minutes"] == 5
    assert len(result["series"]) >= 1
    series = result["series"][0]
    # must be downsampled to stats, not a raw (timestamp, value) array
    assert {"min", "max", "avg", "last", "samples"} <= series.keys()
    assert "values" not in series


def test_query_range_stats_handles_no_data():
    result = query_range_stats('nonexistent_metric_xyz_123{namespace="incident-agent"}', minutes=5)
    assert result["series"] == []
