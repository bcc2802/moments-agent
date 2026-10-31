"""Run a fixed synthetic storage demo. Does not call a model or WeChat."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from workspace import CODE_ROOT, WorkspaceError, initialize, doctor, history, profile
from moments import Moments, MomentsError
from moments_revision import Revision, RevisionError


def _expect(condition, message):
    if not condition:
        raise RuntimeError(message)


def _source_hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((root / "inputs").rglob("*.md"))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="A new private path outside the code directory")
    parser.add_argument("--simulate-publication", action="store_true",
                        help="Explicitly simulate a user report in this new synthetic workspace only")
    args = parser.parse_args(argv)
    try:
        initialized = initialize(args.workspace, demo=True)
        _expect(initialized.get("changed") is True,
                "Demo refuses an existing workspace; choose a new path. No existing drafts were touched.")
        root = Path(initialized["workspace"])
        before = _source_hashes(root)
        fixture = json.loads((CODE_ROOT / "examples/demo-round.json").read_text(encoding="utf-8"))
        _expect(doctor(root)["status"] == "OK", "Initialized workspace failed doctor")
        _expect(profile(root)["status"] == "OK", "Profiles are unreadable")
        imported = history(root, query="清单")
        _expect(imported["status"] == "OK" and imported["items"], "Synthetic history was not found")
        runner = Moments(root / "config.json")
        created = runner.create_round(title=fixture["title"], source_ref=fixture["source_ref"],
                                      variants=fixture["variants"])
        rid = created["round_id"]
        paths = [Path(n["path"]) for n in created["notes"]]
        created_hashes = {p: p.read_bytes() for p in paths}
        state_before_selection = (root / "state.json").read_bytes()
        runner.handle("选2")
        _expect(state_before_selection == (root / "state.json").read_bytes(), "Selection changed state")
        _expect(all(p.read_bytes() == value for p, value in created_hashes.items()), "Selection changed notes")
        Revision(runner).record(n=2, body=fixture["synthetic_final"], round_id=rid, published=False)
        _expect(paths[0].read_bytes() == created_hashes[paths[0]] and
                paths[2].read_bytes() == created_hashes[paths[2]], "Revision changed other variants")
        _expect(Moments(root / "config.json").resume()["round_id"] == rid, "Fresh instance could not resume")
        if args.simulate_publication:
            runner.handle("已发2")
            confirmed = paths[1].read_bytes()
            runner.handle("已发2")
            _expect(paths[1].read_bytes() == confirmed, "Repeat simulated confirmation was not idempotent")
            published = runner.query(status="已发布")
            _expect(len(published["items"]) == 1, "Expected one simulated published variant")
            _expect(not paths[0].exists() and not paths[2].exists(), "Unkept temporary variants remained")
        else:
            _expect(not runner.query(status="已发布")["items"], "Unexpected published state")
        _expect(before == _source_hashes(root), "Input originals changed")
        _expect(doctor(root)["status"] == "OK", "Final workspace failed doctor")
        result = {"status": "PASS", "synthetic_only": True, "model_called": False, "wechat_accessed": False,
                  "publication_simulated": args.simulate_publication, "workspace": str(root), "round_id": rid,
                  "history_matches": len(imported["items"]), "input_originals_unchanged": True,
                  "selected_variant": 2, "notice": "Fixed fixture verifies storage, not model quality or real adoption."}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (WorkspaceError, MomentsError, RevisionError, RuntimeError, OSError, ValueError) as exc:
        print(json.dumps({"status": "FAIL", "code": getattr(exc, "code", "DEMO_FAILED"),
                          "message": str(exc), "synthetic_only": True}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
