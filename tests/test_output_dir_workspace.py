"""Tests for the ``--output-dir`` / WORKROOT workspace-root feature (plugin 1.1.0).

These cover the SCRIPT-level hardening that makes the single-root contract hold when
a caller redirects the workspace with ``--output-dir <WORKROOT>``:

  * ``score_composite._resolve_default_config`` -- the weights-config auto-load
    resolves from the bundle's WORKSPACE ROOT, not the process CWD;
  * ``refresh_plan._scales_dirs`` / ``_pending_proposals`` -- scale/proposal
    discovery derives from the ticker-dir parent (the workspace root), never CWD;
  * ``render_pdf._scale_workspace_root`` -- the methodology-page scale lookup walks
    up to the workspace root.

For an un-redirected run the bundle/ticker-dir sits under the CWD, so the derivation
reaches the CWD and the result is byte-identical to the pre-1.1.0 behavior (asserted
by the ``*_no_flag_parity`` cases). The SKILL-prose threading of ``--output-dir`` is
model-interpreted, not code, and is verified by the caller's live acceptance run --
not by this suite.
"""
import json
import os

from scripts import score_composite, refresh_plan, render_pdf


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh)


# --------------------------------------------------------------------------- #
# score_composite._resolve_default_config
# --------------------------------------------------------------------------- #

def test_resolve_default_config_redirect_new_layout(tmp_path, monkeypatch):
    """Redirected run: config under <WORKROOT> is found though the CWD is elsewhere."""
    ws = tmp_path / "ws"
    bundle = ws / "trading_desk_MU" / "detail_reports_2026-07-24"
    bundle.mkdir(parents=True)
    cfg = ws / "trading_desk_config.json"
    _write_json(str(cfg), {"weights": {}})
    caller = tmp_path / "caller_repo"
    caller.mkdir()
    monkeypatch.chdir(caller)

    got = score_composite._resolve_default_config(str(bundle))
    assert got is not None and os.path.samefile(got, str(cfg))


def test_resolve_default_config_redirect_legacy_layout(tmp_path, monkeypatch):
    """Legacy ``td_bundle_<T>_<date>`` bundle: WORKROOT is one level up."""
    ws = tmp_path / "ws"
    bundle = ws / "td_bundle_MU_2026-07-24"
    bundle.mkdir(parents=True)
    cfg = ws / "trading_desk_config.json"
    _write_json(str(cfg), {"weights": {}})
    caller = tmp_path / "caller_repo"
    caller.mkdir()
    monkeypatch.chdir(caller)

    got = score_composite._resolve_default_config(str(bundle))
    assert got is not None and os.path.samefile(got, str(cfg))


def test_resolve_default_config_no_flag_parity(tmp_path, monkeypatch):
    """No redirect: run FROM the workspace root -> resolves to the CWD config."""
    ws = tmp_path / "ws"
    bundle = ws / "trading_desk_MU" / "detail_reports_2026-07-24"
    bundle.mkdir(parents=True)
    cfg = ws / "trading_desk_config.json"
    _write_json(str(cfg), {"weights": {}})
    monkeypatch.chdir(ws)

    got = score_composite._resolve_default_config(
        "./trading_desk_MU/detail_reports_2026-07-24")
    assert got is not None and os.path.samefile(got, str(cfg))


def test_resolve_default_config_absent_returns_none(tmp_path, monkeypatch):
    """No config anywhere -> None (standard weights, unchanged)."""
    ws = tmp_path / "ws"
    bundle = ws / "trading_desk_MU" / "detail_reports_2026-07-24"
    bundle.mkdir(parents=True)
    caller = tmp_path / "caller_repo"
    caller.mkdir()
    monkeypatch.chdir(caller)

    assert score_composite._resolve_default_config(str(bundle)) is None


def test_resolve_default_config_workspace_beats_stray_cwd(tmp_path, monkeypatch):
    """A stray config in the CWD must NOT shadow the workspace config under redirect."""
    ws = tmp_path / "ws"
    bundle = ws / "trading_desk_MU" / "detail_reports_2026-07-24"
    bundle.mkdir(parents=True)
    ws_cfg = ws / "trading_desk_config.json"
    _write_json(str(ws_cfg), {"weights": {"balanced": {}}})
    caller = tmp_path / "caller_repo"
    caller.mkdir()
    _write_json(str(caller / "trading_desk_config.json"), {"weights": {"trader": {}}})
    monkeypatch.chdir(caller)

    got = score_composite._resolve_default_config(str(bundle))
    assert got is not None and os.path.samefile(got, str(ws_cfg))


# --------------------------------------------------------------------------- #
# refresh_plan._scales_dirs / _pending_proposals
# --------------------------------------------------------------------------- #

def test_scales_dirs_redirect_uses_ticker_parent(tmp_path, monkeypatch):
    """Redirect: scales resolve at the ticker-dir parent; a stray CWD scales dir is ignored."""
    ws = tmp_path / "ws"
    ticker_dir = ws / "trading_desk_MU"
    ticker_dir.mkdir(parents=True)
    scales = ws / "trading_desk_config" / "scales"
    scales.mkdir(parents=True)
    caller = tmp_path / "caller_repo"
    (caller / "trading_desk_config" / "scales").mkdir(parents=True)
    monkeypatch.chdir(caller)

    dirs = refresh_plan._scales_dirs(str(ticker_dir))
    assert dirs == [os.path.realpath(str(scales))]


