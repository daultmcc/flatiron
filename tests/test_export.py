from pathlib import Path

import numpy as np
import pytest

from flatiron.apo import ApoFilter, parse_apo
from flatiron.export import apo, easyeffects, lsp
from flatiron.export.plan import ExportPlan, build_plan, response_db

FIXTURES = Path(__file__).parent / "fixtures"
LSP_GOLDEN = FIXTURES / "lsp" / "para_equalizer_x32_lr-1.2.35.cfg"
EE_GOLDEN = FIXTURES / "easyeffects" / "preset-split.json"


def use_fit(monkeypatch, fit):
    """Stand in for the profile's computed fit."""
    from types import SimpleNamespace

    import flatiron.cli as cli

    monkeypatch.setattr(cli, "_current", lambda prof: SimpleNamespace(summary=lambda: fit))


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

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.chdir(tmp_path)
    prof = Profile.open("HD 600")
    assert main(["export", "-t", "apo"]) == 1  # the only profile, but no trials to fit yet
    assert "Can't fit 'HD 600' yet" in capsys.readouterr().out
    use_fit(monkeypatch, fit_entry([(6150, -4.2, 6.5)], [(7020, -3.1, 8.0)], -0.5))
    # -o names the file; APO gets one per channel.
    capsys.readouterr()
    assert main(["export", "-t", "lsp", "-o", "out/hd600.cfg"]) == 1  # no such folder: say so, don't create it
    assert "the folder out doesn't exist" in capsys.readouterr().out and not (tmp_path / "out").exists()
    (tmp_path / "out").mkdir()
    assert main(["export", "-t", "lsp", "-o", "out/hd600.cfg"]) == 0
    assert main(["export", "HD 600", "-t", "apo", "-o", "out/hd600"]) == 0
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["hd600.cfg", "hd600_L.txt", "hd600_R.txt"]
    # Default: a descriptive name in the current directory, not a hidden data folder.
    assert main(["export", "-t", "lsp"]) == 0
    assert len(list(tmp_path.glob("hd-600-*.cfg"))) == 1
    assert not (prof.dir / "exports").exists()
    pref = tmp_path / "pref.txt"
    pref.write_text("Preamp: -6 dB\nFilter 1: ON LSC Fc 105 Hz Gain 5.5 dB Q 0.7\n")
    capsys.readouterr()
    assert main(["export", "-t", "lsp", "--merge", str(pref), "-o", "merged.cfg"]) == 0
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


def test_easyeffects_golden_file_byte_for_byte():
    """Exported from EasyEffects 7 with split channels. Writing its own 32 bands
    (mode RLC BT, as exported) into the template reproduces it exactly."""
    import json

    eq = json.loads(EE_GOLDEN.read_text())["output"]["equalizer#0"]
    chans = {e: [ApoFilter("PK", b["frequency"], b["gain"], b["q"])
                 for b in (eq[side][f"band{i}"] for i in range(32))]
             for e, side in (("L", "left"), ("R", "right"))}
    plan = ExportPlan(chans, 0.0, False, "x")
    assert easyeffects.write(plan, mode="RLC (BT)") == EE_GOLDEN.read_text()


def test_easyeffects_preset_fields():
    import json

    plan = ExportPlan({"L": [ApoFilter("LS", 105, 5.5, 0.7), ApoFilter("PK", 6150, -4.2, 6.5)],
                       "R": [ApoFilter("PK", 7020, -3.1, 8.0)]}, -6.2, True, "x")
    eq = json.loads(easyeffects.write(plan))["output"]["equalizer#0"]
    assert eq["split-channels"] is True and eq["num-bands"] == 2 and eq["input-gain"] == -6.2
    assert eq["left"]["band0"]["type"] == "Lo-shelf"
    assert (eq["left"]["band1"]["type"], eq["left"]["band1"]["mode"], eq["left"]["band1"]["frequency"],
            eq["left"]["band1"]["gain"], eq["left"]["band1"]["q"]) == ("Bell", "APO (DR)", 6150, -4.2, 6.5)
    assert eq["right"]["band1"]["type"] == "Off"  # right uses one band; num-bands covers the longer side
    # The template carries no gains of its own.
    tpl = easyeffects.template()["output"]["equalizer#0"]
    assert all(b["gain"] == 0 for side in ("left", "right") for b in tpl[side].values())


@pytest.mark.skipif(not __import__("flatiron.export.verify", fromlist=["x"]).available(),
                    reason="LSP LADSPA build not installed")
def test_easyeffects_truth_check_against_real_plugin():
    from flatiron.export.verify import check_easyeffects

    plan = ExportPlan({"L": [ApoFilter("LS", 105, 5.5, 0.7), ApoFilter("PK", 6150, -4.2, 6.5),
                             ApoFilter("PK", 11000, 6, 12)],
                       "R": [ApoFilter("PK", 7020, -3.1, 8.0)]}, -6.5, True, "x")
    assert max(check_easyeffects(easyeffects.write(plan), plan).values()) < 0.3
    assert max(check_easyeffects(easyeffects.write(plan, mode="RLC (BT)"), plan).values()) > 0.3


