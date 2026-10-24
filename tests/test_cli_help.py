"""Every argparse help string must expand: Python 3.14's argparse checks them when a
(sub)parser is built and crashes the whole CLI on a bare "%" (publish failed on 24 Oct when
GitHub's runner image moved to Python 3.14)."""

import argparse
import importlib

import pytest

MODULES = [
    "soccer_stats.cli",
    "soccer_stats.edge.lineups",
    "soccer_stats.edge.totals",
    "soccer_stats.edge.corners2",
    "soccer_stats.edge.market_probe",
    "soccer_stats.edge.run",
    "soccer_stats.edge.corner_history",
    "soccer_stats.edge.corners",
    "soccer_stats.edge.corner_recal",
    "soccer_stats.lab.run",
    "soccer_stats.lab.comebacks",
]


class _Built(Exception):
    pass


def _bad_helps(parser, where, out):
    fmt = parser._get_formatter()
    actions = list(parser._actions)
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            actions += a._choices_actions
            for name, p in a.choices.items():
                _bad_helps(p, f"{where} {name}", out)
    for a in actions:
        if a.help and a.help is not argparse.SUPPRESS:
            try:
                text = fmt._expand_help(a)
            except (TypeError, ValueError, KeyError) as exc:
                out.append(f"{where} {a.dest}: {a.help!r} ({type(exc).__name__})")
                continue
            if "%" in a.help.replace("%%", "") and "%(" not in a.help:
                out.append(f"{where} {a.dest}: bare % in {a.help!r} -> {text!r}")


@pytest.mark.parametrize("module", MODULES)
def test_every_help_string_expands(module, monkeypatch):
    bad = []

    def capture(self, *args, **kwargs):
        _bad_helps(self, self.prog, bad)
        raise _Built

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", capture)
    monkeypatch.setattr(argparse.ArgumentParser, "parse_known_args", capture)
    mod = importlib.import_module(module)
    with pytest.raises(_Built):
        try:
            mod.main([])
        except TypeError:
            mod.main()
    assert not bad, "\n".join(bad)