def test_scales_dirs_no_flag_parity(tmp_path, monkeypatch):
    """No redirect: relative ticker-dir -> scales resolve under the CWD (unchanged)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "trading_desk_MU").mkdir()
    scales = ws / "trading_desk_config" / "scales"
    scales.mkdir(parents=True)
    monkeypatch.chdir(ws)

    dirs = refresh_plan._scales_dirs("./trading_desk_MU")
    assert dirs == [os.path.realpath(str(scales))]


def test_pending_proposals_redirect(tmp_path, monkeypatch):
    """Redirect: proposals resolve at the workspace root, not the CWD."""
    ws = tmp_path / "ws"
    ticker_dir = ws / "trading_desk_MU"
    ticker_dir.mkdir(parents=True)
    props = ws / "trading_desk_config" / "scales" / "proposals"
    props.mkdir(parents=True)
    _write_json(str(props / "semis_rerate_2.json"), {"status": "pending_ratification"})
    caller = tmp_path / "caller_repo"
    caller.mkdir()
    monkeypatch.chdir(caller)

    assert refresh_plan._pending_proposals(str(ticker_dir)) == ["semis_rerate_2.json"]


# --------------------------------------------------------------------------- #
# render_pdf._scale_workspace_root
# --------------------------------------------------------------------------- #

def test_scale_workspace_root_new_layout(tmp_path):
    ws = tmp_path / "ws"
    bundle = ws / "trading_desk_MU" / "detail_reports_2026-07-24"
    bundle.mkdir(parents=True)
    (ws / "trading_desk_config" / "scales").mkdir(parents=True)

    got = render_pdf._scale_workspace_root(str(bundle))
    assert got is not None and os.path.samefile(got, str(ws))


def test_scale_workspace_root_legacy_layout(tmp_path):
    ws = tmp_path / "ws"
    bundle = ws / "td_bundle_MU_2026-07-24"
    bundle.mkdir(parents=True)
    (ws / "trading_desk_config" / "scales").mkdir(parents=True)

    got = render_pdf._scale_workspace_root(str(bundle))
    assert got is not None and os.path.samefile(got, str(ws))


def test_scale_workspace_root_none_when_absent(tmp_path):
    ws = tmp_path / "ws"
    bundle = ws / "trading_desk_MU" / "detail_reports_2026-07-24"
    bundle.mkdir(parents=True)

    assert render_pdf._scale_workspace_root(str(bundle)) is None


# --------------------------------------------------------------------------- #
# v1.2.0 — FLAT layout under --output-dir: the ticker-dir IS the workspace root,
# so scale/proposal discovery must find trading_desk_config/scales directly under
# it (0 up), not at its parent. The walk-up handles both without a --flat flag.
# --------------------------------------------------------------------------- #

def test_scales_dirs_flat_layout(tmp_path):
    """Flat: --ticker-dir = <WORKROOT>; scales sit directly under it."""
    ws = tmp_path / "ws"            # the flat --output-dir root == the ticker dir
    scales = ws / "trading_desk_config" / "scales"
    scales.mkdir(parents=True)

    assert refresh_plan._scales_dirs(str(ws)) == [os.path.realpath(str(scales))]


def test_pending_proposals_flat_layout(tmp_path):
    ws = tmp_path / "ws"
    props = ws / "trading_desk_config" / "scales" / "proposals"
    props.mkdir(parents=True)
    _write_json(str(props / "semis_rerate_2.json"), {"status": "pending_ratification"})

    assert refresh_plan._pending_proposals(str(ws)) == ["semis_rerate_2.json"]


def test_scales_dirs_flat_and_nested_agree_on_workspace(tmp_path):
    """Same physical scales dir is found whether the ticker-dir is the flat root
    or the nested trading_desk_<T> child — the walk-up is layout-agnostic."""
    ws = tmp_path / "ws"
    scales = ws / "trading_desk_config" / "scales"
    scales.mkdir(parents=True)
    nested_ticker = ws / "trading_desk_MU"
    nested_ticker.mkdir()

    flat = refresh_plan._scales_dirs(str(ws))
    nested = refresh_plan._scales_dirs(str(nested_ticker))
    assert flat == nested == [os.path.realpath(str(scales))]


# --------------------------------------------------------------------------- #
# v1.2.0 — --prev-dir: find_previous_bundle rooted at an explicit prior workspace.
# --------------------------------------------------------------------------- #

def test_find_previous_bundle_flat_layout(tmp_path):
    """A flat prior workspace: detail_reports_* are immediate children."""
    prev = tmp_path / "prev"
    (prev / "detail_reports_2026-07-22").mkdir(parents=True)
    (prev / "detail_reports_2026-07-23").mkdir()

    got = refresh_plan.find_previous_bundle(str(prev))
    assert os.path.basename(got) == "detail_reports_2026-07-23"  # newest by name


def test_find_previous_bundle_prev_dir_vs_fresh_output_dir(tmp_path):
    """The --prev-dir case: prior lives in PREV_DIR; the fresh --output-dir is empty.
    find_previous_bundle(PREV) resolves the prior; find_previous_bundle(NEW) refuses."""
    prev = tmp_path / "prev"
    (prev / "detail_reports_2026-07-23").mkdir(parents=True)
    new = tmp_path / "new"
    new.mkdir()  # fresh, empty --output-dir

    got = refresh_plan.find_previous_bundle(str(prev))
    assert os.path.basename(got) == "detail_reports_2026-07-23"

    try:
        refresh_plan.find_previous_bundle(str(new))
        assert False, "expected PlanError on an empty fresh workspace"
    except refresh_plan.PlanError:
        pass
