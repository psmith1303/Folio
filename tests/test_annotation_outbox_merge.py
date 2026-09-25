"""Pure three-way merge for offline annotation sync (annot-outbox.js).

mergeAnnotations(base, local, remote) reconciles a device's offline edits
(`local`, based on `base`) with whatever the server holds now (`remote`,
possibly changed by another device meanwhile). The rule, from the user's
choice of behaviour: additions from both sides are kept; a mark changed on
one side takes that side's version (local wins if both changed it);
deletions win; the start-page stamp is one per score and local's placement/
move/removal wins if local touched it; rotations follow whichever side
changed them.

Run as the real function under Deno via js_module_harness.run_module,
importing the actual annot-outbox.js module (api.js stubbed — mergeAnnotations
never touches it).
"""

from pathlib import Path

import pytest

from deno_harness import requires_deno
from js_module_harness import MODULES, run_module

pytestmark = requires_deno

OUTBOX_JS = MODULES / "annot-outbox.js"


def _merge(base: dict, local: dict, remote: dict) -> dict:
    return run_module(
        OUTBOX_JS, ["api"],
        f"console.log(JSON.stringify(M.mergeAnnotations({_j(base)}, {_j(local)}, {_j(remote)})));",
    )


def _j(obj: dict) -> str:
    import json
    return json.dumps(obj)


def _ink(uid: str, note: str = "x") -> dict:
    return {"uuid": uid, "type": "ink", "note": note}


def _start(uid: str = "start", x: float = 0.1, y: float = 0.1) -> dict:
    return {"uuid": uid, "type": "startpage", "x": x, "y": y}


def _state(pages: dict, rotations: dict | None = None) -> dict:
    return {"pages": pages, "rotations": rotations or {}}


EMPTY = _state({})


# ---------------------------------------------------------------------------
# Additions
# ---------------------------------------------------------------------------

def test_local_add_is_kept():
    r = _merge(EMPTY, _state({"0": [_ink("a")]}), EMPTY)
    assert r["pages"] == {"0": [_ink("a")]}


def test_remote_add_is_kept():
    r = _merge(EMPTY, EMPTY, _state({"0": [_ink("a")]}))
    assert r["pages"] == {"0": [_ink("a")]}


def test_both_add_different_marks_are_both_kept():
    r = _merge(EMPTY, _state({"0": [_ink("a")]}), _state({"0": [_ink("b")]}))
    uuids = {a["uuid"] for a in r["pages"]["0"]}
    assert uuids == {"a", "b"}


# ---------------------------------------------------------------------------
# Edits
# ---------------------------------------------------------------------------

def test_local_edit_remote_unchanged_keeps_local():
    base = _state({"0": [_ink("a", "orig")]})
    local = _state({"0": [_ink("a", "local-edit")]})
    remote = _state({"0": [_ink("a", "orig")]})
    r = _merge(base, local, remote)
    assert r["pages"]["0"] == [_ink("a", "local-edit")]


def test_remote_edit_local_unchanged_keeps_remote():
    base = _state({"0": [_ink("a", "orig")]})
    local = _state({"0": [_ink("a", "orig")]})
    remote = _state({"0": [_ink("a", "remote-edit")]})
    r = _merge(base, local, remote)
    assert r["pages"]["0"] == [_ink("a", "remote-edit")]


def test_both_edit_local_wins():
    base = _state({"0": [_ink("a", "orig")]})
    local = _state({"0": [_ink("a", "local-edit")]})
    remote = _state({"0": [_ink("a", "remote-edit")]})
    r = _merge(base, local, remote)
    assert r["pages"]["0"] == [_ink("a", "local-edit")]


# ---------------------------------------------------------------------------
# Deletions win
# ---------------------------------------------------------------------------

def test_local_delete_remote_edit_stays_deleted():
    base = _state({"0": [_ink("a", "orig")]})
    local = _state({"0": []})
    remote = _state({"0": [_ink("a", "remote-edit")]})
    r = _merge(base, local, remote)
    assert r["pages"].get("0", []) == []


def test_remote_delete_local_edit_stays_deleted():
    base = _state({"0": [_ink("a", "orig")]})
    local = _state({"0": [_ink("a", "local-edit")]})
    remote = _state({"0": []})
    r = _merge(base, local, remote)
    assert r["pages"].get("0", []) == []


# ---------------------------------------------------------------------------
# Start-page stamp: one per score, local wins if local touched it
# ---------------------------------------------------------------------------