def test_pipewire_dropin():
    from flatiron.export import pipewire

    plan = ExportPlan({"L": [ApoFilter("LS", 105, 5.5, 0.7), ApoFilter("PK", 6150, -4.2, 6.5)],
                       "R": [ApoFilter("PK", 7020, -3.1, 8.0)]}, -6.2, True, "abc123")
    text = pipewire.write(plan, "HD 600", "hd-600")
    left = text.split("filters1 = [")[1].split("]")[0]
    right = text.split("filters2 = [")[1].split("]")[0]
    assert "bq_highshelf, freq = 0, gain = -6.20" in left and "bq_highshelf, freq = 0, gain = -6.20" in right
    assert "bq_lowshelf, freq = 105.00, gain = 5.50, q = 0.7000" in left
    assert "bq_peaking, freq = 6150.00, gain = -4.20, q = 6.5000" in left and "7020.00" in right
    assert 'node.name = "effect_input.flatiron.hd-600"' in text and 'node.description = "Flatiron HD 600"' in text
    assert text.count("{") == text.count("}") and text.count("[") == text.count("]")


@pytest.mark.skipif(not __import__("flatiron.export.verify", fromlist=["x"]).pipewire_available(),
                    reason="PipeWire not running")
def test_pipewire_truth_check(tmp_path):
    """Plays noise through a private filter-chain (virtual sink → param_eq →
    virtual source) on the running PipeWire; nothing reaches a device."""
    from flatiron.export.verify import check_pipewire

    plan = ExportPlan({"L": [ApoFilter("PK", 6150, -4.2, 6.5), ApoFilter("HS", 10000, -3, 0.7)],
                       "R": [ApoFilter("PK", 7020, -3.1, 8.0), ApoFilter("PK", 11000, 6, 12)]}, -6.5, True, "x")
    # A real-time capture: with every core busy running the other tests in
    # parallel it can drop audio, so one retry.
    errs = [max(check_pipewire(plan, tmp_path).values())]
    if errs[0] >= 0.3:
        errs.append(max(check_pipewire(plan, tmp_path).values()))
    assert errs[-1] < 0.3, errs


def test_export_preamp_override(tmp_path, monkeypatch, capsys):
    from flatiron.cli import main
    from flatiron.session.store import Profile

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.chdir(tmp_path)
    Profile.open("HD 600")
    use_fit(monkeypatch, fit_entry([(6150, -4.2, 6.5), (9000, 3, 4)], [(7020, -3.1, 8.0)], -3.5))
    assert main(["export", "-t", "apo", "-o", "safe"]) == 0
    assert (tmp_path / "safe_L.txt").read_text().startswith("Preamp: -3.50 dB")
    capsys.readouterr()
    assert main(["export", "-t", "apo", "-o", "zero", "--preamp", "0"]) == 0
    assert (tmp_path / "zero_L.txt").read_text().startswith("Preamp: 0.00 dB")
    assert "can clip" in capsys.readouterr().out
    assert main(["export", "-t", "apo", "--preamp", "loud"]) == 1


def test_tincan_preset_matches_the_plan():
    import tomllib

    from flatiron.export import tincan

    pref = {e: parse_apo("Preamp: -6 dB\nFilter 1: ON LSC Fc 105 Hz Gain 5.5 dB Q 0.7\n") for e in "LR"}
    plan = build_plan(fit_entry([(6150.4, -4.23, 6.5)], [(7020, -3.1, 8.0), (11000, 2, 4)]), pref)
    doc = tomllib.loads(tincan.write(plan, 'HD 600 "merged"'))
    assert doc["format"] == 1 and doc["name"] == 'HD 600 "merged"' and "preamp" not in str(doc)
    # The shared shelf is written once, for both channels.
    assert [(f["channel"], f["type"]) for f in doc["filter"]] == [
        ("LR", "low_shelf"), ("L", "bell"), ("R", "bell"), ("R", "bell")]
    kinds = {v: k for k, v in tincan.TYPE.items()}
    for e in "LR":
        back = [ApoFilter(kinds[f["type"]], f["frequency"], f["gain"], f["q"])
                for f in doc["filter"] if e in f["channel"]]
        np.testing.assert_allclose(response_db(back), response_db(plan.channels[e]), atol=1e-3)
    with pytest.raises(ValueError, match="Q"):
        tincan.write(build_plan(fit_entry([(6150, -4, 60)], [])), "x")
    assert tincan.slug("  Sundara: Flatiron 1a2b ") == "sundara-flatiron-1a2b"


def test_tincan_export_cli(tmp_path, monkeypatch, capsys):
    from flatiron.cli import main
    from flatiron.session.store import Profile

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.chdir(tmp_path)
    Profile.open("HD 600")
    use_fit(monkeypatch, fit_entry([(6150, -4.2, 6.5)], [(7020, -3.1, 8.0)]))
    assert main(["export", "-t", "tincan"]) == 1  # TinCan hasn't made its folder: say so, don't create it
    assert "start TinCan once" in capsys.readouterr().out
    presets = tmp_path / "data" / "tincan" / "presets"
    presets.mkdir(parents=True)
    assert main(["export", "-t", "tincan"]) == 0
    (out,) = presets.glob("hd-600-flatiron-*.toml")
    assert 'name = "HD 600 Flatiron ' in out.read_text()
    assert "preamp" not in capsys.readouterr().out
    assert main(["export", "-t", "tincan", "--preamp", "0"]) == 1
    assert main(["export", "-t", "tincan", "-o", "mine.toml"]) == 0 and (tmp_path / "mine.toml").exists()
