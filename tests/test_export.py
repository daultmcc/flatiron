from pathlib import Path

import numpy as np
import pytest

from flatiron.apo import ApoFilter, parse_apo
from flatiron.export import apo, lsp
from flatiron.export.plan import ExportPlan, build_plan, response_db

FIXTURES = Path(__file__).parent / "fixtures"
LSP_GOLDEN = FIXTURES / "lsp" / "para_equalizer_x32_lr-1.2.35.cfg"


def fit_entry(left, right, preamp=-0.5):
    return {"preamp_db": preamp, "ears": {
        "L": {"filters": [{"fc": a, "gain_db": b, "q": c} for a, b, c in left]},
        "R": {"filters": [{"fc": a, "gain_db": b, "q": c} for a, b, c in right]}}}


def test_lsp_golden_file_byte_for_byte():
    """Exported from LSP 1.2.35: band 0 = Bell 4 kHz Q 3, +1 dB left, −1 dB right,
    mode RLC (BT). Writing the same bands into the template reproduces it."""
    plan = ExportPlan({"L": [ApoFilter("PK", 4000, 1.0, 3.0)], "R": [ApoFilter("PK", 4000, -1.0, 3.0)]},
                      0.0, False, "x")
    assert lsp.write(plan, mode=0) == LSP_GOLDEN.read_text()


def test_lsp_uses_apo_mode_shelves_and_turns_unused_bands_off():
    plan = ExportPlan({"L": [ApoFilter("LS", 105, 5.5, 0.7), ApoFilter("PK", 6150, -4.2, 6.5)],
                       "R": [ApoFilter("HS", 10000, -3, 0.7)]}, -6.2, True, "x")
    text = lsp.write(plan)
    kv = dict(line.split(" = ", 1) for line in text.splitlines() if " = " in line and not line.startswith("#"))
    assert kv["g_in"] == "-6.20 db" and kv["clink"] == "false" and kv["mode"] == "0"
    assert (kv["ftl_0"], kv["fml_0"], kv["fl_0"], kv["gl_0"], kv["ql_0"]) == ("5", "6", "105.00000", "5.50 db", "0.70000")
    assert (kv["ftl_1"], kv["fml_1"], kv["fl_1"], kv["gl_1"]) == ("1", "6", "6150.00000", "-4.20 db")
    assert (kv["ftr_0"], kv["fr_0"]) == ("3", "10000.00000")
    assert all(kv[f"ftl_{i}"] == "0" for i in range(2, 32)) and all(kv[f"ftr_{i}"] == "0" for i in range(1, 32))
    # Everything outside the band, gain and mode lines is the template, untouched.
    tpl = lsp.template().splitlines()
    changed = [a for a, b in zip(tpl, text.splitlines()) if a != b]
    assert all(x.split(" = ")[0].rstrip("0123456789").rstrip("_")[:-1] in ("ft", "fm", "f", "g", "q", "s", "xs", "xm")
               or x.startswith("g_in") for x in changed)


def test_lsp_refuses_more_than_32_bands():
    plan = ExportPlan({"L": [ApoFilter("PK", 1000 + i, 1, 1) for i in range(33)], "R": []}, 0, False, "x")
    with pytest.raises(ValueError, match="32"):
        lsp.write(plan)


def test_apo_text_round_trips():
    fit = fit_entry([(6150.4, -4.23, 6.5)], [(7020, -3.1, 8.0), (11000, 2, 4)], preamp=-2.5)
    texts = apo.write(build_plan(fit))
    assert texts["L"] == "Preamp: -2.50 dB\nFilter 1: ON PK Fc 6150 Hz Gain -4.23 dB Q 6.500\n"
    back = parse_apo(texts["R"])
    assert back.preamp_db == -2.5 and [(f.fc, f.gain_db, f.q) for f in back.filters] == [(7020, -3.1, 8), (11000, 2, 4)]


def test_merged_plan_puts_preference_first_and_recomputes_preamp():
    fit = fit_entry([(9000, -4, 6), (6000, 3, 5)], [(7000, -2, 4)])
    pref = parse_apo("Preamp: -6 dB\nFilter 1: ON LSC Fc 105 Hz Gain 5.5 dB Q 0.7\n")
    plan = build_plan(fit, {"L": pref, "R": pref})
    assert plan.merged and [f.kind for f in plan.channels["L"]] == ["LS", "PK", "PK"]
    assert [f.fc for f in plan.channels["L"]][1:] == [6000, 9000]  # correction sorted by frequency
    peak = max(response_db(plan.channels[e]).max() for e in "LR")
    assert plan.preamp_db == pytest.approx(-peak - 0.5)
    assert build_plan(fit).fit_hash == plan.fit_hash


def test_export_cli(tmp_path, monkeypatch, capsys):
    from flatiron.cli import main
    from flatiron.session.store import Profile

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("sys.platform", "linux")
    prof = Profile.open("HD 600")
    assert main(["export", "--profile", "HD 600", "--target", "apo"]) == 1
    assert "flatiron fit" in capsys.readouterr().out
    prof.update(fit=fit_entry([(6150, -4.2, 6.5)], [(7020, -3.1, 8.0)], -0.5))
    assert main(["export", "--profile", "HD 600", "--target", "lsp"]) == 0
    assert main(["export", "--profile", "HD 600", "--target", "apo"]) == 0
    files = sorted(p.name for p in (prof.dir / "exports").iterdir())
    assert len(files) == 3 and any(f.endswith("-lsp.cfg") for f in files)
    pref = tmp_path / "pref.txt"
    pref.write_text("Preamp: -6 dB\nFilter 1: ON LSC Fc 105 Hz Gain 5.5 dB Q 0.7\n")
    assert main(["export", "--profile", "HD 600", "--target", "lsp", "--merge", str(pref)]) == 0
    assert "Merged export, bands L 2 / R 2" in capsys.readouterr().out


@pytest.mark.skipif(not __import__("flatiron.export.verify", fromlist=["x"]).available(),
                    reason="LSP LADSPA build not installed")
def test_lsp_truth_check_against_real_plugin():
    from flatiron.export.verify import check_lsp

    plan = ExportPlan({"L": [ApoFilter("LS", 105, 5.5, 0.7), ApoFilter("PK", 6150, -4.2, 6.5),
                             ApoFilter("PK", 11000, 6, 12)],
                       "R": [ApoFilter("PK", 7020, -3.1, 8.0), ApoFilter("HS", 10000, -3, 0.7)]},
                      -6.5, True, "x")
    err = check_lsp(lsp.write(plan), plan)
    assert max(err.values()) < 0.3, err  # DESIGN.md tolerance
    # The fixture's mode (RLC BT) would miss: the check has teeth.
    assert max(check_lsp(lsp.write(plan, mode=0), plan).values()) > 0.3