def test_start_stamp_moved_on_both_sides_local_wins():
    """placeStartPage mints a brand-new uuid on every move (it removes the
    old mark wherever it was and pushes a fresh one), so a stamp moved on
    both sides independently ends up as two DIFFERENT annotations with no
    uuid in common — merging them naively would keep both. The uuid-keyed
    dedup filter must collapse that back down to one, local's."""
    base = _state({"0": [_start("s0", 0.1, 0.1)]})
    local = _state({"0": [_start("s-local", 0.5, 0.5)]})
    remote = _state({"0": [_start("s-remote", 0.9, 0.9)]})
    r = _merge(base, local, remote)
    starts = [a for page in r["pages"].values() for a in page if a["type"] == "startpage"]
    assert len(starts) == 1, f"expected exactly one start-page stamp after merge, got {starts}"
    assert starts[0]["uuid"] == "s-local"
    assert starts[0]["x"] == 0.5 and starts[0]["y"] == 0.5


def test_start_stamp_removed_locally_stays_removed_even_if_remote_moved_it():
    base = _state({"0": [_start("s0", 0.1, 0.1)]})
    local = _state({"0": []})
    remote = _state({"0": [_start("s-remote", 0.9, 0.9)]})   # remote moved it: a new uuid
    r = _merge(base, local, remote)
    starts = [a for page in r["pages"].values() for a in page if a["type"] == "startpage"]
    assert starts == [], f"local removed the stamp; remote's (differently-uuid'd) move must not resurrect it, got {starts}"


def test_start_stamp_untouched_locally_follows_remote():
    base = _state({"0": [_start("s0", 0.1, 0.1)]})
    local = _state({"0": [_start("s0", 0.1, 0.1)]})            # untouched: same uuid as base
    remote = _state({"1": [_start("s-remote", 0.9, 0.9)]})     # remote moved it: a new uuid, another page
    r = _merge(base, local, remote)
    starts = [(pg, a) for pg, page in r["pages"].items() for a in page if a["type"] == "startpage"]
    assert len(starts) == 1
    pg, a = starts[0]
    assert pg == "1" and a["uuid"] == "s-remote" and a["x"] == 0.9


# ---------------------------------------------------------------------------
# Rotations
# ---------------------------------------------------------------------------

def test_rotation_changed_locally_wins():
    base = _state({}, {"0": 0})
    local = _state({}, {"0": 90})
    remote = _state({}, {"0": 0})
    r = _merge(base, local, remote)
    assert r["rotations"] == {"0": 90}


def test_rotation_changed_remotely_wins_when_local_unchanged():
    base = _state({}, {"0": 0})
    local = _state({}, {"0": 0})
    remote = _state({}, {"0": 180})
    r = _merge(base, local, remote)
    assert r["rotations"] == {"0": 180}


def test_rotation_back_to_zero_locally_drops_the_key():
    base = _state({}, {"0": 90})
    local = _state({}, {"0": 0})
    remote = _state({}, {"0": 90})
    r = _merge(base, local, remote)
    assert r["rotations"] == {}


# ---------------------------------------------------------------------------
# Pages appearing on only one side
# ---------------------------------------------------------------------------

def test_page_only_on_remote_side_is_kept():
    r = _merge(EMPTY, EMPTY, _state({"3": [_ink("a")]}))
    assert r["pages"] == {"3": [_ink("a")]}


def test_page_only_on_local_side_is_kept():
    r = _merge(EMPTY, _state({"3": [_ink("a")]}), EMPTY)
    assert r["pages"] == {"3": [_ink("a")]}


# ---------------------------------------------------------------------------
# Order: remote's order, then local additions
# ---------------------------------------------------------------------------

def test_order_is_remote_order_then_local_additions():
    base = _state({"0": [_ink("a"), _ink("b")]})
    local = _state({"0": [_ink("a"), _ink("b"), _ink("c")]})  # c added locally
    remote = _state({"0": [_ink("b"), _ink("a")]})  # remote reordered
    r = _merge(base, local, remote)
    assert [a["uuid"] for a in r["pages"]["0"]] == ["b", "a", "c"]


# ---------------------------------------------------------------------------
# No mutation of inputs
# ---------------------------------------------------------------------------

def test_inputs_are_not_mutated():
    """Checked inside the JS run, not by round-tripping Python objects: a
    Python dict passed into _merge is serialized into the script and never
    shared with the JS objects mergeAnnotations actually receives, so
    comparing it afterwards would prove nothing."""
    base = _state({"0": [_ink("a", "orig")]})
    local = _state({"0": [_ink("a", "local-edit"), _ink("b")]})
    remote = _state({"0": [_ink("a", "remote-edit")]})
    r = run_module(OUTBOX_JS, ["api"], f"""
const base = {_j(base)}, local = {_j(local)}, remote = {_j(remote)};
const before = JSON.stringify({{ base, local, remote }});
M.mergeAnnotations(base, local, remote);
const after = JSON.stringify({{ base, local, remote }});
console.log(JSON.stringify({{ unchanged: before === after }}));
""")
    assert r["unchanged"] is True
