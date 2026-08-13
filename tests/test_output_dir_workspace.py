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


# --------------------------------------------------------------------------- #
# 1.6.0 -- report-renderer workspace-root threading. Its Step-1 discovery, its
# resume rule and its prior-bundle resolution are SKILL PROSE, not code: the LLM
# reads that file and runs the globs. The document is therefore the
# implementation, and these are its regression tests. They also guard the
# stale-doc-string class that 1.5.1 made a standing release-procedure sweep.
# --------------------------------------------------------------------------- #

_SKILL_MD = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills", "report-renderer", "SKILL.md")


def _renderer_skill():
    with open(_SKILL_MD) as fh:
        return fh.read()


def test_renderer_skill_documents_all_three_flags():
    text = _renderer_skill()
    assert "--output-dir" in text
    assert "--prev-dir" in text
    assert "--fresh-skeleton" in text


def test_renderer_skill_discovery_is_flat_before_the_fallback():
    """ORDERED, never merged -- (a)'s flat find must be checked, and win, before
    (b)'s nested/legacy fallback runs. A caller who checked (b) first could
    resolve a stale legacy sibling over the flat bundle the caller meant -- a
    silent wrong-bundle render, the worst failure available on a path whose
    entire purpose is recovery."""
    text = _renderer_skill()
    flat = text.index("find <WORKROOT> -maxdepth 1 -type d -name 'detail_reports_*'")
    nested = text.index(
        "find <WORKROOT>/trading_desk_<TICKER> -maxdepth 1 -type d -name 'detail_reports_*'")
    assert flat < nested


def test_renderer_skill_keeps_guardrail_1_discovery_is_terminal():
    """NON-NEGOTIABLE. An incomplete bundle exits 2 naming the missing module;
    it must NEVER fall through and render from a different bundle. Discovery
    success is terminal; completeness is a separate, later gate."""
    assert "never a reason to resume discovery" in _renderer_skill()


def test_renderer_skill_keeps_guardrail_2_announce_the_branch():
    """A fallback that fires silently is indistinguishable from one that never
    fired, and the operator needs to know which bundle was actually rendered."""
    assert "discovery: flat under --output-dir" in _renderer_skill()


def test_renderer_skill_keeps_guardrail_3_never_assert_absence():
    """Under the caller's live host fault a denied readdir returns EMPTY rather
    than erroring, so `ls` cannot tell "nothing here" from "I cannot read this".
    The remedy must be a real byte-read: `stat` succeeds against a dead volume."""
    text = _renderer_skill()
    assert "no bundle found at" in text
    assert "succeeds against a dead volume" in text


def test_renderer_skill_binds_the_report_date_from_the_bundle():
    """CRITICAL. The recovery path runs hours-to-days after the bundle was built,
    and render_report names the report from snapshot.meta.as_of_utc -- so an
    agent constructing TODAY's date misses a file that exists, takes the
    "absent -> render" branch, and overwrites ~1,700 words of authored analysis.
    That is the --fresh-skeleton outcome without the operator's consent."""
    text = _renderer_skill()
    assert "never from today" in text
    assert "Confirm by listing, not by constructing" in text


def test_renderer_skill_forbids_the_bundle_as_its_own_prior():
    """CRITICAL. This skill runs when <BUNDLE> already exists and IS the newest,
    so "newest under the workspace" resolves to <BUNDLE> itself. No script
    rejects --previous <BUNDLE>: it renders an all-zero Delta Note that reads as
    a real one."""
    assert "can never be `<PREV_BUNDLE>`" in _renderer_skill()


def test_renderer_skill_resumes_an_existing_report_by_default():
    """Never silently re-render -- it discards authored prose, and that choice
    belongs to the operator, not the machine."""
    text = _renderer_skill()
    assert "resumed existing report at" in text
    assert "--fresh-skeleton" in text


def test_renderer_skill_still_writes_the_docket_on_the_resume_path():
    """The resume path must not re-render or rewrite module_decision.json -- but
    it DOES still write pdf_slots.json and charts/. An earlier wording said
    "touch NOTHING in the bundle", which an agent obeying literally would read as
    "skip Step 5" -- shipping md-only on the very run that exists to produce the
    docket."""
    assert "the prohibition is on re-rendering" in _renderer_skill()


def test_renderer_skill_prints_a_budget_on_the_resume_path():
    """The run this feature recovers died 33 words over the cap. On a resume
    nothing renders, so nothing would print the budget -- the one path where it
    matters most."""
    assert "word_budget.py --report" in _renderer_skill()


