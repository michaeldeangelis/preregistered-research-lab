"""`lab` command line.

  lab init    --root DIR --name NAME [--description TEXT]
  lab import  --root DIR FILE.md [--batch NAME]      questions from a markdown list
  lab approve --root DIR --batch NAME                Mike's portfolio gate (a whole batch)
  lab approve --root DIR --ids Q-0005,Q-0012         ... or single questions
  lab publish --root DIR Q-0005 [--public]           create the GitHub repo from the question's repo/ (outward)
  lab run     --root DIR [--once]                    move questions through their stages
  lab status  --root DIR [--json]
  lab show    --root DIR QID
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from . import questions as Q
from .kernel import Kernel
from .project import init_project, load_project


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lab", description="agentic research lab kernel")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("init", "import", "approve", "run", "status", "show", "screen-apply", "publish"):
        s = sub.add_parser(name)
        s.add_argument("--root", required=True, type=Path)
        if name == "init":
            s.add_argument("--name", required=True)
            s.add_argument("--description", default="")
        if name == "import":
            s.add_argument("file", type=Path)
            s.add_argument("--batch")
        if name == "approve":
            g = s.add_mutually_exclusive_group(required=True)
            g.add_argument("--batch")
            g.add_argument("--ids", help="comma-separated question ids")
        if name == "run":
            s.add_argument("--once", action="store_true")
        if name == "status":
            s.add_argument("--json", action="store_true")
        if name == "show":
            s.add_argument("qid")
        if name == "publish":
            s.add_argument("qid")
            s.add_argument("--public", action="store_true", help="default is a private repo")
        if name == "screen-apply":
            s.add_argument("screen_json", type=Path)
            s.add_argument("--replacements", type=Path, help="screen markdown with a '## Replacements' section")
            s.add_argument("--batch", default="frontier-v1")
    a = p.parse_args(argv)

    if a.cmd == "init":
        print(init_project(a.root.expanduser().resolve(), a.name, a.description))
        return 0
    project = load_project(a.root)
    if a.cmd == "import":
        batch, created = Q.import_batch(project, a.file, a.batch)
        print(f"batch {batch}: {len(created)} new questions ({', '.join(created[:5])}{'...' if len(created) > 5 else ''})")
        print(f"approve with: lab approve --root {project.root} --batch {batch}")
    elif a.cmd == "approve":
        if a.ids:
            ids, skipped = Q.approve_ids(project, [i.strip() for i in a.ids.split(",") if i.strip()])
            print(f"approved {len(ids)} questions" + (f"; skipped {'; '.join(skipped)}" if skipped else ""))
        else:
            ids = Q.approve_batch(project, a.batch)
            print(f"approved {len(ids)} questions")
    elif a.cmd == "run":
        kernel = Kernel(project)
        steps = kernel.tick() if a.once else kernel.run()
        print(f"{steps} stage steps; digest: {project.qbank / 'digest.md'}")
    elif a.cmd == "status":
        qs = Q.all_questions(project)
        data = {"project": project.name, "root": str(project.root), "questions": len(qs),
                "states": Counter(q["state"] for q in qs), "verdicts": Counter(q.get("verdict") for q in qs if q.get("verdict")),
                "spend_usd": round(sum(q.get("spend_usd", 0) for q in qs), 4),
                "blocked": [{"id": q["id"], "reason": q.get("blocked_reason")} for q in qs if q["state"] == "blocked"],
                "items": [{k: q.get(k) for k in ("id", "state", "type", "verdict", "value", "spend_usd", "text")} for q in qs]}
        if a.json:
            json.dump(data, sys.stdout, indent=1, default=dict)
            print()
        else:
            print(f"{project.name}: {len(qs)} questions, spend ${data['spend_usd']:.2f}")
            for k, v in sorted(data["states"].items()):
                print(f"  {k}: {v}")
    elif a.cmd == "screen-apply":
        from .screen import apply, load_screen
        md = a.replacements.read_text() if a.replacements else ""
        print(json.dumps(apply(project, load_screen(a.screen_json), md, a.batch)))
    elif a.cmd == "publish":
        from . import repo as RP
        q = Q.load(project, a.qid)
        if not q.get("repo"):
            print(f"{a.qid} has no write-up yet (state {q['state']})")
            return 1
        ok, msg = RP.publish(Q.qdir(project, a.qid) / q["repo"], a.public)
        print(msg)
        if ok:
            Q.set_state(project, q, q["state"], published=msg)
        return 0 if ok else 1
    elif a.cmd == "show":
        card = Q.qdir(project, a.qid) / "card.md"
        print(card.read_text() if card.exists() else json.dumps(Q.load(project, a.qid), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
