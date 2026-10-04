"""Single-page operator dashboard: aggregates live cluster/alert state, the
agent's investigations (including in-progress ones), and pending approvals
onto one server-rendered page, so day-to-day testing doesn't require
switching between Grafana/Prometheus/Alertmanager/the trace viewer/the
approval UI. Those stay available and linked from the dashboard for the
cases this page doesn't cover (e.g. the actual metric graphs, or a
full span-by-span trace waterfall) - this page is a summary, not a
replacement for them.
"""