def test_renderer_skill_can_render_the_delta_note():
    """Step 5 has always promised a three-PDF docket while issuing two
    render_pdf calls; the delta command existed only in refresh-analysis."""
    assert "--doc delta" in _renderer_skill()


def test_renderer_skill_has_no_cwd_relative_bundle_literals_left():
    """Every scripted path is absolute from <BUNDLE>. A stray CWD literal would
    silently reintroduce the exact bug this release exists to fix."""
    assert "trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD>" not in _renderer_skill()


def test_full_trade_analysis_hands_the_workspace_root_to_report_renderer():
    """full-trade-analysis is the one skill that DELEGATES to report-renderer
    rather than inlining its commands. Now that report-renderer accepts
    --output-dir, an invocation without it falls back to CWD discovery and finds
    nothing under a redirected root."""
    path = os.path.join(os.path.dirname(_SKILL_MD), "..", "full-trade-analysis",
                        "SKILL.md")
    with open(os.path.normpath(path)) as fh:
        text = fh.read()
    invoke = text.index("Invoke the **report-renderer** skill")
    assert "--output-dir <WORKROOT>" in text[invoke:invoke + 300]


def test_renderer_skill_flat_discovery_ranks_by_date_not_mtime():
    """The spec says "newest by DATE if several", and that is not mtime order.

    mtime and the date in the name diverge the moment a workspace is copied,
    restored from a backup, or rsync'd without -t -- and a recovered workspace
    is exactly what this entry point serves. Caught while staging the acceptance
    fixture: a plain `cp -RL` collapsed two real bundles to one timestamp, and
    an mtime-ordered listing then ranked the OLDER-dated bundle first. Ranking
    by mtime would silently resolve the wrong bundle precisely on the recovery
    path.

    Every branch is now name-sorted with its own `find ... | sort -r`: branch
    (b) runs the nested and legacy shapes as two SEPARATE find+sort commands
    (nested first, legacy only as a fallback) rather than mixing them in one
    mtime-ordered listing, and branch (c) gets the same two-command treatment
    for the human/CWD path.
    """
    text = _renderer_skill()
    flat = text.index("find <WORKROOT> -maxdepth 1 -type d -name 'detail_reports_*'")
    assert "sort -r | head -1" in text[flat:flat + 100]
    assert "not by mtime" in text


def test_renderer_skill_discovery_is_shell_portable():
    """Discovery must not depend on bash glob semantics.

    Under zsh -- the default macOS shell -- an unmatched glob is a NOMATCH
    error that aborts the WHOLE command before `ls` runs, so pairing two globs
    on one line makes an existing bundle invisible whenever the other glob has
    no match (the normal case: no legacy td_bundle_*). `2>/dev/null` hides the
    message, not the abort. `find` with a quoted -name pattern does its own
    matching, so it behaves identically in both shells.

    This was live in shipped 1.5.1 and defeated discovery in the human path.
    """
    text = _renderer_skill()
    discovery = text[text.index("Discovery is ORDERED"):text.index("Completeness is a SEPARATE")]
    assert "ls -d" not in discovery, "discovery must use find, not shell globs"
    assert discovery.count("find ") >= 5
    assert "-name 'detail_reports_*'" in discovery


def test_no_skill_relies_on_bash_glob_semantics():
    """No SKILL.md may put an unquoted glob in a bash block.

    Under zsh -- the macOS default -- an unmatched glob is a NOMATCH error that
    aborts the command BEFORE it runs, and `2>/dev/null` cannot suppress it
    (the shell emits it, not the command). Piped into `head`, the pipeline then
    exits 0: empty output with a success status, which reads as a legitimate
    "nothing found". For a DISCOVERY command that is the exact case being
    tested for, so the command fails precisely when it is doing its job.

    Nine such lines shipped across nine skills in 1.5.1. `find` with a QUOTED
    -name pattern does its own matching and behaves identically in both shells.
    """
    import glob as _glob
    import re as _re

    skills_dir = os.path.join(os.path.dirname(_SKILL_MD), "..")
    offenders = []
    for path in sorted(_glob.glob(os.path.join(skills_dir, "*", "SKILL.md"))):
        with open(path) as fh:
            text = fh.read()
        for block in _re.findall(r"```bash\n(.*?)```", text, _re.S):
            for line in block.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                unquoted = _re.sub(r"'[^']*'|\"[^\"]*\"", "", stripped)
                if "*" in unquoted:
                    offenders.append(f"{os.path.basename(os.path.dirname(path))}: {stripped}")
    assert offenders == [], (
        "unquoted glob in a SKILL bash block -- use find with a quoted -name:\n"
        + "\n".join(offenders))
